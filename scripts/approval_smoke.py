"""Deterministic M3 approval smoke: pause a strict-policy run, decide it, resume in a new generation.

Runs against the deployed stack with workers stopped or running; each approval costs one
execution generation and resumes from the registered checkpoint. Uses built-in demo tasks
only, so no model credits are consumed.
"""

import json
import time
import uuid
from pathlib import Path

import httpx

client = httpx.Client(base_url="http://localhost:3101", timeout=20)


def run_with_approvals(payload, limit=300):
    run = client.post("/runs", json={**payload, "requestKey": f"m3-approval-{uuid.uuid4()}"})
    run.raise_for_status()
    run = run.json()
    approvals, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run['id']}").raise_for_status().json()
        if run["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
            break
        pending = [a for a in run.get("approvals", []) if a["status"] == "PENDING"]
        if run["status"] == "WAITING_APPROVAL" and pending:
            approval = pending[0]
            assert approval["action"]["command"], approval
            assert approval["generation"] == run["generation"] and approval["workspaceSha256"]
            assert approval["checkpointSequence"] >= 1 and approval["policy"] == "strict"
            approvals.append(approval)
            client.post(f"/runs/{run['id']}/approvals/{approval['id']}/decide",
                        json={"decision": "approve", "note": "M3 冒烟：批准该动作"}).raise_for_status()
        time.sleep(1)
    return run, approvals


def verify(run, approvals, label):
    assert run["status"] == "SUCCEEDED", run.get("result")
    assert approvals, f"{label}: 严格策略下应至少暂停一次等待审批"
    assert run["recoveryAttempts"] == 0, run["recoveryAttempts"]
    events = run["events"]
    assert run["generation"] == 1 + len(approvals), (run["generation"], len(approvals))
    requested = {e["data"]["approval_id"]: e["data"] for e in events if e["type"] == "APPROVAL_REQUESTED"}
    recovered = [e["data"] for e in events if e["type"] == "RECOVERED"]
    decided = [e["data"] for e in events if e["type"] == "APPROVAL_DECIDED"]
    applied = [e["data"] for e in events if e["type"] == "APPROVAL_APPLIED"]
    assert len(requested) == len(approvals) == len(recovered) == len(decided) == len(applied)
    assert all(e["decision"] == "APPROVED" for e in decided)
    assert all(e["decision"] == "execute" for e in applied)
    assert [e["from_generation"] + 1 for e in recovered] == [e["generation"] for e in recovered]
    for approval in approvals:
        # The approval was bound to the paused action, its checkpoint and that workspace version.
        assert requested[approval["id"]]["action_sha256"] == approval["actionSha256"]
        assert requested[approval["id"]]["workspace_sha256"] == approval["workspaceSha256"]
        assert requested[approval["id"]]["checkpoint"]["id"] == approval["checkpointId"]
    assert not [e for e in events if e["type"] in ("FAILED", "INTERRUPTED")]
    assert run["result"]["verification"]["passed"] is True
    return {"run_id": run["id"], "approvals": len(approvals), "generation": run["generation"],
            "status": run["status"], "changed_files": run["result"].get("changed_files")}


repository_run, repository_approvals = run_with_approvals(
    {"mode": "demo", "baselineId": "checkout", "contextMode": "managed", "memoryEnabled": False,
     "approvalPolicy": "strict"})
legacy_run, legacy_approvals = run_with_approvals({"mode": "demo", "approvalPolicy": "strict"})

report = {
    "checks": ["strict_policy_pauses_before_writing", "approval_binds_action_checkpoint_and_workspace_version",
               "decision_requeues_the_run", "new_generation_resumes_from_the_checkpoint",
               "approved_action_is_applied_once_and_run_completes", "acceptance_passed_after_recovery"],
    "repository": verify(repository_run, repository_approvals, "repository"),
    "legacy": verify(legacy_run, legacy_approvals, "legacy"),
    "generative_model_calls": 0,
}
target = Path(__file__).resolve().parents[1] / "runtime" / "validation" / "m3-approval.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
client.close()
