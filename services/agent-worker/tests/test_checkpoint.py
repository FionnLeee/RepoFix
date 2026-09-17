import copy
import json
import threading
import time
import uuid

import pytest
from minisweagent.models.test_models import DeterministicModel, make_output
from repopilot.checkpoint import (
    AgentState,
    Checkpoint,
    CheckpointStore,
    atomic_json,
    binding_matches,
    checksum,
    load_registered,
)
from repopilot.repository import digest
from repopilot.repository_runtime import read_tree
from repopilot.runtime import ApprovalPaused, Sandbox, TracedAgent


class BoundaryPause(BaseException):
    pass


class PauseAfterFirstTool(TracedAgent):
    def save(self, path, *extra):
        result = super().save(path, *extra)
        if self.n_calls == 1:
            raise BoundaryPause()
        return result


@pytest.mark.docker
@pytest.mark.parametrize("step_limit,exit_status", [(3, "Submitted"), (2, "LimitsExceeded")])
def test_restore_new_sandbox_continues_without_replaying_tools_or_resetting_budget(tmp_path, monkeypatch,
                                                                                step_limit, exit_status):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "mode": "demo", "task": "increment twice"}
    source = {"count.txt": "0", "remove.txt": "old"}
    commands = [
        "python -c \"from pathlib import Path; p=Path('count.txt'); p.write_text(str(int(p.read_text())+1)); "
        "Path('added.txt').write_text('new'); Path('remove.txt').unlink()\"",
        "python -c \"from pathlib import Path; p=Path('count.txt'); p.write_text(str(int(p.read_text())+1))\"",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]

    def build(cls, sandbox):
        agent = cls(DeterministicModel(outputs=[make_output("action", [{"command": c}], cost=0.1)
                                                for c in commands], cost_per_call=0.1),
                    sandbox, emit=lambda *_: None, cancelled=threading.Event(), system_template="repair",
                    instance_template="{{task}}", step_limit=step_limit, cost_limit=1.,
                    wall_time_limit_seconds=120, output_path=tmp_path / run["id"] / "trajectory.json")
        agent.enable_checkpoints(run, source)
        return agent

    first = Sandbox(run["id"], lambda *_: None, threading.Event(), files=source)
    try:
        agent = build(PauseAfterFirstTool, first)
        agent._start_time = time.time() - 30
        with pytest.raises(BoundaryPause):
            agent.run(run["task"])
        reference = agent.checkpoints.latest
        checkpoint = agent.checkpoints.load(reference, for_resume=True)
        assert checkpoint.files == {"count.txt": "1", "added.txt": "new"}
        assert checkpoint.state.model_calls == checkpoint.state.tool_calls == 1
        assert checkpoint.state.cost == pytest.approx(0.1)
        assert checkpoint.state.active_seconds >= 30
    finally:
        first.close()

    second = Sandbox(run["id"], lambda *_: None, threading.Event(), files=checkpoint.files,
                     image=checkpoint.binding["image_id"])
    try:
        restored = build(TracedAgent, second)
        restored.restore_checkpoint(reference)
        assert restored.n_calls == 1 and restored.env.index == 1
        assert time.time() - restored._start_time >= 30
        with pytest.raises(ValueError, match="task parameters"):
            restored.run("another task")
        assert restored.run(run["task"])["exit_status"] == exit_status
        assert read_tree(second) == {"count.txt": "2", "added.txt": "new"}
        assert restored.n_calls == step_limit
        assert restored.cost == pytest.approx(step_limit * 0.1)
        assert restored.env.index == 2
        assert restored.checkpoints.sequence > reference["sequence"]
        with pytest.raises(ValueError, match="terminal"):
            restored.checkpoints.load(restored.checkpoints.latest, for_resume=True)
    finally:
        second.close()


