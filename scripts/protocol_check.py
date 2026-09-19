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

# Delivery: the host executor registers what it saw on the target, the person approves exactly that,
# and only the registering executor can apply. The control plane never touches the target itself.
delivered = create_repository({"reviewPolicy": "off"})
delivery_run = delivered["id"]
subdir = delivered["spec"]["subdir"]
claim = client.post(f"/internal/runs/{delivery_run}/claim", json={"workerId": "protocol-delivery"},
                    headers=headers).raise_for_status().json()
delivery_patch = ("diff --git a/money.py b/money.py\nindex 1111111..2222222 100644\n--- a/money.py\n+++ b/money.py\n"
                  "@@ -1 +1 @@\n-old\n+new\n"
                  "diff --git a/extra.py b/extra.py\nnew file mode 100644\nindex 0000000..3333333\n--- /dev/null\n"
                  "+++ b/extra.py\n@@ -0,0 +1 @@\n+x\n")
patch_sha = hashlib.sha256(delivery_patch.encode()).hexdigest()
executor_token = "e" * 64
delivery_files = [{"path": f"{subdir}/extra.py", "change": "added", "before": None, "after": "3" * 40},
                  {"path": f"{subdir}/money.py", "change": "modified", "before": "1" * 40, "after": "2" * 40}]
survey_body = {"id": str(uuid.uuid4()), "targetPath": "/home/me/checkout", "targetHead": delivered["spec"]["commit"],
               "baseCommit": delivered["spec"]["commit"], "patchSha256": patch_sha, "targetFingerprint": "f" * 64,
               "executorSha256": hashlib.sha256(executor_token.encode()).hexdigest(), "files": delivery_files}
assert client.post(f"/runs/{delivery_run}/deliveries", json=survey_body).status_code == 409  # still RUNNING
unresolved_body = {**survey_body, "baseCommit": "a" * 40, "targetHead": "a" * 40,
                   "patchSha256": hashlib.sha256(patch.encode()).hexdigest()}
assert client.post(f"/runs/{evaluation_id}/deliveries", json=unresolved_body).status_code == 409  # unresolved harness
client.post(f"/internal/runs/{delivery_run}/step", headers=headers, json={
    "workerId": "protocol-delivery", "generation": claim["generation"], "key": "synthetic-done",
    "type": "SUCCEEDED", "status": "SUCCEEDED", "data": {"patch": delivery_patch, "changed_files": ["money.py", "extra.py"],
    "verification": {"passed": True, "output": "ok"}}}).raise_for_status()
checks.append("delivery_requires_a_finished_and_accepted_candidate")
assert client.post(f"/runs/{delivery_run}/deliveries",
                   json={**survey_body, "files": [{**delivery_files[1], "path": "money.py"}, delivery_files[0]]}).status_code == 409
assert client.post(f"/runs/{delivery_run}/deliveries",
                   json={**survey_body, "files": [{**delivery_files[0], "before": "9" * 40}, delivery_files[1]]}).status_code == 400
assert client.post(f"/runs/{delivery_run}/deliveries", json={**survey_body, "patchSha256": "0" * 64}).status_code == 409
assert client.post(f"/runs/{delivery_run}/deliveries", json={**survey_body, "baseCommit": "0" * 40}).status_code == 409
checks.append("delivery_file_list_patch_and_base_must_match_the_run")
delivery = client.post(f"/runs/{delivery_run}/deliveries", json=survey_body).raise_for_status().json()
assert delivery["status"] == "PENDING" and "executorSha256" not in delivery
assert client.post(f"/runs/{delivery_run}/deliveries", json=survey_body).raise_for_status().json()["id"] == delivery["id"]
assert client.post(f"/runs/{delivery_run}/deliveries", json={**survey_body, "targetFingerprint": "a" * 64}).status_code == 409
checks.append("delivery_registration_is_idempotent_per_id")
decision = {"decision": "approve", "patchSha256": patch_sha, "targetFingerprint": "f" * 64}
assert client.post(f"/runs/{delivery_run}/deliveries/{delivery['id']}/decide",
                   json={**decision, "targetFingerprint": "a" * 64}).status_code == 409
