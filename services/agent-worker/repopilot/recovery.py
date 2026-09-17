"""Cross-generation recovery: resume a lost attempt from its last registered checkpoint.

Only the control plane decides *which* checkpoint a new attempt may resume from; this
module then re-validates the file against the pinned task, source, image and agent
configuration before anything is restored. A resumed attempt rebuilds its workspace from
the checkpoint instead of reusing a container, so effects of a step that was in flight
when the previous worker died are discarded rather than half-applied.
"""

import os
from pathlib import Path

from repopilot.checkpoint import checksum, load_registered
from repopilot.repository import digest, validate_files

PRE_AGENT_KEYS = ("run_id", "task_sha256", "spec_sha256", "source_sha256", "mode", "context_mode", "memory_enabled")


class RecoveryError(RuntimeError):
    pass


def artifact_root():
    return Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts"))


def load_resume(run, reference, source):
    """Validate a registered checkpoint against everything known before an agent exists."""
    checkpoint = load_registered(artifact_root(), run["id"], reference)
    expected = {
        "run_id": run["id"], "task_sha256": checksum(run["task"]), "spec_sha256": checksum(run.get("spec")),
        "source_sha256": digest(validate_files(source)), "mode": run["mode"],
        "context_mode": run.get("contextMode", "full"), "memory_enabled": run.get("memoryEnabled", False),
    }
    if any(checkpoint.binding.get(key) != value for key, value in expected.items()):
        raise RecoveryError("Registered checkpoint does not match this task, source or configuration")
    if type(checkpoint.binding.get("generation")) is not int or checkpoint.binding["generation"] >= run["generation"]:
        raise RecoveryError("Registered checkpoint is not from an earlier attempt")
    if checkpoint.phase not in ("ready", "awaiting_approval"):
        raise RecoveryError("Registered checkpoint is not resumable")
    return checkpoint


def preserve_trajectory(folder, generation):
    """Keep the interrupted attempt's trajectory next to the resumed one."""
    trajectory = Path(folder) / "trajectory.json"
    if trajectory.is_file():
        target = Path(folder) / f"trajectory-before-recovery-g{generation}.json"
        if not target.exists():
            target.write_bytes(trajectory.read_bytes())


def resume_action(plan, checkpoint):
    """Turn an approval decision into the action the restored agent applies first."""
    approval = plan.get("approval")
    if not approval:
        return None
    pending = checkpoint.pending_action or {}
    if approval["actionSha256"] != pending.get("action_sha256"):
        raise RecoveryError("Approval decision does not match the paused action")
    if approval["workspaceSha256"] != checkpoint.workspace_sha256:
        raise RecoveryError("Approval was bound to a different workspace version")
    return {"kind": "execute" if approval["status"] == "APPROVED" else "denied", "approval_id": approval["id"],
            "note": approval.get("note"), "workspace_sha256": approval["workspaceSha256"]}


def approval_request(run, checkpoint):
    """Rebuild the approval request when a checkpoint was registered but the request was lost."""
    pending = checkpoint.pending_action or {}
    if not pending:
        raise RecoveryError("Checkpoint has no pending action to request approval for")
    return {"generation": run["generation"], "checkpoint": {"id": checkpoint.id, "sequence": checkpoint.sequence},
            "workspaceSha256": checkpoint.workspace_sha256, "action": pending["action"],
            "actionSha256": pending["action_sha256"], "reason": pending.get("reason", ""),
            "targets": pending.get("targets", []), "policy": pending.get("policy", "auto")}
