"""Versioned checkpoints for isolated, quiescent workspaces; scheduling stays in the control API."""

import argparse
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repopilot.repository import digest, validate_files

MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
    if len(data) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Checkpoint exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    messages: list[dict]
    full_messages: list[dict]
    compactions: list[dict]
    template_vars: dict
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    cost: float = Field(ge=0, allow_inf_nan=False)
    active_seconds: float = Field(ge=0, allow_inf_nan=False)
    consecutive_format_errors: int = Field(ge=0)
    model_cursor: int | None = Field(default=None, ge=-1)
    context_state: dict = Field(default_factory=dict)
    review_rounds: int = Field(default=0, ge=0)


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    id: str
    binding: dict
    phase: Literal["ready", "submitted", "stopped", "awaiting_approval"]
    sequence: int = Field(ge=1)
    created_at: float
    workspace_sha256: str
    files: dict[str, str]
    state: AgentState
    pending_action: dict | None = None


def agent_signature(agent):
    from minisweagent.models.test_models import DeterministicModel

    config = agent.config.model_dump(mode="json")
    config.pop("output_path", None)
    model = agent.model.config.model_dump(mode="json")
    if isinstance(agent.model, DeterministicModel):
        for output in model["outputs"]:
            output.get("extra", {}).pop("timestamp", None)
    # Hash model settings without persisting endpoint credentials or deterministic reference actions.
    return checksum({"agent": config, "model": model, "context_mode": agent.context_mode,
                     "context_config": agent.context_manager.config.model_dump() if agent.context_manager else None})


BINDING_KEYS = ("run_id", "task_sha256", "spec_sha256", "source_sha256", "image_id", "agent_signature",
                "mode", "context_mode", "memory_enabled")


def binding_matches(expected, actual, allow_previous_generation=False):
    """Compare a checkpoint binding with the current attempt.

    Cross-generation recovery keeps every binding field except the attempt generation;
    the restored checkpoint must come from an earlier generation than the new attempt.
    """
    if not allow_previous_generation:
        return expected == actual
    if type(actual.get("generation")) is not int or actual["generation"] >= expected.get("generation", 0):
        return False
    return all(expected.get(key) == actual.get(key) for key in BINDING_KEYS)