def checkpoint_fixture(tmp_path):
    binding = {"run_id": str(uuid.uuid4()), "generation": 1, "task_sha256": "task", "image_id": "image"}
    store = CheckpointStore(tmp_path, binding, lambda *_: None)
    state = AgentState(messages=[{"role": "user", "content": "task"}], full_messages=[], compactions=[],
                       template_vars={"task": "task"}, model_calls=2, tool_calls=2, cost=0., active_seconds=12.,
                       consecutive_format_errors=0)
    doc = Checkpoint(schema_version=1, id=uuid.uuid4().hex, binding=binding, phase="ready", sequence=3,
                     created_at=time.time(), files={"a.py": "x=1"}, workspace_sha256=digest({"a.py": "x=1"}),
                     state=state).model_dump(mode="json")
    ref = {"id": doc["id"], "sha256": checksum(doc), "generation": 1, "sequence": 3, "phase": "ready",
           "workspace_sha256": doc["workspace_sha256"]}
    atomic_json(store.folder / f"{doc['id']}.json", doc)
    atomic_json(store.folder / "latest.json", ref)
    return store, doc, ref


def test_checkpoint_rejects_tampering_wrong_binding_inflight_and_older_budget(tmp_path):
    store, doc, ref = checkpoint_fixture(tmp_path)
    assert store.load(ref, for_resume=True).state.model_calls == 2
    with pytest.raises(ValueError, match="Invalid checkpoint"):
        store.load({"id": "../outside"})
    store.binding = {**store.binding, "task_sha256": "changed"}
    with pytest.raises(ValueError, match="mismatch"):
        store.load(ref)
    store.binding = doc["binding"]
    store.mark_inflight(3)
    with pytest.raises(ValueError, match="in-flight"):
        store.load(ref, for_resume=True)
    (store.folder / "inflight.json").unlink()
    atomic_json(store.folder / "latest.json", {**ref, "id": uuid.uuid4().hex})
    with pytest.raises(ValueError, match="Older checkpoints"):
        store.load(ref, for_resume=True)
    atomic_json(store.folder / "latest.json", ref)
    corrupted = copy.deepcopy(doc)
    corrupted["files"]["a.py"] = "changed"
    atomic_json(store.folder / f"{doc['id']}.json", corrupted)
    with pytest.raises(ValueError, match="checksum"):
        store.load(ref)
    with pytest.raises(ValueError, match="workspace checksum"):
        store.load({**ref, "sha256": checksum(corrupted)})
    corrupted = {**doc, "schema_version": 2}
    atomic_json(store.folder / f"{doc['id']}.json", corrupted)
    with pytest.raises(ValueError):
        store.load({**ref, "sha256": checksum(corrupted)})


def test_atomic_write_failure_keeps_previous_checkpoint_reference(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    atomic_json(target, {"id": "previous"})

    def fail_replace(*_):
        raise OSError("Simulated disk publication failure")

    monkeypatch.setattr("repopilot.checkpoint.os.replace", fail_replace)
    with pytest.raises(OSError):
        atomic_json(target, {"id": "new"})
    assert json.loads(target.read_text()) == {"id": "previous"}
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.docker
def test_unknown_tool_result_keeps_previous_snapshot_and_blocks_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "task": "change file", "mode": "demo"}

    def emit(kind, *_):
        if kind == "TOOL_RESULT":
            raise OSError("Tool result publication failed")

    sandbox = Sandbox(run["id"], emit, threading.Event(), files={"a.py": "old"})
    try:
        agent = TracedAgent(DeterministicModel(outputs=[make_output("edit", [{"command": "echo new > a.py"}])]),
                            sandbox, emit=emit, cancelled=threading.Event(), system_template="repair",
                            instance_template="{{task}}", output_path=tmp_path / "trajectory.json")
        agent.enable_checkpoints(run, {"a.py": "old"})
        with pytest.raises(OSError, match="publication failed"):
            agent.run(run["task"])
        assert read_tree(sandbox)["a.py"] == "new\n"
        assert agent.checkpoints.load(agent.checkpoints.latest).files == {"a.py": "old"}
        with pytest.raises(ValueError, match="in-flight"):
            agent.checkpoints.load(agent.checkpoints.latest, for_resume=True)
    finally:
        sandbox.close()


