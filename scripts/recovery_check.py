"""Deterministic M3 recovery fault injection against the deployed stack.

Fault: a live attempt's lease is expired in the database while its worker is still executing,
which is the observable state of a worker that died mid-step. The coordinator must requeue the
run, a new generation must resume from the last registered checkpoint, and the container the
lost attempt left behind must be reclaimed. No model calls are made.
"""

import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
client = httpx.Client(base_url="http://localhost:3101", timeout=20)
worker_headers = {"authorization": f"Bearer {dotenv_values(root / '.env')['WORKER_TOKEN']}"}
terminal = ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED")


def psql(sql):
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "repopilot", "-d", "repopilot", "-tAc", sql],
        cwd=root, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()


def sandboxes():
    return subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=repopilot.managed=sandbox",
         "--format", "{{.ID}} {{.Label \"repopilot.run\"}}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip().splitlines()


run = client.post("/runs", json={"requestKey": f"m3-recovery-{uuid.uuid4()}", "mode": "demo",
                                 "baselineId": "checkout", "contextMode": "managed",
                                 "memoryEnabled": False}).raise_for_status().json()
run_id = run["id"]

deadline = time.monotonic() + 120
detail, checkpoint = None, None
while time.monotonic() < deadline:
    detail = client.get(f"/runs/{run_id}").raise_for_status().json()
    registered = [e["data"] for e in detail["events"] if e["type"] == "CHECKPOINT_SAVED"]
    if detail["status"] == "RUNNING" and detail["workerId"] and registered:
        checkpoint = registered[-1]
        break
    assert detail["status"] not in terminal, f"run finished before the fault: {detail['status']}"
    time.sleep(0.5)
assert checkpoint, "no registered checkpoint was available before the fault"
lost_worker, lost_generation = detail["workerId"], detail["generation"]

# The worker stays alive and keeps executing; only its lease disappears, exactly as after a crash.
psql(f"UPDATE \"Run\" SET \"leaseUntil\" = now() - interval '2 minutes' WHERE id = '{run_id}'")
injected = time.monotonic()

requeued = None
while time.monotonic() - injected < 150:
    detail = client.get(f"/runs/{run_id}").raise_for_status().json()
    requeued = next((e for e in detail["events"] if e["type"] == "REQUEUED"), None)
    if requeued:
        requeued_at = time.monotonic()
        break
    assert detail["status"] not in terminal, f"run ended without automatic recovery: {detail['status']}"
    time.sleep(1)
assert requeued, "the coordinator did not requeue the run after its lease expired"
assert requeued["data"]["from_generation"] == lost_generation
assert requeued["data"]["checkpoint_id"] == checkpoint["id"] or requeued["data"]["phase"] == "ready"

deadline = time.monotonic() + 240
while time.monotonic() < deadline:
    detail = client.get(f"/runs/{run_id}").raise_for_status().json()
    if detail["status"] in terminal:
        break
    time.sleep(1)
assert detail["status"] == "SUCCEEDED", detail.get("result")
events = detail["events"]
recovered = [e["data"] for e in events if e["type"] == "RECOVERED"]
assert len(recovered) == 1 and recovered[0]["from_generation"] == lost_generation
assert recovered[0]["generation"] == lost_generation + 1 == detail["generation"]
assert recovered[0]["checkpoint_id"] == requeued["data"]["checkpoint_id"]
registered = {e["data"]["id"] for e in events
              if e["type"] == "CHECKPOINT_SAVED" and e["data"]["generation"] == lost_generation}
assert recovered[0]["checkpoint_id"] in registered
assert detail["recoveryAttempts"] == 1
assert len([e for e in events if e["type"] == "RUNNING"]) == 2, "each attempt claims once"
assert not [e for e in events if e["type"] in ("FAILED", "INTERRUPTED", "CANCELLED")]
assert detail["result"]["verification"]["passed"] is True
assert detail["result"]["provenance"]["recovered_from"]["generation"] == lost_generation

# The lost attempt released its own sandbox when it stopped; nothing may be left running.
time.sleep(2)
leftovers = [line for line in sandboxes() if run_id in line]
assert not leftovers, leftovers

# A container whose owning process died outright is reclaimed by the worker's sweeper.
orphan_run = str(uuid.uuid4())
subprocess.run(["docker", "run", "-d", "--label", "repopilot.managed=sandbox",
                "--label", f"repopilot.run={orphan_run}",
                "--label", f"repopilot.created={time.time() - 600}",
                "python:3.12-slim", "sleep", "600"],
               capture_output=True, text=True, timeout=180, check=True)
started, reclaimed = time.monotonic(), None
while time.monotonic() - started < 180:
    if not [line for line in sandboxes() if orphan_run in line]:
        reclaimed = round(time.monotonic() - started, 1)
        break
    time.sleep(3)
assert reclaimed is not None, "the worker sweeper never reclaimed an unowned sandbox"

report = {
    "checks": ["lost_lease_requeued_without_losing_the_run", "new_generation_resumed_from_the_registered_checkpoint",
               "replayed_attempt_did_not_write_terminal_state", "no_container_survived_the_interrupted_attempt",
               "acceptance_passed_after_recovery", "worker_sweeper_reclaimed_an_unowned_sandbox"],
    "run_id": run_id, "lost_worker": lost_worker, "lost_generation": lost_generation,
    "final_generation": detail["generation"], "resumed_checkpoint_id": recovered[0]["checkpoint_id"],
    "requeue_seconds": round(requeued_at - injected, 1),
    "orphan_reclaim_seconds": reclaimed, "generative_model_calls": 0,
}
target = root / "runtime" / "validation" / "m3-recovery.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
client.close()
