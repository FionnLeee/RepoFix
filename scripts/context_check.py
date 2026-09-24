"""M2 protocol/real-index fault checks. Stop queue workers first; no generative model calls."""
import json
import os
import threading
import uuid
from pathlib import Path

import httpx
from repofix.indexing import CodeIndex, ControlContext, content_hash
from repofix.repository import RepositoryTask, load_snapshot

base = os.getenv("CONTROL_API_URL", "http://localhost:3101")
headers = {"authorization": f"Bearer {os.environ['WORKER_TOKEN']}"}
client = httpx.Client(base_url=base, headers=headers, timeout=30)
owned = []
checks = []
stop = threading.Event()


def emit(run, kind, data, status=None):
    body = {"workerId": run["workerId"], "generation": run["generation"], "key": str(uuid.uuid4()),
            "type": kind, "data": data}
    if status:
        body["status"] = status
    client.post(f"/internal/runs/{run['id']}/step", json=body).raise_for_status()


def pulse():
    while not stop.wait(8):
        for run in owned[:]:
            try:
                emit(run, "HEARTBEAT", {})
            except httpx.HTTPError:
                pass


def create(baseline="checkout", memory=True, **overrides):
    body = {"mode": "demo", "requestKey": "m2-" + str(uuid.uuid4()), "baselineId": baseline,
            "contextMode": "managed", "memoryEnabled": memory, **overrides}
    if body.get("spec"):
        body.pop("baselineId")
    run = client.post("/runs", json=body).raise_for_status().json()
    run = client.post(f"/internal/runs/{run['id']}/claim", json={"workerId": "m2-check"}).raise_for_status().json()
    owned.append(run)
    return run


thread = threading.Thread(target=pulse, daemon=True)
thread.start()
try:
    run = create()
    control = ControlContext(run)
    detail = client.get(f"/runs/{run['id']}").raise_for_status().json()
    evidence = detail["events"][-1]["id"]
    memory_body = {"content": "M2 verification note: preserve rounding.", "sourceRun": run["id"],
                   "evidenceRefs": [evidence], "scope": "project"}
    memory = client.post(f"/projects/{run['projectId']}/memories", json=memory_body).raise_for_status().json()
    assert any(m["id"] == memory["id"] for m in control.call("context")["memories"])
    other = create("pagination")
    other_control = ControlContext(other)
    assert all(m["id"] != memory["id"] for m in other_control.call("context")["memories"])
    assert client.post(f"/projects/{other['projectId']}/memories", json=memory_body).status_code == 400
    disabled_run = create(memory=False)
    disabled_control = ControlContext(disabled_run)
    assert disabled_control.call("context")["memories"] == []
    checks.append("project_isolation_and_memory_read_opt_out")
    path = f"/projects/{run['projectId']}/memories/{memory['id']}"
    disabled = client.post(path, json={**memory_body, "version": 1, "validity": "disabled"}).raise_for_status().json()
    assert not any(m["id"] == memory["id"] for m in control.call("context")["memories"])
    assert client.post(path, json={**memory_body, "version": 1}).status_code == 409
    enabled = client.post(path, json={**memory_body, "version": disabled["version"]}).raise_for_status().json()
    checks.append("memory_disable_and_optimistic_version_conflict")
    # This claimed synthetic run never calls a model or loads the invented commit.
    if client.get("/health").json()["liveEnabled"]:
        newer = create(mode="live", task="M2 synthetic provenance check only; do not execute",
                       spec={**run["spec"], "commit": "b" * 40})
        newer_control = ControlContext(newer)
        stale = next(m for m in newer_control.call("context")["memories"] if m["id"] == memory["id"])
        assert stale["validity"] == "needs_review"
        newer_control.close()
        checks.append("base_commit_change_marks_memory_needs_review")
    client.post(path, json={**memory_body, "version": enabled["version"], "validity": "deleted"}).raise_for_status()
    assert not any(m["id"] == memory["id"] for m in client.get(f"/projects/{run['projectId']}/memories").json())
    assert not any(m["id"] == memory["id"] for m in control.call("context")["memories"])
    checks.append("deleted_memory_not_recalled")
    client.post(f"/runs/{run['id']}/compact", json={}).raise_for_status()
    assert control.call("context")["compactRequested"] == 1
    checks.append("manual_compaction_persisted")
    files = load_snapshot(RepositoryTask.model_validate(run["spec"]))
    index = CodeIndex(run, files, control, lambda kind, data: emit(run, kind, data))
    hits, report = index.search("discount order rounding", files)
    assert hits and report["strategy"] == "fused" and report["indexes"], report
    changed_path = run["spec"]["allowedPaths"][0]
    current = {**files, changed_path: "def m2_changed():\n    return 'CURRENT_SENTINEL'\n"}
    removed_path = next(p for p in files if p != changed_path)
    del current[removed_path]
    hits, report = index.search("m2_changed CURRENT_SENTINEL", current)
    assert report["strategy"] == "fused" and len(report["indexes"]) == 2
    assert not any(h["path"] == removed_path for h in hits)
    assert all(h["file_hash"] == content_hash(current[h["path"]]) for h in hits)
    assert any(h["path"] == changed_path and "CURRENT_SENTINEL" in h["text"] for h in hits)
    checks.append("real_embedding_qdrant_base_and_changed_deleted_overlay")
    partial = {"id": str(uuid.uuid4()), "scope": "overlay", "embedding": index.encoder.version,
               "snapshotHash": "c" * 64, "manifestHash": "d" * 64, "pointIds": [str(uuid.uuid4())]}
    build = control.call("indexes/begin", partial)
    assert control.call("indexes/begin", partial)["id"] == build["id"]
    try:
        control.call("indexes/publish", {"id": build["id"]})
        raise AssertionError("Incomplete index published")
    except httpx.HTTPStatusError as error:
        assert error.response.status_code == 409
    newer_build = control.call("indexes/begin", {**partial, "id": str(uuid.uuid4())})
    try:
        control.call("indexes/publish", {"id": build["id"]})
        raise AssertionError("Stale build published")
    except httpx.HTTPStatusError as error:
        assert error.response.status_code == 409
    bad = {**partial, "id": str(uuid.uuid4()), "workerId": run["workerId"], "generation": run["generation"] + 1}
    assert client.post(f"/internal/runs/{run['id']}/indexes/begin", json=bad).status_code == 409
    assert newer_build["id"] != build["id"]
    checks.append("incomplete_stale_build_and_stale_worker_cannot_publish")
    index.close()
    control.close()
    other_control.close()
    disabled_control.close()
finally:
    stop.set()
    thread.join(timeout=10)
    for run in owned:
        client.post(f"/runs/{run['id']}/cancel", json={})
        emit(run, "CANCELLED", {"reason": "M2 synthetic check complete"}, "CANCELLED")
    client.close()
report = {"checks": checks, "run_ids": [r["id"] for r in owned], "generative_model_calls": 0,
          "embedding": "pinned local BGE ONNX", "note": "Functional checks, not an effectiveness evaluation"}
target = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / "validation" / "m2-protocol.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
