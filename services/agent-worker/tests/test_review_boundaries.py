import threading
from types import SimpleNamespace

import pytest
from minisweagent.exceptions import LimitsExceeded
from minisweagent.models.test_models import DeterministicModel, make_output
from repopilot import review
from repopilot.quota import QuotaUnavailable
from repopilot.repository_runtime import review_before_acceptance
from repopilot.runtime import Cancelled, TracedAgent


class Quota:
    limit = 1

    def __init__(self):
        self.held = False
        self.acquisitions = 0

    def acquire(self):
        assert not self.held
        self.held = True
        self.acquisitions += 1
        return 0

    def release(self):
        assert self.held
        self.held = False


def agent_at(tmp_path, limit=5):
    agent = TracedAgent(DeterministicModel(outputs=[make_output("read", [{"command": "pwd"}])]),
                        SimpleNamespace(serialize=lambda: {}), emit=lambda *_: None, cancelled=threading.Event(),
                        system_template="", instance_template="", step_limit=limit,
                        output_path=tmp_path / "trajectory.json")
    agent.add_messages({"role": "user", "content": "fix"})
    return agent


def test_coder_and_reviewer_share_admission_and_budget(tmp_path):
    agent = agent_at(tmp_path, limit=2)
    quota = Quota()
    agent.enable_quota(quota)
    agent.query()
    def complete():
        assert quota.held
        return {"content": "ok", "cost": 0, "usage": None}
    agent.perform_review(complete)
    assert quota.acquisitions == 2 and not quota.held and agent.n_calls == 2
    with pytest.raises(LimitsExceeded):
        agent.perform_review(lambda: pytest.fail("must not call model"))
    assert quota.acquisitions == 2


def test_cancel_after_quota_wait_releases_slot_without_call(tmp_path):
    agent = agent_at(tmp_path)
    quota = Quota()
    acquire = quota.acquire
    def delayed_acquire():
        result = acquire()
        agent.cancelled.set()
        return result
    quota.acquire = delayed_acquire
    agent.enable_quota(quota)
    with pytest.raises(Cancelled):
        agent.perform_review(lambda: pytest.fail("must not call model"))
    assert not quota.held and agent.n_calls == 0


def test_redis_failure_does_not_remove_the_limit(tmp_path):
    agent = agent_at(tmp_path)
    def unavailable():
        raise QuotaUnavailable("offline")
    agent.enable_quota(SimpleNamespace(acquire=unavailable, limit=1))
    with pytest.raises(RuntimeError, match="未发出模型请求"):
        agent.perform_review(lambda: pytest.fail("must not call model"))
    assert agent.n_calls == 0


@pytest.mark.parametrize("answer", ["invalid", '{"summary":"missing findings"}', '{"findings":null}'])
def test_invalid_review_is_failed_at_the_orchestrator_not_clean(tmp_path, monkeypatch, answer):
    monkeypatch.setattr(review, "reviewer_for", lambda *args: SimpleNamespace(
        complete=lambda _: {"content": answer, "cost": 0}))
    events = []
    result = review_before_acceptance(agent_at(tmp_path), {"mode": "live", "task": "fix"},
        SimpleNamespace(allowedPaths=["a.py"], testCommand="test"),
        SimpleNamespace(execute=lambda _: {"output": "ok"}),
        lambda: ({"a.py": "x=1"}, ["a.py"], "patch"), None,
        lambda event, data: events.append(event), "fix")
    assert result[0][0]["status"] == "failed"
    assert "REVIEW_FAILED" in events and "REVIEW_COMPLETED" not in events


def test_test_mutation_invalidates_evidence_before_model_call(tmp_path, monkeypatch):
    workspace = {"a.py": "x=1"}
    def execute(_):
        workspace["a.py"] = "x=2"
        return {"output": "PASS x=2"}
    monkeypatch.setattr(review, "reviewer_for", lambda *args: pytest.fail("must not call model"))
    result = review_before_acceptance(agent_at(tmp_path), {"mode": "live", "task": "fix"},
        SimpleNamespace(allowedPaths=["a.py"], testCommand="test"), SimpleNamespace(execute=execute),
        lambda: (dict(workspace), ["a.py"], workspace["a.py"]), None, lambda *_: None, "fix")
    assert result[0][0]["status"] == "failed"
    assert result[2] == workspace and result[4] == "x=2"


def test_deleted_and_truncated_sources_are_explicitly_partial():
    reviewer = SimpleNamespace(complete=lambda _: {"content": '{"findings":[]}', "cost": 0})
    result = review.perform(reviewer, {"task": "fix"},
                            SimpleNamespace(allowedPaths=["gone.py"], testCommand="test"),
                            {}, ["gone.py"], "deleted file", "ok", 0)
    assert result["partial"] and result["parse_error"] is None


def test_deleted_file_findings_bind_to_base_and_large_file_uses_changed_hunk():
    text = '{"findings":[{"file":"gone.py","line":1,"side":"base","severity":"blocking","finding":"removed API"}]}'
    assert review.parse(text, {}, {"gone.py": "def api(): pass\n"})["findings"][0]["side"] == "base"
    assert review.parse(text, {})["invalid"]
    candidate = {"large.py": "\n".join(["unchanged"] * 999 + ["CHANGED_HERE"])}
    patch = "diff --git a/large.py b/large.py\n@@ -1000 +1000 @@\n-old\n+CHANGED_HERE"
    messages, partial = review.request({"task": "fix"},
        SimpleNamespace(allowedPaths=["large.py"], testCommand="test"), candidate, ["large.py"], patch,
        "actual test output", with_coverage=True)
    assert "1000 | CHANGED_HERE" in messages[1]["content"] and partial
    assert "actual test output" in messages[1]["content"]
