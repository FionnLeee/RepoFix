"""Run with worker containers stopped; uses only synthetic control-plane tasks."""

import hashlib
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

# M4: the platform's own review entry, separate from the coder asking for one at delivery.
def create_repository(payload=None):
    response = client.post("/runs", json={"mode": "demo", "baselineId": "checkout",
                                          "requestKey": f"protocol-{uuid.uuid4()}", **(payload or {})})
    response.raise_for_status()
    return response.json()


assert client.post("/runs", json={"mode": "demo", "baselineId": "checkout", "reviewPolicy": "sometimes",
                                  "requestKey": f"protocol-{uuid.uuid4()}"}).status_code == 400
reviewable = create_repository({"reviewPolicy": "off"})
assert reviewable["reviewPolicy"] == "off"
assert client.post(f"/runs/{reviewable['id']}/review-request", json={}).raise_for_status().json()["reviewPolicy"] == "auto"
detail = client.get(f"/runs/{reviewable['id']}").json()
assert any(e["type"] == "REVIEW_ENABLED" for e in detail["events"])
assert client.post(f"/runs/{reviewable['id']}/review-request", json={}).raise_for_status().json()["reviewPolicy"] == "auto"
checks.append("review_can_be_turned_on_while_the_run_is_queued")
# An attempt reads its configuration when it is claimed, so asking later cannot change it.
too_late = create_repository({"reviewPolicy": "off"})
client.post(f"/internal/runs/{too_late['id']}/claim", json={"workerId": "protocol-test"},
            headers=headers).raise_for_status()
assert client.post(f"/runs/{too_late['id']}/review-request", json={}).status_code == 409
checks.append("review_request_is_rejected_once_an_attempt_has_claimed_the_run")
single_file = create()
assert client.post(f"/runs/{single_file}/review-request", json={}).status_code == 400
checks.append("review_is_rejected_for_a_run_without_a_repository_spec")

# M5: a task whose verdict belongs to an external harness must not also carry local acceptance.
harness_spec = {"source": "registered:baseline-v1", "commit": "a" * 40, "allowedPaths": ["money.py"],
                "verificationMode": "harness", "verificationFiles": {"test_acceptance.py": "import unittest\n"}}
response = client.post("/runs", json={"mode": "live", "task": "交给官方 harness 判定的任务", "spec": harness_spec,
                                      "requestKey": f"protocol-{uuid.uuid4()}"})
assert response.status_code == 400, response.text
checks.append("harness_verified_tasks_reject_local_acceptance_files")

# M5: an image workspace keeps the instance's own image and drops context management, because
# there is no bounded snapshot to index, remember or bind an approval to.
image_spec = {"source": "https://github.com/psf/requests", "commit": "a" * 40, "subdir": "",
              "allowedPaths": ["requests/utils.py"], "verificationMode": "harness", "workspaceMode": "image",
              "sandboxImage": "swebench/sweb.eval.x86_64.psf_1776_requests-3362:latest",
              "workspacePath": "/testbed", "instanceId": "psf__requests-3362"}
created = client.post("/runs", json={"mode": "live", "task": "把候选补丁交给官方 harness 判定。", "spec": image_spec,
                                     "requestKey": f"protocol-{uuid.uuid4()}"}).raise_for_status().json()
assert created["spec"]["workspaceMode"] == "image" and created["contextMode"] == "full"
assert created["spec"]["sandboxImage"].endswith("requests-3362:latest")
assert created["spec"]["workspacePath"] == "/testbed"
client.post(f"/runs/{created['id']}/cancel", json={}).raise_for_status()
checks.append("an_image_workspace_run_keeps_its_image_and_drops_context_management")

# Exact patch/version binding of an imported official result. No worker or model is used.
evaluated = client.post("/runs", json={"mode": "live", "task": "Synthetic evaluation import protocol check",
    "spec": {**image_spec, "allowedPaths": []}, "requestKey": f"protocol-{uuid.uuid4()}"}).raise_for_status().json()
evaluation_id = evaluated["id"]
claim = client.post(f"/internal/runs/{evaluation_id}/claim", json={"workerId": "protocol-evaluation"},
                    headers=headers).raise_for_status().json()
patch = "synthetic patch for protocol validation only"
evaluation = {"instanceId": image_spec["instanceId"], "patchSha256": hashlib.sha256(patch.encode()).hexdigest(),
              "status": "unresolved", "batchId": "protocol-evaluation"}
assert client.post(f"/runs/{evaluation_id}/evaluation", json=evaluation).status_code == 409
client.post(f"/internal/runs/{evaluation_id}/step", headers=headers, json={
    "workerId": "protocol-evaluation", "generation": claim["generation"], "key": "synthetic-done",
    "type": "SUCCEEDED", "status": "SUCCEEDED", "data": {"patch": patch,
    "verification": {"passed": None, "delegated": "swebench-harness"}}}).raise_for_status()
assert client.post(f"/runs/{evaluation_id}/evaluation", json={**evaluation, "patchSha256": "0" * 64}).status_code == 409
assert client.post(f"/runs/{evaluation_id}/evaluation", json={**evaluation, "instanceId": "wrong"}).status_code == 409
for _ in range(2):
    imported = client.post(f"/runs/{evaluation_id}/evaluation", json=evaluation).raise_for_status().json()
    assert imported["evaluation"]["status"] == "unresolved" and imported["status"] == "SUCCEEDED"
assert client.post(f"/runs/{evaluation_id}/evaluation", json={**evaluation, "status": "resolved"}).status_code == 409
detail = client.get(f"/runs/{evaluation_id}").raise_for_status().json()
assert len([e for e in detail["events"] if e["type"] == "EVALUATION_IMPORTED"]) == 1
checks.append("evaluation_is_separate_immutable_idempotent_and_bound_to_instance_and_patch")

folder = root / "runtime" / "artifacts" / evaluation_id
folder.mkdir(parents=True, exist_ok=True)
(folder / "source.json").write_text(json.dumps({"old.py": "old\n", "same.py": "same\n"}), encoding="utf-8")
(folder / "candidate.json").write_text(json.dumps({"new.py": "new\n", "same.py": "same\n"}), encoding="utf-8")
candidate = client.get(f"/runs/{evaluation_id}/candidate").raise_for_status().json()
assert candidate["changed_files"] == ["new.py", "old.py"]
assert candidate["base"]["new.py"] == "" and candidate["candidate"]["old.py"] == ""
checks.append("candidate_diff_includes_added_and_deleted_files")

report = {
    "checks": checks,
    "run_ids": [cancelled, owned, expired, abandoned, paused_run, invalidated, reviewable, too_late, single_file, created["id"]],
    "note": "Synthetic control-plane verification, no model calls.",
}
path = root / "runtime" / "validation" / "protocol.json"
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
client.close()
