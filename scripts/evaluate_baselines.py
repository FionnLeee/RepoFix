"""Runs a fixed catalog; keeps failures in the denominator and raw reports locally."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def evaluate(mode, contexts, repeats, output):
    base = "http://localhost:3101"
    with httpx.Client(base_url=base, timeout=20) as client:
        response = client.get("/baseline-tasks")
        response.raise_for_status()
        catalog = response.json()
        if not catalog:
            raise ValueError("No baseline tasks registered")
        output.mkdir(parents=True, exist_ok=True)
        report = {"mode": mode, "repeats": repeats, "task_ids": [t["id"] for t in catalog], "runs": []}
        target = output / f"baseline-{mode}-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
        for repeat in range(repeats):
            for task in catalog:
                # Alternate paired order across repetitions to reduce a fixed order effect.
                for context in (contexts if repeat % 2 == 0 else list(reversed(contexts))):
                    row = {"task_id": task["id"], "context": context, "repeat": repeat, "status": "CLIENT_ERROR"}
                    try:
                        response = client.post("/runs", json={"mode": mode, "baselineId": task["id"],
                            "contextMode": context, "requestKey": str(uuid.uuid4())})
                        response.raise_for_status()
                        run_id = response.json()["id"]
                        row["run_id"] = run_id
                        deadline = time.monotonic() + 540
                        while time.monotonic() < deadline:
                            response = client.get(f"/runs/{run_id}")
                            response.raise_for_status()
                            run = response.json()
                            if run["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
                                row.update(status=run["status"], result=run.get("result"), events=run.get("events"))
                                break
                            time.sleep(1)
                        else:
                            row["status"] = "CLIENT_TIMEOUT"
                            client.post(f"/runs/{run_id}/cancel", json={}).raise_for_status()
                    except httpx.HTTPError as error:
                        row["error"] = type(error).__name__
                    report["runs"].append(row)
                    result = row.get("result") or {}
                    if row["status"] == "SUCCEEDED":
                        row["outcome"] = "verified_patch"
                    elif "RepeatedFormatError" in result.get("error", ""):
                        row["outcome"] = "model_output_format"
                    elif result.get("verification") and not result["verification"]["passed"]:
                        row["outcome"] = "verification_failed"
                    else:
                        row["outcome"] = "execution_or_transport_failure"
                    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({k: row[k] for k in ("task_id", "context", "status")}), flush=True)
        report["summary"] = {}
        for context in contexts:
            rows = [r for r in report["runs"] if r["context"] == context]
            usages = [(r.get("result") or {}).get("usage") for r in rows]
            report["summary"][context] = {
                "attempted": len(rows), "succeeded": sum(r["status"] == "SUCCEEDED" for r in rows),
                "usage_covered_runs": sum(u is not None for u in usages),
                "input_tokens_reported": sum(u["input_tokens"] for u in usages if u is not None),
                "output_tokens_reported": sum(u["output_tokens"] for u in usages if u is not None),
                "compactions": sum(len((r.get("result") or {}).get("context_compactions", [])) for r in rows),
            }
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(target), "summary": report["summary"]}), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--contexts", nargs="+", choices=["full", "compact"], default=["full", "compact"])
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--output", type=Path, default=Path("runtime/validation"))
    args = parser.parse_args()
    report = evaluate("live" if args.live else "demo", args.contexts, args.repeats, args.output)
    raise SystemExit(0 if all(r["status"] == "SUCCEEDED" for r in report["runs"]) else 1)