class CheckpointStore:
    def __init__(self, root, binding, emit):
        self.root, self.binding, self.emit = Path(root), binding, emit
        self.run_id = str(uuid.UUID(binding["run_id"]))
        if type(binding["generation"]) is not int or binding["generation"] < 1:
            raise ValueError("Checkpoint needs a claimed generation")
        self.sequence = 0
        self.latest = None

    def folder_for(self, generation):
        if type(generation) is not int or generation < 1:
            raise ValueError("Invalid checkpoint generation")
        return self.root / self.run_id / "checkpoints" / f"g{generation}"

    @property
    def folder(self):
        return self.folder_for(self.binding["generation"])

    @classmethod
    def binding_for(cls, agent, run, source):
        return {
            "run_id": run["id"], "generation": run["generation"], "task_sha256": checksum(run["task"]),
            "spec_sha256": checksum(run.get("spec")), "source_sha256": digest(validate_files(source)),
            "image_id": agent.env.container.image.id, "agent_signature": agent_signature(agent),
            "mode": run["mode"], "context_mode": agent.context_mode,
            "memory_enabled": run.get("memoryEnabled", False),
        }

    @classmethod
    def for_agent(cls, agent, run, source):
        return cls(os.getenv("ARTIFACT_ROOT", "runtime/artifacts"), cls.binding_for(agent, run, source), agent.emit)

    def mark_inflight(self, call):
        atomic_json(self.folder / "inflight.json", {
            "call": call, "kind": "model_or_tool_step", "started_at": time.time(),
            "generation": self.binding["generation"],
        })

    def save(self, agent, phase, pending_action=None):
        from minisweagent.models.test_models import DeterministicModel

        from repopilot.repository_runtime import read_tree

        files = read_tree(agent.env, quiescent=True)
        state = AgentState(
            messages=agent.messages, full_messages=agent.full_messages, compactions=agent.compactions,
            template_vars=agent.extra_template_vars, model_calls=agent.n_calls, tool_calls=agent.env.index,
            cost=float(agent.cost), active_seconds=max(0., time.time() - agent._start_time),
            consecutive_format_errors=agent.n_consecutive_format_errors,
            model_cursor=agent.model.current_index if isinstance(agent.model, DeterministicModel) else None,
            context_state=agent.context_manager.state if agent.context_manager else {},
            review_rounds=agent.review_rounds,
        )
        checkpoint = Checkpoint(
            schema_version=1, id=uuid.uuid4().hex, binding=self.binding, phase=phase,
            sequence=self.sequence + 1, created_at=time.time(), workspace_sha256=digest(files), files=files,
            state=state, pending_action=pending_action,
        )
        payload = checkpoint.model_dump(mode="json")
        reference = {
            "id": checkpoint.id, "sha256": checksum(payload), "generation": self.binding["generation"],
            "sequence": checkpoint.sequence, "phase": phase, "workspace_sha256": checkpoint.workspace_sha256,
            "model_calls": agent.n_calls, "tool_calls": agent.env.index, "active_seconds": state.active_seconds,
            "remaining_steps": max(0, agent.config.step_limit - agent.n_calls) if agent.config.step_limit else None,
            "remaining_seconds": max(0., agent.config.wall_time_limit_seconds - state.active_seconds)
            if agent.config.wall_time_limit_seconds else None,
            "file_count": len(files), "image_id": self.binding["image_id"],
            "action_sha256": pending_action["action_sha256"] if pending_action else None,
        }
        atomic_json(self.folder / f"{checkpoint.id}.json", payload)
        # Registration uses the existing authenticated generation/lease check. Unregistered files are not published.
        self.emit("CHECKPOINT_SAVED", reference)
        atomic_json(self.folder / "latest.json", reference)
        (self.folder / "inflight.json").unlink(missing_ok=True)
        self.sequence, self.latest = checkpoint.sequence, reference
        return reference

    def load(self, reference, *, for_resume=False, allow_previous_generation=False):
        if not re.fullmatch(r"[0-9a-f]{32}", reference.get("id", "")):
            raise ValueError("Invalid checkpoint ID")
        folder = self.folder_for(reference.get("generation"))
        path = folder / f"{reference['id']}.json"
        if path.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise ValueError("Checkpoint exceeds size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if checksum(payload) != reference.get("sha256"):
            raise ValueError("Checkpoint checksum mismatch")
        checkpoint = Checkpoint.model_validate(payload)
        if not binding_matches(self.binding, checkpoint.binding, allow_previous_generation) or checkpoint.id != reference["id"]:
            raise ValueError("Checkpoint task, generation, source, image or configuration mismatch")
        if digest(validate_files(checkpoint.files)) != checkpoint.workspace_sha256:
            raise ValueError("Checkpoint workspace checksum mismatch")
        if for_resume:
            if checkpoint.phase not in ("ready", "awaiting_approval"):
                raise ValueError("Checkpoint is terminal or has an unresolved in-flight step")
            if checkpoint.phase == "awaiting_approval":
                pending = checkpoint.pending_action or {}
                message = checkpoint.state.messages[-1] if checkpoint.state.messages else {}
                if (not pending or checksum(pending.get("action")) != pending.get("action_sha256")
                        or message.get("role") != "assistant"
                        or message.get("extra", {}).get("actions") != [pending.get("action")]):
                    raise ValueError("Checkpoint does not carry a verifiable pending action")
            elif not checkpoint.state.messages or checkpoint.state.messages[-1].get("role") != "user":
                raise ValueError("Checkpoint is not at a completed observation boundary")
            # A checkpoint is only written at a completed boundary, so a later automatic recovery may
            # discard the in-flight marker: the workspace is rebuilt from the checkpoint, not reused.
            if (folder / "inflight.json").exists() and not allow_previous_generation:
                raise ValueError("Checkpoint is terminal or has an unresolved in-flight step")
            if not allow_previous_generation:
                latest = json.loads((folder / "latest.json").read_text(encoding="utf-8"))
                if latest != reference:
                    raise ValueError("Older checkpoints cannot reset consumed budgets")
        return checkpoint


def load_registered(root, run_id, reference):
    """Read the file named by a control-plane registration; validates integrity, not resumability."""
    if not re.fullmatch(r"[0-9a-f]{32}", reference.get("id", "")):
        raise ValueError("Invalid checkpoint ID")
    generation = reference.get("generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("Invalid checkpoint generation")
    path = Path(root) / str(uuid.UUID(run_id)) / "checkpoints" / f"g{generation}" / f"{reference['id']}.json"
    if not path.is_file():
        raise ValueError("Checkpoint file is missing for the registered reference")
    if path.stat().st_size > MAX_CHECKPOINT_BYTES:
        raise ValueError("Checkpoint exceeds size limit")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if checksum(payload) != reference.get("sha256"):
        raise ValueError("Checkpoint checksum mismatch")
    checkpoint = Checkpoint.model_validate(payload)
    if (checkpoint.id != reference["id"] or checkpoint.binding["run_id"] != str(uuid.UUID(run_id))
            or checkpoint.binding["generation"] != generation):
        raise ValueError("Checkpoint run or generation mismatch")
    if digest(validate_files(checkpoint.files)) != checkpoint.workspace_sha256:
        raise ValueError("Checkpoint workspace checksum mismatch")
    return checkpoint


def restore_check(run_id, generation, checkpoint_id=None):
    """Verify restoration in a fresh sandbox; never resume or change a business Run."""
    from repopilot.repository_runtime import read_tree
    from repopilot.runtime import Sandbox

    root = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts"))
    run_id = str(uuid.UUID(run_id))
    if generation < 1:
        raise ValueError("Invalid checkpoint generation")
    if checkpoint_id and not re.fullmatch(r"[0-9a-f]{32}", checkpoint_id):
        raise ValueError("Invalid checkpoint ID")
    # Always verify against the control plane, including when selecting the latest checkpoint.
    # An orphaned file or local pointer alone is not evidence of successful registration.
    import httpx
    refs = httpx.get(f"{os.getenv('CONTROL_API_URL', 'http://localhost:3101')}/runs/{run_id}/checkpoints",
                     timeout=15).raise_for_status().json()
    matches = [r["data"] for r in refs if r["data"]["generation"] == generation
               and (checkpoint_id is None or r["data"]["id"] == checkpoint_id)]
    if not matches:
        raise ValueError("No registered checkpoint matches the requested run and generation")
    reference = max(matches, key=lambda item: item["sequence"])
    checkpoint = load_registered(root, run_id, reference)
    sandbox = Sandbox(f"checkpoint-check-{uuid.uuid4()}", lambda *_: None, threading.Event(),
                      files=checkpoint.files, image=checkpoint.binding["image_id"])
    try:
        if sandbox.container.image.id != checkpoint.binding["image_id"] or read_tree(sandbox) != checkpoint.files:
            raise ValueError("Restored workspace differs from checkpoint")
    finally:
        sandbox.close()
    return {"run_id": run_id, "checkpoint_id": checkpoint.id, "restored": True, "phase": checkpoint.phase,
            "pending_action": bool(checkpoint.pending_action), "model_calls": checkpoint.state.model_calls,
            "workspace_sha256": checkpoint.workspace_sha256}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore a checkpoint into a disposable sandbox without model calls")
    parser.add_argument("run_id")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--checkpoint-id")
    args = parser.parse_args()
    print(json.dumps(restore_check(args.run_id, args.generation, args.checkpoint_id)))
