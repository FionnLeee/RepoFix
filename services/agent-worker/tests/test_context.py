import copy
import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from minisweagent.models.test_models import DeterministicModel, make_output
from qdrant_client import QdrantClient
from repofix.context import ContextConfig, ContextManager, project_rules, tokens
from repofix.indexing import CodeIndex, chunks, content_hash
from repofix.repository import digest
from repofix.runtime import Sandbox, TracedAgent


class Encoder:
    version = "e" * 64

    def documents(self, texts):
        return [[1.0] + [0.0] * 383 for _ in texts]

    def query(self, text):
        return self.documents([text])[0]


class Registry:
    def __init__(self, run):
        self.run, self.builds, self.published = run, {}, {}
        self.memory = {"memories": [], "compactRequested": 0, "memoryEnabled": True}

    def call(self, suffix, body=None):
        if suffix == "context":
            return copy.deepcopy(self.memory)
        if suffix == "indexes/begin":
            scope = "base" if body["scope"] == "base" else self.run["id"] + ":1"
            key = (scope, body["snapshotHash"], body["manifestHash"])
            if key in self.published:
                return {**self.published[key], "reused": True}
            build = {**body, "head": {"scope": scope}, "collection": "repofix_" + Encoder.version[:24], "key": key}
            self.builds[body["id"]] = build
            return build
        build = self.builds[body["id"]]
        self.published[build["key"]] = build
        return build

    def close(self):
        pass


def run_fixture():
    return {"id": str(uuid.uuid4()), "projectId": str(uuid.uuid4()), "generation": 1, "workerId": "test",
            "task": "Fix discount; MUST preserve currency rounding", "memoryEnabled": True,
            "spec": {"commit": "a" * 40, "allowedPaths": ["pkg/a.py"]}}


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect")
def test_published_index_overlay_deletion_isolation_and_unavailable_fallback(monkeypatch):
    run = run_fixture()
    base = {"pkg/a.py": "def discount():\n    return 'old'\n", "removed.py": "DELETE_SENTINEL = 1"}
    control = Registry(run)
    client = QdrantClient(":memory:")
    events = []
    index = CodeIndex(run, base, control, lambda kind, data: events.append((kind, data)), Encoder(), client)
    initial, report = index.search("discount", base)
    assert report["strategy"] == "fused" and initial
    current = {"pkg/a.py": "def discount():\n    return 'new'\n", "added.py": "ADDED = 2"}
    found, report = index.search("discount", current)
    assert report["strategy"] == "fused" and len(report["indexes"]) == 2
    assert {p["path"] for p in found} == set(current)
    assert all(p["file_hash"] == content_hash(current[p["path"]]) for p in found)
    assert not any("old" in p["text"] or "DELETE_SENTINEL" in p["text"] for p in found)
    other_run = run_fixture()
    other = CodeIndex(other_run, {"secret.py": "OTHER_PROJECT"}, Registry(other_run), lambda *_: None, Encoder(), client)
    other.search("discount", other.base)
    found, _ = index.search("OTHER_PROJECT discount", current)
    assert not any("OTHER_PROJECT" in p["text"] for p in found)

    def down(*_, **__):
        raise ConnectionError("Qdrant unavailable")

    monkeypatch.setattr(client, "upsert", down)
    newer = {"pkg/a.py": "def discount():\n    return 'fresh'\n"}
    before = len(control.published)
    found, report = index.search("discount", newer)
    assert report["strategy"] == "literal" and len(control.published) == before
    assert all("fresh" in p["text"] for p in found)
    assert events[-1][0] == "INDEX_FALLBACK"
    client.close()


def test_ast_chunks_cover_decorators_and_broken_code():
    files = {"a.py": "import functools\n@decorator\ndef work():\n    pass\n", "b.py": "def broken(:\n new"}
    result = chunks(files)
    function = next(c for c in result if c["symbol"] == "work")
    assert function["start"] == 2 and "@decorator" in function["text"]
    assert any(c["path"] == "b.py" for c in result)