@pytest.mark.docker
def test_checkpoint_refuses_background_writers():
    sandbox = Sandbox(str(uuid.uuid4()), lambda *_: None, threading.Event(), files={"a.py": "old"})
    try:
        assert read_tree(sandbox, quiescent=True) == {"a.py": "old"}
        sandbox.container.exec_run(["sleep", "60"], detach=True)
        with pytest.raises(ValueError):
            read_tree(sandbox, quiescent=True)
    finally:
        sandbox.close()


@pytest.mark.docker
@pytest.mark.parametrize("budget,status", [("time", "TimeExceeded"), ("cost", "LimitsExceeded")])
def test_restored_exhausted_budget_stops_before_another_model_or_tool_call(tmp_path, monkeypatch, budget, status):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "mode": "demo", "task": "keep progress"}
    source = {"count.txt": "0"}

    def build(cls, sandbox):
        agent = cls(DeterministicModel(outputs=[make_output("edit", [{"command": "echo 1 > count.txt"}], cost=0.1)]),
                    sandbox, emit=lambda *_: None, cancelled=threading.Event(), system_template="repair",
                    instance_template="{{task}}", cost_limit=0.1 if budget == "cost" else 0,
                    wall_time_limit_seconds=60, context_mode="compact", output_path=tmp_path / "trajectory.json")
        agent.enable_checkpoints(run, source)
        return agent

    first = Sandbox(run["id"], lambda *_: None, threading.Event(), files=source)
    try:
        agent = build(PauseAfterFirstTool, first)
        with pytest.raises(BoundaryPause):
            agent.run(run["task"])
        # Simulate elapsed active work and a compacted history at a safe boundary.
        for _ in range(3):
            agent.add_messages({"role": "assistant", "content": "a" * 1600},
                               {"role": "user", "content": "observation"})
        agent.compact_context()
        assert agent.compactions and agent.messages != agent.full_messages
        if budget == "time":
            agent._start_time = time.time() - 65
        reference = agent.checkpoints.save(agent, "ready")
        checkpoint = agent.checkpoints.load(reference, for_resume=True)
    finally:
        first.close()
    second = Sandbox(run["id"], lambda *_: None, threading.Event(), files=checkpoint.files,
                     image=checkpoint.binding["image_id"])
    try:
        restored = build(TracedAgent, second)
        restored.restore_checkpoint(reference)
        assert restored.messages == checkpoint.state.messages
        assert restored.full_messages == checkpoint.state.full_messages
        assert restored.compactions == checkpoint.state.compactions
        assert restored.run(run["task"])["exit_status"] == status
        assert restored.n_calls == restored.env.index == 1
        assert restored.cost == pytest.approx(0.1)
        assert read_tree(second) == {"count.txt": "1\n"}
    finally:
        second.close()


@pytest.mark.docker
def test_failed_registration_never_publishes_a_new_recovery_point(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "mode": "demo", "task": "task"}

    def reject(*_):
        raise OSError("Registration unavailable")

    sandbox = Sandbox(run["id"], lambda *_: None, threading.Event(), files={"a.py": "old"})
    try:
        agent = TracedAgent(DeterministicModel(outputs=[]), sandbox, emit=lambda *_: None,
                            cancelled=threading.Event(), system_template="repair", instance_template="{{task}}")
        agent.enable_checkpoints(run, {"a.py": "old"})
        agent.add_messages({"role": "user", "content": "task"})
        reference = agent.checkpoints.save(agent, "ready")
        agent.checkpoints.mark_inflight(1)
        agent.checkpoints.emit = reject
        with pytest.raises(OSError, match="Registration unavailable"):
            agent.checkpoints.save(agent, "ready")
        assert agent.checkpoints.latest == reference
        assert json.loads((agent.checkpoints.folder / "latest.json").read_text()) == reference
        with pytest.raises(ValueError, match="in-flight"):
            agent.checkpoints.load(reference, for_resume=True)
    finally:
        sandbox.close()


