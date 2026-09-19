import importlib.util
import json
from pathlib import Path


def module():
    spec = importlib.util.spec_from_file_location("subset_cli", Path("scripts/swebench_subset.py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_wait_does_not_treat_verifying_as_terminal(monkeypatch):
    subset = module()
    states = iter(["VERIFYING", "SUCCEEDED"])
    monkeypatch.setattr(subset.cli, "api", lambda _: {"status": next(states)})
    sleeps = []
    monkeypatch.setattr(subset.time, "sleep", lambda seconds: sleeps.append(seconds))
    assert subset.wait_for_runs(["run"])["run"] == "SUCCEEDED"
    assert sleeps == [30]


def test_batch_retry_persists_final_id_and_all_attempt_usage(tmp_path, monkeypatch):
    subset = module()
    cache = tmp_path / "cache"
    cache.mkdir()
    instance = {"instance_id": "case", "repo": "test/repo", "base_commit": "a" * 40,
                "problem_statement": "fix", "image": "test:latest", "patch": "gold"}
    (cache / "case.json").write_text(json.dumps(instance))
    monkeypatch.setattr(subset, "CACHE", cache)
    monkeypatch.setattr(subset, "VALIDATION", tmp_path / "batches")
    monkeypatch.setattr(subset, "ROOT", tmp_path)
    monkeypatch.setattr(subset, "ensure_images", lambda ids: ([], []))
    monkeypatch.setattr(subset, "wait_for_runs", lambda ids: {})
    monkeypatch.setattr(subset.sys, "argv", ["subset", "--instances", "case"])
    submitted, imported = [], []
    def api(path, payload=None):
        if path == "/runs":
            assert payload is not None, "must never query global listing"
            run_id = f"run-{len(submitted)}"
            submitted.append(run_id)
            return {"id": run_id}
        if path.endswith("/evaluation"):
            imported.append((path, payload))
            return {}
        run_id = path.split("/")[-1]
        first = run_id == "run-0"
        return {"id": run_id, "status": "FAILED" if first else "SUCCEEDED", "spec": {"instanceId": "case"},
                "result": {"error": "Connection error" if first else None, "patch": "" if first else "current",
                           "model_calls": 3 if first else 4, "usage_status": "complete"}}
    monkeypatch.setattr(subset.cli, "api", api)
    monkeypatch.setattr(subset, "harness", lambda *args, **kwargs: {"resolved_ids": ["case"],
                        "unresolved_ids": [], "empty_patch_ids": [], "error_ids": [], "total_instances": 1})
    assert subset.main() == 0
    reports = list((tmp_path / "batches").glob("*/manifest.json"))
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["runs"] == {"case": "run-1"}
    assert report["attempts"] == {"case": ["run-0", "run-1"]}
    assert report["generative_model_calls"] == 7
    assert report["bindings"][0]["run_id"] == "run-1"
    assert imported[0][0] == "/runs/run-1/evaluation"
    assert "current" in (reports[0].parent / "predictions.jsonl").read_text()
