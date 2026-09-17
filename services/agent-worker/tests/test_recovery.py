import time
import uuid

import pytest
from repopilot.checkpoint import AgentState, Checkpoint, atomic_json, checksum
from repopilot.recovery import RecoveryError, approval_request, load_resume, resume_action
from repopilot.repository import digest


def write_checkpoint(tmp_path, run, source, *, generation=1, phase="ready", pending=None, files=None):
    files = files if files is not None else source
    binding = {"run_id": run["id"], "generation": generation, "task_sha256": checksum(run["task"]),
               "spec_sha256": checksum(run.get("spec")), "source_sha256": digest(source), "mode": run["mode"],
               "context_mode": run.get("contextMode", "full"), "memory_enabled": run.get("memoryEnabled", False),
               "image_id": "sha256:image", "agent_signature": "signature"}
    messages = [{"role": "user", "content": "task"}]
    if pending:
        messages.append({"role": "assistant", "content": "edit", "extra": {"actions": [pending["action"]]}})
    state = AgentState(messages=messages, full_messages=[], compactions=[], template_vars={}, model_calls=1,
                       tool_calls=1, cost=0., active_seconds=1., consecutive_format_errors=0)
    doc = Checkpoint(schema_version=1, id=uuid.uuid4().hex, binding=binding, phase=phase, sequence=1,
                     created_at=time.time(), files=files, workspace_sha256=digest(files), state=state,
                     pending_action=pending).model_dump(mode="json")
    atomic_json(tmp_path / run["id"] / "checkpoints" / f"g{generation}" / f"{doc['id']}.json", doc)
    return {"id": doc["id"], "sha256": checksum(doc), "generation": generation, "sequence": 1, "phase": phase,
            "workspace_sha256": doc["workspace_sha256"]}


def pending(action):
    return {"action": action, "action_sha256": checksum(action), "reason": "outside", "policy": "auto", "targets": []}


def run_fixture(generation=2):
    return {"id": str(uuid.uuid4()), "generation": generation, "mode": "demo", "task": "fix the bug",
            "contextMode": "full", "memoryEnabled": False}


def test_load_resume_validates_the_pinned_task_source_and_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run, source = run_fixture(), {"a.py": "old"}
    reference = write_checkpoint(tmp_path, run, source)
    assert load_resume(run, reference, source).binding["generation"] == 1
    with pytest.raises(RecoveryError, match="does not match"):
        load_resume({**run, "task": "another task"}, reference, source)
    with pytest.raises(RecoveryError, match="does not match"):
        load_resume(run, reference, {"a.py": "changed"})
    with pytest.raises(RecoveryError, match="earlier attempt"):
        load_resume({**run, "generation": 1}, reference, source)
    stopped = write_checkpoint(tmp_path, run, source, phase="stopped")
    with pytest.raises(RecoveryError, match="not resumable"):
        load_resume(run, stopped, source)


def test_approval_decision_must_match_the_paused_action_and_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run, source = run_fixture(), {"a.py": "old"}
    action = {"command": "echo changed > other.py"}
    reference = write_checkpoint(tmp_path, run, source, phase="awaiting_approval", pending=pending(action))
    checkpoint = load_resume(run, reference, source)
    assert approval_request(run, checkpoint) == {
        "generation": 2, "checkpoint": {"id": reference["id"], "sequence": 1},
        "workspaceSha256": reference["workspace_sha256"], "action": action,
        "actionSha256": checksum(action), "reason": "outside", "targets": [], "policy": "auto"}
    decision = {"id": "approval-1", "status": "APPROVED", "note": "ok",
                "actionSha256": checksum(action), "workspaceSha256": checkpoint.workspace_sha256}
    assert resume_action({"approval": decision}, checkpoint)["kind"] == "execute"
    assert resume_action({"approval": {**decision, "status": "REJECTED"}}, checkpoint)["kind"] == "denied"
    assert resume_action({"approval": None}, checkpoint) is None
    with pytest.raises(RecoveryError, match="paused action"):
        resume_action({"approval": {**decision, "actionSha256": "0" * 64}}, checkpoint)
    with pytest.raises(RecoveryError, match="workspace version"):
        resume_action({"approval": {**decision, "workspaceSha256": "f" * 64}}, checkpoint)