def test_rule_scope_precedence_and_budget():
    files = {"AGENTS.md": "root", "pkg/AGENTS.md": "nested", "other/AGENTS.md": "unrelated"}
    rules = project_rules(files, ["pkg/a.py"])
    assert [r["content"] for r in rules] == ["root", "nested"]
    assert rules[-1]["scope"] == "pkg"
    with pytest.raises(ValueError, match="budget"):
        project_rules(files, ["pkg/a.py"], 2)


class CurrentIndex:
    def search(self, query, files):
        return chunks(files), {"indexes": [], "changed_paths": ["pkg/a.py"], "strategy": "literal"}

    def close(self):
        pass


def test_long_history_budget_verbatim_constraints_memory_revocation_and_manual_compaction(tmp_path):
    run = run_fixture()
    control = Registry(run)
    good = {"id": "good", "version": 1, "scope": "project", "validity": "active", "content": "MEMORY_VALID",
            "sourceRun": run["id"], "evidenceRefs": [1], "baseCommit": "a" * 40}
    control.memory["memories"] = [good, {**good, "id": "stale", "validity": "needs_review", "content": "STALE_MEMORY"}]
    files = {"AGENTS.md": "NEVER change rounding", "pkg/AGENTS.md": "use decimal", "pkg/a.py": "value = 1"}
    messages = [{"role": "system", "content": "Platform policy"}, {"role": "user", "content": run["task"]}]
    for step in range(12):
        messages.extend([{"role": "assistant", "content": f"action {step}"},
                         {"role": "user", "content": "LOG" * 8000}])
    agent = SimpleNamespace(messages=messages, full_messages=copy.deepcopy(messages), compactions=[], n_calls=12)
    manager = ContextManager(run, files, lambda *_: None, tmp_path, control, CurrentIndex(), ContextConfig(window=8192))
    request = manager.prepare(agent, files)
    text = json.dumps(request)
    assert tokens(request) <= manager.config.input_limit
    assert run["task"] in text and "NEVER change rounding" in text and "MEMORY_VALID" in text
    assert "STALE_MEMORY" not in text and len(agent.messages) == 26
    assert request[-2]["role"] == "assistant" and request[-1]["role"] == "user"
    assert agent.compactions[-1]["summary"]["attempted_changes"] == ["pkg/a.py"]
    assert agent.compactions[-1]["summary"]["evidence_refs"][0]["hash"] == content_hash(files["pkg/a.py"])
    control.memory["memories"] = []
    control.memory["compactRequested"] = 1
    request = manager.prepare(agent, {**files, "pkg/a.py": "value = 2"})
    assert "MEMORY_VALID" not in json.dumps(request)
    assert agent.compactions[-1]["manual"] and manager.state["compact_seen"] == 1
    archived = json.loads((tmp_path / "context" / "call-13.json").read_text())
    assert archived["report"]["memories"] == []


def test_context_does_not_use_cached_memory_when_control_plane_unavailable(tmp_path):
    run = run_fixture()
    control = Registry(run)

    def fail(*_):
        raise httpx.ConnectError("Control unavailable")

    control.call = fail
    manager = ContextManager(run, {}, lambda *_: None, tmp_path, control, CurrentIndex())
    with pytest.raises(httpx.ConnectError):
        manager.prepare(SimpleNamespace(), {})


