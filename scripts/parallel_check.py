import concurrent.futures
import json
import time
import uuid
from pathlib import Path

import httpx


def run_one(_):
    response = httpx.post(
        "http://localhost:3101/runs", json={"mode": "demo", "requestKey": f"parallel-{uuid.uuid4()}"}, timeout=15
    )
    response.raise_for_status()
    run_id = response.json()["id"]
    for _ in range(60):
        result = httpx.get(f"http://localhost:3101/runs/{run_id}", timeout=10).json()
        if result["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
            return result
        time.sleep(2)
    raise TimeoutError(run_id)


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(run_one, range(2)))
report = {
    "runs": [{"id": r["id"], "status": r["status"], "workerId": r["workerId"]} for r in results],
    "distinct_workers": len({r["workerId"] for r in results}),
}
path = Path(__file__).resolve().parents[1] / "runtime" / "validation" / "parallel.json"
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
assert all(r["status"] == "SUCCEEDED" for r in results)
assert report["distinct_workers"] == 2
