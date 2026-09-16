"""One deterministic managed-context deployment smoke, not a baseline effectiveness evaluation."""
import json
import time
import uuid
from pathlib import Path

import httpx

client = httpx.Client(base_url="http://localhost:3101", timeout=15)
run = client.post("/runs", json={"requestKey": "m2-smoke-" + str(uuid.uuid4()), "mode": "demo",
    "baselineId": "checkout", "contextMode": "managed", "memoryEnabled": False}).raise_for_status().json()
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    run = client.get(f"/runs/{run['id']}").raise_for_status().json()
    if run["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
        break
    time.sleep(1)
assert run["status"] == "SUCCEEDED", run.get("result")
events = run["events"]
assembled = [e["data"] for e in events if e["type"] == "CONTEXT_ASSEMBLED"]
assert assembled and all(c["estimated_input_tokens"] <= c["input_limit"] for c in assembled)
assert all(c["retrieval"]["strategy"] == "fused" for c in assembled), assembled
assert not any(e["type"] == "INDEX_FALLBACK" for e in events)
assert all(c["memories"] == [] for c in assembled)
assert any(c["retrieval"]["changed_paths"] for c in assembled)
assert any(e["type"] == "CHECKPOINT_SAVED" for e in events)
report = {"run_id": run["id"], "status": run["status"], "checks": ["managed_task_completed", "all_requests_within_budget",
    "real_vector_retrieval_without_fallback", "changed_workspace_overlay", "memory_disabled", "checkpoint_saved"],
    "context_calls": len(assembled), "generative_model_calls": 0}
target = Path(__file__).resolve().parents[1] / "runtime" / "validation" / "m2-smoke.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
client.close()
