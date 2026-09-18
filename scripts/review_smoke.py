"""Deterministic M4 review smoke: independent review, one bounded revision, then a re-review.

Runs against the deployed stack with a built-in demo task. The reviewer is the deterministic
stub, whose first finding is labelled as a stub on purpose: this script verifies the
collaboration flow (independent context, position-validated findings, bounded revision,
stale review, acceptance still deciding), not review quality, and spends no model credits.
"""

import json
import time
import uuid
from pathlib import Path

import httpx

client = httpx.Client(base_url="http://localhost:3101", timeout=20)
REPOSITORY = {"mode": "demo", "baselineId": "checkout", "contextMode": "managed", "memoryEnabled": False}


def execute(payload, limit=300):
    run = client.post("/runs", json={**payload, "requestKey": f"m4-review-{uuid.uuid4()}"})
    run.raise_for_status()
    run = run.json()
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run['id']}").raise_for_status().json()
        if run["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
            break
        time.sleep(1)
    return run


def verify_reviewed(run):
    assert run["status"] == "SUCCEEDED", run.get("result")
    assert run["spec"]["allowedPaths"], run.get("spec")
    events = run["events"]
    kinds = [event["type"] for event in events]
    assert kinds.count("REVIEW_REQUESTED") == 2, kinds
    assert kinds.count("REVIEW_COMPLETED") == 2 and kinds.count("REVISION_REQUESTED") == 1
    assert kinds.count("REVIEW_INVALIDATED") == 1 and "REVIEW_FAILED" not in kinds
    first, second = (event["data"] for event in events if event["type"] == "REVIEW_COMPLETED")
    assert first["round"] == 1 and second["round"] == 2
    assert first["reviewer"] == "deterministic-stub" and first["parse_error"] is None
    assert first["invalid_count"] == 0 and first["invalid"] == []
    # The reviewer named a real position in the candidate it was shown.
    finding = first["findings"][0]
    assert finding["severity"] == "blocking" and finding["file"] in run["spec"]["allowedPaths"]
    assert finding["line"] >= 1 and finding["trigger"] and finding["evidence"] and finding["suggestion"]
    assert second["findings"] == [] and second["summary"]
    # A revision happened between the two reviews and it changed the candidate, so the first
    # review stops being evidence for the current version.
    revision = next(event["data"] for event in events if event["type"] == "REVISION_REQUESTED")
    assert revision["findings"] == first["findings"] and revision["round"] == 1
    invalidated = next(event["data"] for event in events if event["type"] == "REVIEW_INVALIDATED")
    assert invalidated["reviewed_sha256"] == first["candidate_sha256"]
    assert invalidated["candidate_sha256"] == second["candidate_sha256"] != first["candidate_sha256"]
    # Every review is bound to the candidate it read, and the acceptance run still decides.
    assert first["candidate_sha256"] != second["candidate_sha256"]
    result = run["result"]
    assert result["verification"]["passed"] is True
    assert result["review"]["policy"] == "auto" and result["review"]["rounds"] == 1
    assert result["review"]["unresolved_blocking"] == 0 and result["review"]["max_rounds"] == 2
    assert [record["round"] for record in result["review"]["reviews"]] == [1, 2]
    assert "评审修订" in result["patch"]
    assert set(result["changed_files"]).issubset(run["spec"]["allowedPaths"])
    # The reviewer only read: the candidate is the coder's revision, not the reviewer's.
    assert kinds.index("REVISION_REQUESTED") < kinds.index("REVIEW_COMPLETED", kinds.index("REVISION_REQUESTED"))
    return {"run_id": run["id"], "reviews": 2, "revisions": 1, "changed_files": result["changed_files"],
            "blocking_findings": first["blocking_count"] if "blocking_count" in first else len(first["findings"]),
            "status": run["status"]}


def verify_unreviewed(run):
    assert run["status"] == "SUCCEEDED", run.get("result")
    kinds = [event["type"] for event in run["events"]]
    assert not [kind for kind in kinds if kind.startswith("REVIEW_")], kinds
    assert "评审修订" not in run["result"]["patch"]
    assert run["result"]["review"]["policy"] == "off" and run["result"]["review"]["reviews"] == []
    assert run["result"]["verification"]["passed"] is True
    return {"run_id": run["id"], "reviews": 0, "status": run["status"]}


reviewed = execute({**REPOSITORY, "reviewPolicy": "auto"})
off = execute({**REPOSITORY, "reviewPolicy": "off"})
report = {
    "checks": ["reviewer_runs_with_its_own_context_before_acceptance",
               "findings_must_name_a_real_candidate_position",
               "one_bounded_revision_is_requested_and_the_old_review_is_invalidated",
               "second_review_is_clean_and_acceptance_still_decides",
               "review_can_be_turned_off_per_run",
               "the_reviewer_never_writes_to_the_candidate"],
    "reviewed": verify_reviewed(reviewed),
    "unreviewed": verify_unreviewed(off),
    "generative_model_calls": 0,
}
target = Path(__file__).resolve().parents[1] / "runtime" / "validation" / "m4-review.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
client.close()