def test_old_workspace_observations_and_test_summary_are_invalidated_without_erasing_history(tmp_path):
    run = run_fixture()
    control = Registry(run)
    old = {"pkg/a.py": "value = 1"}
    current = {"pkg/a.py": "value = 2"}
    history = [{"role": "system", "content": "repair"}, {"role": "user", "content": run["task"]},
        {"role": "assistant", "content": "run development tests", "extra": {"actions": [{"command": "python -m unittest"}]}},
        {"role": "user", "content": "OLD_TEST_SUCCESS_AND_SOURCE", "extra": {
            "workspace_sha256": digest(old), "raw_output": "OLD_TEST_SUCCESS_AND_SOURCE"}},
        {"role": "assistant", "content": "modify file"},
        {"role": "user", "content": "CURRENT_EDIT_RESULT", "extra": {"workspace_sha256": digest(current)}}]
    agent = SimpleNamespace(messages=history, full_messages=copy.deepcopy(history), compactions=[], n_calls=2)
    manager = ContextManager(run, old, lambda *_: None, tmp_path, control, CurrentIndex())
    for manual in (0, 1):
        control.memory["compactRequested"] = manual
        request = manager.prepare(agent, current)
        text = json.dumps(request)
        assert "OLD_TEST_SUCCESS_AND_SOURCE" not in text
        assert "CURRENT_EDIT_RESULT" in text and "earlier workspace version" in text
        assert agent.messages[3]["content"] == agent.full_messages[3]["content"] == "OLD_TEST_SUCCESS_AND_SOURCE"
        assert request[-2]["role"] == "assistant" and request[-1]["role"] == "user"
    summary = manager.state["summary"]
    assert summary["latest_test"]["stale"] is True
    assert summary["latest_test"]["workspace_sha256"] == digest(old)
    report = json.loads((tmp_path / "context" / "call-3.json").read_text())["report"]
    assert report["stale_observation_count"] == 1


@pytest.mark.docker
def test_full_tool_log_can_be_reread_without_host_path_access(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    sandbox = Sandbox(str(uuid.uuid4()), lambda *_: None, threading.Event(), files={"a.py": "pass"})
    sandbox.archive_logs = True
    try:
        result = sandbox.execute({"command": "python -c \"print('x'*22000 + 'FINAL_SENTINEL')\""})
        assert "FINAL_SENTINEL" not in result["output"]
        log_id = next(sandbox.log_folder.iterdir()).stem
        read = sandbox.execute({"command": f"repofix_read_log {log_id} 18000"})
        assert "FINAL_SENTINEL" in read["output"]
        with pytest.raises(ValueError):
            sandbox.execute({"command": "repofix_read_log ../credentials 0"})
    finally:
        sandbox.close()


@pytest.mark.docker
def test_managed_checkpoint_restores_context_state_and_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {**run_fixture(), "mode": "demo", "contextMode": "managed"}
    source = {"pkg/a.py": "value = 0"}
    control = Registry(run)
    control.memory["compactRequested"] = 2

    class Pause(BaseException):
        pass

    class PausingAgent(TracedAgent):
        def save(self, path, *extra):
            data = super().save(path, *extra)
            if self.n_calls == 1:
                raise Pause()
            return data

    def build(cls, sandbox):
        model = DeterministicModel(outputs=[make_output("edit", [{"command": "echo 'value = 1' > pkg/a.py"}], cost=0),
            make_output("submit", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}], cost=0)])
        agent = cls(model, sandbox, emit=lambda *_: None, cancelled=threading.Event(), context_mode="managed",
                    system_template="repair", instance_template="{{task}}", output_path=tmp_path / "trajectory.json")
        agent.context_manager = ContextManager(run, source, lambda *_: None, tmp_path, control, CurrentIndex())
        agent.enable_checkpoints(run, source)
        return agent

    first = Sandbox(run["id"], lambda *_: None, threading.Event(), files=source)
    try:
        original = build(PausingAgent, first)
        with pytest.raises(Pause):
            original.run(run["task"])
        reference = original.checkpoints.latest
        checkpoint = original.checkpoints.load(reference, for_resume=True)
        assert checkpoint.state.context_state["compact_seen"] == 2
        assert checkpoint.state.messages[-1]["extra"]["workspace_sha256"] == digest(checkpoint.files)
    finally:
        first.close()
    second = Sandbox(run["id"], lambda *_: None, threading.Event(), files=checkpoint.files,
                     image=checkpoint.binding["image_id"])
    try:
        restored = build(TracedAgent, second)
        restored.restore_checkpoint(reference)
        assert restored.context_manager.state == checkpoint.state.context_state
        assert restored.run(run["task"])["exit_status"] == "Submitted"
        assert restored.n_calls == 2 and restored.env.index == 1
        assert len([m for m in restored.full_messages if m["role"] == "assistant"]) == 2
    finally:
        second.close()
