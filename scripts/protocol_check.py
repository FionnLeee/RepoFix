"""Run with worker containers stopped; uses only synthetic control-plane tasks."""

import json
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
config = dotenv_values(root / ".env")
client = httpx.Client(base_url="http://localhost:3101", timeout=10)
headers = {"authorization": f"Bearer {config['WORKER_TOKEN']}"}
checks = []


def create():
    response = client.post("/runs", json={"mode": "demo", "requestKey": f"protocol-{uuid.uuid4()}"})
    response.raise_for_status()
    return response.json()["id"]


cancelled = create()
response = client.post(f"/runs/{cancelled}/cancel", json={})
assert response.json()["status"] == "CANCELLED"
assert (
    client.post(f"/internal/runs/{cancelled}/claim", json={"workerId": "protocol-test"}, headers=headers).status_code
    == 409
)
checks.append("queued_cancel_prevents_claim")

owned = create()
claim = client.post(f"/internal/runs/{owned}/claim", json={"workerId": "protocol-test"}, headers=headers)
claim.raise_for_status()
generation = claim.json()["generation"]
assert client.post(f"/internal/runs/{owned}/claim", json={"workerId": "duplicate"}, headers=headers).status_code == 409
checks.append("duplicate_claim_rejected")
body = {
    "workerId": "protocol-test",
    "generation": generation,
    "key": "stable-step",
    "type": "PROTOCOL_CHECK",
    "data": {"purpose": "synthetic protocol verification"},
}
assert (
    client.post(
        f"/internal/runs/{owned}/step", json={**body, "generation": generation + 1}, headers=headers
    ).status_code
    == 409
)
checks.append("stale_generation_rejected")
for _ in range(2):
    assert client.post(f"/internal/runs/{owned}/step", json=body, headers=headers).status_code == 201
detail = client.get(f"/runs/{owned}").json()
assert sum(e["key"] == "stable-step" for e in detail["events"]) == 1
checks.append("duplicate_step_persisted_once")
cursor = detail["events"][-2]["id"]
with client.stream("GET", f"/runs/{owned}/stream", headers={"last-event-id": str(cursor)}) as stream:
    assert stream.status_code == 200
    for line in stream.iter_lines():
        if line.startswith("id: "):
            assert int(line[4:]) > cursor
            break
checks.append("sse_reconnect_resumes_after_cursor")
checkpoint = {"id": uuid.uuid4().hex, "sha256": "a" * 64, "workspace_sha256": "b" * 64,
              "generation": generation, "sequence": 1, "phase": "ready"}
cp_body = {**body, "key": "checkpoint-1", "type": "CHECKPOINT_SAVED", "data": checkpoint}
for invalid, expected in [({"generation": generation + 1}, 400), ({"id": "../bad"}, 400),
                          ({"sequence": 2}, 409), ({"phase": "inflight"}, 400)]:
    response = client.post(f"/internal/runs/{owned}/step",
                           json={**cp_body, "data": {**checkpoint, **invalid}}, headers=headers)
    assert response.status_code == expected, response.text
checks.append("invalid_checkpoint_metadata_and_sequence_rejected")
for _ in range(2):
    client.post(f"/internal/runs/{owned}/step", json=cp_body, headers=headers).raise_for_status()
assert client.post(f"/internal/runs/{owned}/step", json={**cp_body, "key": "repeated-sequence"},
                   headers=headers).status_code == 409
registered = client.get(f"/runs/{owned}/checkpoints").raise_for_status().json()
assert len(registered) == 1 and registered[0]["data"] == checkpoint
checks.append("checkpoint_registration_idempotent_and_monotonic")
client.post(f"/runs/{owned}/cancel", json={}).raise_for_status()
client.post(
    f"/internal/runs/{owned}/step",
    json={
        **body,
        "key": "finish",
        "type": "CANCELLED",
        "status": "CANCELLED",
        "data": {"reason": "synthetic protocol check completed"},
    },
    headers=headers,
).raise_for_status()

