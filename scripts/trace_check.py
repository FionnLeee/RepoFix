"""Deterministic M4 trace check: one trace covers the control plane, the broker and the worker.

Runs a built-in demo task (no model credits), then reads the trace back from Jaeger and checks
that the attempt span continues the context the control plane put on the queue message, and
that the steps a slow run would be diagnosed from are in it.
"""

import json
import time
import urllib.request
import uuid
from pathlib import Path

API = "http://localhost:3101"
JAEGER = "http://localhost:16686"


def api(path, payload=None):
    request = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"content-type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def jaeger(path):
    with urllib.request.urlopen(JAEGER + path, timeout=30) as response:
        return json.loads(response.read())


run = api("/runs", {"mode": "demo", "baselineId": "checkout", "contextMode": "managed", "memoryEnabled": False,
                    "reviewPolicy": "auto", "requestKey": f"m4-trace-{uuid.uuid4()}"})
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    detail = api(f"/runs/{run['id']}")
    if detail["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
        break
    time.sleep(1)
assert detail["status"] == "SUCCEEDED", detail.get("result")
result = detail["result"]
trace_id, parent = result.get("trace_id"), result.get("trace_parent_span_id")
assert trace_id and parent, result

# Jaeger ingests spans in batches, so the export is polled rather than assumed.
payload = None
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    payload = jaeger(f"/api/traces/{trace_id}")
    if payload.get("data"):
        break
    time.sleep(2)
spans = payload["data"][0]["spans"] if payload.get("data") else []
by_name = {}
for span in spans:
    by_name.setdefault(span["operationName"], []).append(span)
attempt = by_name.get("attempt", [None])[0]
assert attempt is not None, [span["operationName"] for span in spans]
# The attempt continues the trace the control plane started: same trace, same parent span.
assert attempt["traceID"] == trace_id
parents = [reference["spanID"] for reference in attempt.get("references", [])]
assert parents == [parent], (parents, parent)

expected = {"attempt", "model.call", "tool.execute", "review", "acceptance.baseline", "acceptance.candidate"}
missing = expected - set(by_name)
assert not missing, sorted(missing)
# Steps are children of the attempt, not siblings of it.
child_refs = {span["spanID"]: span for span in spans if span["spanID"] != attempt["spanID"]}
assert all(child["references"] and child["references"][0]["spanID"] == attempt["spanID"]
           for child in child_refs.values()), "steps must hang off the attempt span"

report = {
    "checks": ["the_run_reports_the_trace_it_ran_in",
               "the_attempt_continues_the_context_the_control_plane_published",
               "one_run_covers_model_tool_review_and_acceptance_steps",
               "every_step_is_a_child_of_the_attempt_span"],
    "trace_id": trace_id,
    "parent_span_id": parent,
    "spans": sorted(f"{span['operationName']} ({span['duration'] / 1000:.0f} ms)" for span in spans),
    "generative_model_calls": 0,
}
target = Path(__file__).resolve().parents[1] / "runtime" / "validation" / "m4-trace.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