assert client.post(f"/deliveries/{delivery['id']}/claim", json={"executorToken": executor_token}).status_code == 409
approved = client.post(f"/runs/{delivery_run}/deliveries/{delivery['id']}/decide", json=decision).raise_for_status().json()
assert approved["status"] == "APPROVED" and approved["decidedAt"]
assert client.post(f"/runs/{delivery_run}/deliveries/{delivery['id']}/decide", json=decision).raise_for_status().json()["status"] == "APPROVED"
assert client.post(f"/runs/{delivery_run}/deliveries/{delivery['id']}/decide",
                   json={**decision, "decision": "reject"}).status_code == 409
checks.append("delivery_decision_binds_patch_and_target_fingerprint_and_is_final")
assert client.post(f"/deliveries/{delivery['id']}/claim", json={"executorToken": "d" * 64}).status_code == 409
for _ in range(2):
    assert client.post(f"/deliveries/{delivery['id']}/claim", json={"executorToken": executor_token}).raise_for_status().json()["status"] == "APPLYING"
assert client.post(f"/runs/{delivery_run}/deliveries", json={**survey_body, "id": str(uuid.uuid4())}).status_code == 409
receipt = {"executorToken": executor_token, "status": "APPLIED", "receipt": {"files_written": 2, "head": "a" * 40}}
for _ in range(2):
    assert client.post(f"/deliveries/{delivery['id']}/finish", json=receipt).raise_for_status().json()["status"] == "APPLIED"
assert client.post(f"/deliveries/{delivery['id']}/finish", json={**receipt, "status": "INVALIDATED"}).status_code == 409
assert client.post(f"/deliveries/{delivery['id']}/claim", json={"executorToken": executor_token}).raise_for_status().json()["status"] == "APPLIED"
checks.append("only_the_registering_executor_applies_and_repeats_are_no_ops")
older = client.post(f"/runs/{delivery_run}/deliveries", json={**survey_body, "id": str(uuid.uuid4())}).raise_for_status().json()
newer = client.post(f"/runs/{delivery_run}/deliveries",
                    json={**survey_body, "id": str(uuid.uuid4()), "targetFingerprint": "a" * 64}).raise_for_status().json()
rows = {row["id"]: row for row in client.get(f"/runs/{delivery_run}/deliveries").raise_for_status().json()}
assert rows[older["id"]]["status"] == "INVALIDATED" and rows[older["id"]]["receipt"]["superseded_by"] == newer["id"]
assert rows[newer["id"]]["status"] == "PENDING" and all("executorSha256" not in row for row in rows.values())
assert client.post(f"/runs/{delivery_run}/deliveries/{older['id']}/decide", json=decision).status_code == 409
rejected = client.post(f"/runs/{delivery_run}/deliveries/{newer['id']}/decide",
                       json={**decision, "decision": "reject", "targetFingerprint": "a" * 64}).raise_for_status().json()
assert rejected["status"] == "REJECTED"
assert client.post(f"/deliveries/{newer['id']}/claim", json={"executorToken": executor_token}).status_code == 409
assert client.get(f"/deliveries/{newer['id']}").raise_for_status().json()["status"] == "REJECTED"
detail = client.get(f"/runs/{delivery_run}").raise_for_status().json()
delivery_events = [e["type"] for e in detail["events"] if e["type"].startswith("DELIVERY_")]
assert delivery_events == ["DELIVERY_REQUESTED", "DELIVERY_APPROVED", "DELIVERY_APPLYING", "DELIVERY_APPLIED",
                           "DELIVERY_REQUESTED", "DELIVERY_INVALIDATED", "DELIVERY_REQUESTED", "DELIVERY_REJECTED"], delivery_events
checks.append("a_newer_survey_supersedes_an_undecided_delivery_and_rejection_blocks_apply")

report = {
    "checks": checks,
    "run_ids": [cancelled, owned, expired, abandoned, paused_run, invalidated, reviewable["id"], too_late["id"], single_file, created["id"],
                delivery_run],
    "note": "Synthetic control-plane verification, no model calls.",
}
path = root / "runtime" / "validation" / "protocol.json"
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
client.close()