expired = create()
client.post(
    f"/internal/runs/{expired}/claim", json={"workerId": "lost-worker-test"}, headers=headers
).raise_for_status()
# A cancelled run is stopped by the worker holding it; when that worker is lost instead, the
# coordinator has to finish the cancellation the user asked for.
abandoned = create()
client.post(
    f"/internal/runs/{abandoned}/claim", json={"workerId": "lost-worker-test"}, headers=headers
).raise_for_status()
assert client.post(f"/runs/{abandoned}/cancel", json={}).json()["status"] == "RUNNING"
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    detail = client.get(f"/runs/{expired}").json()
    abandoned_detail = client.get(f"/runs/{abandoned}").json()
    if detail["status"] == "INTERRUPTED" and abandoned_detail["status"] == "CANCELLED":
        break
    time.sleep(2)
assert detail["status"] == "INTERRUPTED"
assert any(e["type"] == "INTERRUPTED" and "没有已登记的可恢复检查点" in e["data"]["reason"]
           for e in detail["events"])
checks.append("expired_lease_without_checkpoint_marked_interrupted")
assert abandoned_detail["status"] == "CANCELLED" and abandoned_detail["cancelRequested"] is True
assert any(e["type"] == "CANCELLED" and "已请求取消" in e["data"]["reason"] for e in abandoned_detail["events"])
checks.append("cancelled_run_whose_worker_is_lost_still_ends_as_cancelled")

# M3: version-bound approvals, their decision, and the resume plan a new generation receives.
paused_run = create()
claim = client.post(f"/internal/runs/{paused_run}/claim", json={"workerId": "protocol-test"},
                    headers=headers).raise_for_status().json()
generation = claim["generation"]
plan = client.post(f"/internal/runs/{paused_run}/resume-plan",
                   json={"workerId": "protocol-test", "generation": generation},
                   headers=headers).raise_for_status().json()
assert plan == {"mode": "fresh"}
assert client.post(f"/internal/runs/{paused_run}/resume-plan",
                   json={"workerId": "someone-else", "generation": generation}, headers=headers).status_code == 409
checks.append("resume_plan_is_fresh_without_a_checkpoint_and_checks_ownership")

checkpoint = {"id": uuid.uuid4().hex, "sha256": "a" * 64, "workspace_sha256": "b" * 64,
              "generation": generation, "sequence": 1, "phase": "awaiting_approval", "action_sha256": "c" * 64}
client.post(f"/internal/runs/{paused_run}/step",
            json={"workerId": "protocol-test", "generation": generation, "key": "paused-checkpoint",
                  "type": "CHECKPOINT_SAVED", "data": checkpoint}, headers=headers).raise_for_status()

# The worker that owned this attempt is gone; the lease expires and the coordinator requeues it.
deadline = time.monotonic() + 90
while True:
    detail = client.get(f"/runs/{paused_run}").json()
    requeued = next((e for e in detail["events"] if e["type"] == "REQUEUED"), None)
    if requeued or time.monotonic() > deadline:
        break
    time.sleep(2)
assert requeued, "a paused checkpoint must be recoverable"
assert requeued["data"]["checkpoint_id"] == checkpoint["id"]
assert detail["status"] == "QUEUED" and detail["recoveryAttempts"] == 1
checks.append("expired_lease_requeued_from_a_registered_checkpoint")

attempt = client.post(f"/internal/runs/{paused_run}/claim", json={"workerId": "protocol-test"},
                      headers=headers).raise_for_status().json()
assert attempt["generation"] == generation + 1
plan = client.post(f"/internal/runs/{paused_run}/resume-plan",
                   json={"workerId": "protocol-test", "generation": attempt["generation"]},
                   headers=headers).raise_for_status().json()
assert plan["mode"] == "wait" and plan["approval"] is None and plan["checkpoint"]["id"] == checkpoint["id"]
checks.append("resume_plan_waits_for_a_decision_that_was_never_recorded")

request = {"workerId": "protocol-test", "generation": attempt["generation"],
           "checkpoint": {"id": checkpoint["id"], "sequence": 1}, "workspaceSha256": "b" * 64,
           "action": {"command": "echo changed > conftest.py"}, "actionSha256": "c" * 64,
           "reason": "该动作写入允许范围之外的位置（/conftest.py）。",
           "targets": [{"path": "/conftest.py", "allowed": False}], "policy": "auto"}