def test_binding_matches_only_earlier_generations_when_recovering():
    binding = {"run_id": "run", "generation": 2, "task_sha256": "task", "source_sha256": "source", "image_id": "image"}
    earlier = {**binding, "generation": 1}
    assert binding_matches(binding, earlier, allow_previous_generation=True) is True
    assert binding_matches(binding, binding, allow_previous_generation=True) is False
    assert binding_matches(binding, {**earlier, "generation": 3}, allow_previous_generation=True) is False
    assert binding_matches(binding, {**earlier, "task_sha256": "other"}, allow_previous_generation=True) is False
    assert binding_matches(binding, binding) is True
    assert binding_matches(binding, earlier) is False


def pending_action_fixture(tmp_path, action=None, generation=1):
    action = action or {"command": "echo changed > other.py"}
    binding = {"run_id": str(uuid.uuid4()), "generation": generation, "task_sha256": "task", "image_id": "image"}
    store = CheckpointStore(tmp_path, binding, lambda *_: None)
    state = AgentState(messages=[{"role": "user", "content": "task"},
                                 {"role": "assistant", "content": "edit", "extra": {"actions": [action]}}],
                       full_messages=[], compactions=[], template_vars={"task": "task"}, model_calls=1, tool_calls=1,
                       cost=0., active_seconds=5., consecutive_format_errors=0)
    doc = Checkpoint(schema_version=1, id=uuid.uuid4().hex, binding=binding, phase="awaiting_approval", sequence=2,
                     created_at=time.time(), files={"a.py": "x"}, workspace_sha256=digest({"a.py": "x"}), state=state,
                     pending_action={"action": action, "action_sha256": checksum(action), "reason": "outside",
                                     "targets": [{"path": "/other.py", "allowed": False}], "policy": "auto"}
                     ).model_dump(mode="json")
    ref = {"id": doc["id"], "sha256": checksum(doc), "generation": generation, "sequence": 2,
           "phase": "awaiting_approval", "workspace_sha256": doc["workspace_sha256"]}
    atomic_json(store.folder / f"{doc['id']}.json", doc)
    atomic_json(store.folder / "latest.json", ref)
    return store, doc, ref


def test_awaiting_approval_checkpoint_requires_a_verifiable_pending_action(tmp_path):
    store, doc, ref = pending_action_fixture(tmp_path)
    loaded = store.load(ref, for_resume=True)
    assert loaded.pending_action["action_sha256"] == checksum({"command": "echo changed > other.py"})

    tampered = copy.deepcopy(doc)
    tampered["pending_action"]["action_sha256"] = "0" * 64
    atomic_json(store.folder / f"{doc['id']}.json", tampered)
    with pytest.raises(ValueError, match="verifiable pending action"):
        store.load({**ref, "sha256": checksum(tampered)}, for_resume=True)

    mismatched = copy.deepcopy(doc)
    mismatched["state"]["messages"][-1]["extra"]["actions"] = [{"command": "echo other > other.py"}]
    atomic_json(store.folder / f"{doc['id']}.json", mismatched)
    with pytest.raises(ValueError, match="verifiable pending action"):
        store.load({**ref, "sha256": checksum(mismatched)}, for_resume=True)


def test_automatic_recovery_discards_an_in_flight_marker(tmp_path):
    store, doc, ref = pending_action_fixture(tmp_path)
    store.mark_inflight(2)
    with pytest.raises(ValueError, match="in-flight"):
        store.load(ref, for_resume=True)
    later = CheckpointStore(tmp_path, {**store.binding, "generation": 2}, lambda *_: None)
    # The workspace is rebuilt from the checkpoint, so an unfinished step cannot leak into it.
    assert later.load(ref, for_resume=True, allow_previous_generation=True).id == doc["id"]
    with pytest.raises(ValueError, match="mismatch"):
        later.load(ref)


