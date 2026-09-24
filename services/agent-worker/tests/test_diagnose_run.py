import base64
import json
import uuid

from repopilot.checkpoint import AgentState, Checkpoint, checksum
from repopilot.diagnostics import diagnose
from repopilot.repository import digest


def test_cancelled_run_without_checkpoint_has_no_candidate(tmp_path):
    run_id = str(uuid.uuid4())
    folder = tmp_path / run_id
    folder.mkdir()
    (folder / "result.json").write_text(json.dumps({"stop_reason": "Cancelled", "model_calls": 0}))
    summary, patch = diagnose(run_id, tmp_path)
    assert summary["stop_reason"] == "Cancelled"
    assert summary["first_observed_delta"] is None
    assert summary["formal_candidate_present"] is False
    assert patch is None


def test_latest_reference_must_match_before_process_patch_is_exported(tmp_path):
    run_id = str(uuid.uuid4())
    folder = tmp_path / run_id
    checkpoints = folder / "checkpoints" / "g1"
    checkpoints.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({"stop_reason": "LimitsExceeded", "model_calls": 3}))
    files = {"image-base": "a" * 40, "delta/000000": base64.b64encode(b"diff --git a/x b/x\n").decode()}
    checkpoint = Checkpoint(schema_version=1, id=uuid.uuid4().hex,
                            binding={"run_id": run_id, "generation": 1}, phase="stopped", sequence=1,
                            created_at=1.0, workspace_sha256=digest(files), files=files,
                            state=AgentState(messages=[], full_messages=[], compactions=[], template_vars={},
                                             model_calls=3, tool_calls=2, cost=0, active_seconds=1,
                                             consecutive_format_errors=0))
    payload = checkpoint.model_dump()
    reference = {"id": checkpoint.id, "generation": 1, "sha256": checksum(payload)}
    path = checkpoints / f"{checkpoint.id}.json"
    path.write_text(json.dumps(payload))
    (checkpoints / "latest.json").write_text(json.dumps(reference))
    summary, patch = diagnose(run_id, tmp_path)
    assert patch == b"diff --git a/x b/x\n"
    assert summary["formal_candidate_present"] is False
    assert summary["diagnostic_delta"]["status"] == "process_snapshot_unsubmitted_unverified"
    payload["state"]["model_calls"] = 4
    path.write_text(json.dumps(payload))
    summary, patch = diagnose(run_id, tmp_path)
    assert patch is None
    assert summary["valid_checkpoints"] == 0
    assert summary["invalid_checkpoint_files"]
