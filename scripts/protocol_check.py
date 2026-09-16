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
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    detail = client.get(f"/runs/{expired}").json()
    if detail["status"] == "INTERRUPTED":
        break
    time.sleep(2)
assert detail["status"] == "INTERRUPTED"
checks.append("expired_lease_marked_interrupted")
report = {
    "checks": checks,
    "run_ids": [cancelled, owned, expired],
    "note": "Synthetic control-plane verification, no model calls.",
}
path = root / "runtime" / "validation" / "protocol.json"
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
client.close()