def test_denied_approval_returns_the_decision_as_an_observation():
    class StubEnv:
        index = 0

        def get_template_vars(self):
            return {}

        def serialize(self):
            return {}

    agent = TracedAgent(DeterministicModel(outputs=[]), StubEnv(), emit=lambda *_: None,
                        cancelled=threading.Event(), system_template="repair", instance_template="{{task}}")
    action = {"command": "echo changed > other.py"}
    agent.pending_action = {"action": action, "action_sha256": checksum(action), "reason": "outside", "targets": []}
    agent.messages = [{"role": "user", "content": "task"},
                      {"role": "assistant", "content": "edit", "extra": {"actions": [action]}}]
    agent.apply_resume({"kind": "denied", "approval_id": "a1", "note": "不要改这个文件", "workspace_sha256": "w"})
    assert agent.pending_action is None
    decision = agent.messages[-1]
    assert decision["role"] == "user" and "rejected this action" in decision["content"]
    assert "不要改这个文件" in decision["content"] and "other.py" in decision["content"]


@pytest.mark.docker
def test_approval_pause_holds_the_action_and_resumes_in_a_new_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "mode": "demo", "task": "edit the file"}
    source = {"a.py": "old"}
    requests = []

    def request(payload):
        requests.append(payload)
        return {"id": f"approval-{len(requests)}"}

    def build(sandbox, generation):
        outputs = [make_output("edit", [{"command": "echo new > a.py"}], cost=0),
                   make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}], cost=0)]
        agent = TracedAgent(DeterministicModel(outputs=outputs), sandbox, emit=lambda *_: None,
                            cancelled=threading.Event(), system_template="repair", instance_template="{{task}}",
                            output_path=tmp_path / run["id"] / "trajectory.json")
        agent.enable_checkpoints({**run, "generation": generation}, source)
        agent.enable_approvals("strict", ["a.py"], request)
        return agent

    first = Sandbox(run["id"], lambda *_: None, threading.Event(), files=source)
    try:
        agent = build(first, 1)
        with pytest.raises(ApprovalPaused):
            agent.run(run["task"])
        reference = agent.checkpoints.latest
        assert reference["phase"] == "awaiting_approval"
        assert reference["action_sha256"] == checksum({"command": "echo new > a.py"})
        assert read_tree(first) == {"a.py": "old"}  # the action is held, not executed
        assert requests[0]["action"] == {"command": "echo new > a.py"}
        assert requests[0]["workspaceSha256"] == reference["workspace_sha256"]
        assert requests[0]["checkpoint"] == {"id": reference["id"], "sequence": reference["sequence"]}
        assert agent.messages[-1]["role"] == "assistant"
    finally:
        first.close()

    checkpoint = load_registered(tmp_path, run["id"], reference)
    second = Sandbox(run["id"], lambda *_: None, threading.Event(), files=checkpoint.files,
                     image=checkpoint.binding["image_id"])
    try:
        resumed = build(second, 2)
        resumed.restore_checkpoint(reference, allow_previous_generation=True,
                                   resume_action={"kind": "execute", "approval_id": "approval-1", "note": None,
                                                  "workspace_sha256": checkpoint.workspace_sha256})
        assert resumed.run(run["task"])["exit_status"] == "Submitted"
        assert read_tree(second) == {"a.py": "new\n"}
        assert resumed.pending_action is None
        assert resumed.checkpoints.binding["generation"] == 2
        assert resumed.checkpoints.latest["generation"] == 2  # new saves belong to the new attempt
        with pytest.raises(ValueError, match="terminal"):
            resumed.checkpoints.load(resumed.checkpoints.latest, for_resume=True)
    finally:
        second.close()
