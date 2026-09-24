import importlib.util
import json
import threading
from pathlib import Path
from uuid import uuid4

import litellm
import pytest
from repopilot.reporting import failure_result, trajectory_summary, usage_summary
from repopilot.runtime import SafeModel, TracedAgent


class RecordedInvalidModel(SafeModel):
    def _query(self, messages, **kwargs):
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "I will inspect the code."}}],
            usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        )


class UnusedEnvironment:
    def get_template_vars(self):
        return {}

    def serialize(self):
        return {}

    def execute(self, action):
        raise AssertionError("Malformed model output must not execute commands")


def test_repeated_format_failure_preserves_all_usage_and_redacts_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_POLICY", "free-quota")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    run = {"id": str(uuid4()), "mode": "live"}
    agent = TracedAgent(
        RecordedInvalidModel(model_name="openai/deepseek-v4-pro-0813", cost_tracking="ignore_errors"), UnusedEnvironment(),
        emit=lambda *_: None, cancelled=threading.Event(), system_template="Repair the repository",
        instance_template="{{task}}", output_path=tmp_path / run["id"] / "trajectory.json", step_limit=5,
    )
    assert agent.run("Fix the bug")["exit_status"] == "RepeatedFormatError"
    result = failure_result(run, RuntimeError("RepeatedFormatError test-secret"))
    assert result["model_calls"] == result["usage_calls_reported"] == 3
    assert result["usage"] == {"input_tokens": 36, "output_tokens": 9}
    assert result["usage_status"] == "complete"
    assert result["stop_reason"] == "RepeatedFormatError"
    assert "test-secret" not in json.dumps(result)
    assert json.loads((tmp_path / run["id"] / "result.json").read_text()) == result
    feedback = [m["content"] for m in agent.full_messages if m.get("extra", {}).get("interrupt_type") == "FormatError"]
    assert len(feedback) == 3 and all("```mswea_bash_command\npwd\n```" in m for m in feedback)


def test_usage_coverage_does_not_treat_partial_or_invalid_data_as_complete():
    messages = [{"extra": {"response": {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}}},
                {"extra": {"response": "unserializable response"}},
                {"extra": {"response": {"usage": {"prompt_tokens": 5}}}},
                {"extra": {"response": {"usage": {"prompt_tokens": -1, "completion_tokens": 2}}}}]
    result = usage_summary(messages, 4, "live")
    assert result["usage_status"] == "partial" and result["usage_calls_reported"] == 1
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert usage_summary([], 1, "live")["usage"] is None
    assert usage_summary([], None, "live")["usage_status"] == "unavailable"
    assert usage_summary([], 4, "demo")["usage_status"] == "complete"


def test_missing_or_corrupt_trajectory_does_not_hide_original_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid4()), "mode": "live"}
    assert failure_result(run, RuntimeError("Sandbox failed"))["usage_status"] == "unavailable"
    (tmp_path / run["id"] / "trajectory.json").write_text("not json")
    result = failure_result(run, RuntimeError("Original failure"))
    assert result["error"] == "Original failure" and result["usage"] is None
    with pytest.raises(ValueError):
        trajectory_summary("../outside", "live", tmp_path)


def test_recovery_preserves_original_report_failures_and_denominator(tmp_path):
    spec = importlib.util.spec_from_file_location("evaluate_baselines", Path("scripts/evaluate_baselines.py"))
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    rid = str(uuid4())
    folder = tmp_path / "artifacts" / rid
    folder.mkdir(parents=True)
    response = {"extra": {"response": {"usage": {"prompt_tokens": 20, "completion_tokens": 5}}}}
    (folder / "trajectory.json").write_text(json.dumps({
        "info": {"model_stats": {"api_calls": 2}, "exit_status": "RepeatedFormatError"},
        "messages": [response], "full_messages": [response, response], "context_compactions": [],
    }))
    source = tmp_path / "source.json"
    original = json.dumps({"mode": "live", "runs": [
        {"run_id": rid, "context": "full", "status": "FAILED", "result": {"error": "RepeatedFormatError"}},
        {"run_id": str(uuid4()), "context": "full", "status": "FAILED", "result": None},
    ]})
    source.write_text(original)
    report = evaluator.recover_report(source, tmp_path / "artifacts", tmp_path / "reports")
    assert source.read_text() == original
    assert [r["status"] for r in report["runs"]] == ["FAILED", "FAILED"]
    assert report["runs"][0]["result"]["error"] == "RepeatedFormatError"
    assert report["summary"]["full"]["attempted"] == 2
    assert report["summary"]["full"]["usage_complete_runs"] == 1
    assert report["summary"]["full"]["input_tokens_reported"] == 40
    assert len(list((tmp_path / "reports").glob("recovered-*.json"))) == 1
