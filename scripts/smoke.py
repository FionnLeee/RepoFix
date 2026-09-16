import argparse
import concurrent.futures
import json
import time
import uuid
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true")
args = parser.parse_args()
base = "http://localhost:3101"
report = {"mode": "live" if args.live else "demo", "checks": [], "started_at": time.time()}


def create(key):
    response = httpx.post(f"{base}/runs", json={"mode": report["mode"], "requestKey": key}, timeout=20)
    response.raise_for_status()
    return response.json()


assert httpx.get(f"{base}/health").status_code == 200
report["checks"].append("health")
key = f"smoke-{uuid.uuid4()}"
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    created = list(pool.map(create, [key] * 6))
assert len({run["id"] for run in created}) == 1
report["checks"].append("six_concurrent_creates_one_run")
run_id = created[0]["id"]
assert httpx.post(f"{base}/internal/runs/{run_id}/claim", json={"workerId": "unauthorized"}).status_code == 401
report["checks"].append("worker_auth_required")
assert (
    httpx.post(f"{base}/runs", json={"mode": "demo", "requestKey": str(uuid.uuid4()), "path": "/etc"}).status_code
    == 400
)
report["checks"].append("unknown_fields_rejected")
deadline = time.monotonic() + (420 if args.live else 120)
while time.monotonic() < deadline:
    result = httpx.get(f"{base}/runs/{run_id}", timeout=10).json()
    if result["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
        break
    time.sleep(2)
report["run"] = result
report["duration_seconds"] = round(time.time() - report["started_at"], 2)
folder = Path(__file__).resolve().parents[1] / "runtime" / "validation"
folder.mkdir(parents=True, exist_ok=True)
target = folder / f"{report['mode']}-{run_id}.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        {"run_id": run_id, "status": result["status"], "checks": report["checks"], "report": str(target)},
        ensure_ascii=False,
    )
)
assert result["status"] == "SUCCEEDED", result.get("result")
assert result["result"]["verification"]["passed"] and result["result"]["patch"]
assert sum(e["type"] == "RUNNING" for e in result["events"]) == 1