assert client.post(f"/internal/runs/{paused_run}/approvals",
                   json={**request, "workspaceSha256": "d" * 64}, headers=headers).status_code == 400
assert client.post(f"/internal/runs/{paused_run}/approvals",
                   json={**request, "actionSha256": "e" * 64}, headers=headers).status_code == 400
checks.append("approval_must_match_the_registered_checkpoint")

approval_id = client.post(f"/internal/runs/{paused_run}/approvals",
                          json=request, headers=headers).raise_for_status().json()["id"]
detail = client.get(f"/runs/{paused_run}").json()
assert detail["status"] == "WAITING_APPROVAL" and len(detail["approvals"]) == 1
assert detail["approvals"][0]["workspaceSha256"] == "b" * 64
assert detail["approvals"][0]["checkpointSequence"] == 1
assert client.post(f"/internal/runs/{paused_run}/approvals", json=request, headers=headers).json()["id"] == approval_id
checks.append("approval_request_pauses_the_run_and_is_idempotent")

assert client.post(f"/internal/runs/{paused_run}/resume-plan",
                   json={"workerId": "protocol-test", "generation": attempt["generation"]},
                   headers=headers).status_code == 409
client.post(f"/runs/{paused_run}/approvals/{approval_id}/decide",
            json={"decision": "approve", "note": "协议检查"}).raise_for_status()
assert client.post(f"/runs/{paused_run}/approvals/{approval_id}/decide",
                   json={"decision": "reject"}).status_code == 409
detail = client.get(f"/runs/{paused_run}").json()
assert detail["status"] == "QUEUED" and detail["approvals"][0]["status"] == "APPROVED"
assert detail["approvals"][0]["note"] == "协议检查"
checks.append("approval_decision_is_final_and_requeues_the_run")

resumed = client.post(f"/internal/runs/{paused_run}/claim", json={"workerId": "protocol-test"},
                      headers=headers).raise_for_status().json()
assert resumed["generation"] == generation + 2
plan = client.post(f"/internal/runs/{paused_run}/resume-plan",
                   json={"workerId": "protocol-test", "generation": resumed["generation"]},
                   headers=headers).raise_for_status().json()
assert plan["mode"] == "restore" and plan["checkpoint"]["id"] == checkpoint["id"]
assert plan["approval"]["status"] == "APPROVED" and plan["approval"]["actionSha256"] == "c" * 64
checks.append("resume_plan_returns_the_decided_checkpoint_to_the_new_generation")

detail = client.post(f"/runs/{paused_run}/cancel", json={}).raise_for_status().json()
assert detail["cancelRequested"] is True

invalidated = create()
claim = client.post(f"/internal/runs/{invalidated}/claim", json={"workerId": "protocol-test"},
                    headers=headers).raise_for_status().json()
paused = {**checkpoint, "id": uuid.uuid4().hex, "generation": claim["generation"]}
client.post(f"/internal/runs/{invalidated}/step",
            json={"workerId": "protocol-test", "generation": claim["generation"], "key": "paused-checkpoint",
                  "type": "CHECKPOINT_SAVED", "data": paused}, headers=headers).raise_for_status()
pending = client.post(f"/internal/runs/{invalidated}/approvals",
                      json={**request, "generation": claim["generation"],
                            "checkpoint": {"id": paused["id"], "sequence": 1}},
                      headers=headers).raise_for_status().json()
client.post(f"/runs/{invalidated}/cancel", json={}).raise_for_status()
detail = client.get(f"/runs/{invalidated}").json()
assert detail["status"] == "CANCELLED" and detail["approvals"][0]["status"] == "INVALIDATED"
assert client.post(f"/runs/{invalidated}/approvals/{pending['id']}/decide",
                   json={"decision": "approve"}).status_code == 409
checks.append("cancel_invalidates_a_pending_approval")

report = {
    "checks": checks,
    "run_ids": [cancelled, owned, expired, abandoned, paused_run, invalidated],
    "note": "Synthetic control-plane verification, no model calls.",
}
path = root / "runtime" / "validation" / "protocol.json"
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
client.close()
