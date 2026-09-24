"""Runs a fixed catalog; keeps failures in the denominator and raw reports locally."""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/agent-worker"))
from repofix.reporting import trajectory_summary  # noqa: E402


def summarize(report):
    summary = {}
    for context in dict.fromkeys(r["context"] for r in report["runs"]):
        rows = [r for r in report["runs"] if r["context"] == context]
        results = [r.get("result") or {} for r in rows]
        usages = [r.get("usage") for r in results]
        summary[context] = {
            "attempted": len(rows), "succeeded": sum(r["status"] == "SUCCEEDED" for r in rows),
            "usage_covered_runs": sum(u is not None for u in usages),
            "usage_complete_runs": sum(r.get("usage_status") == "complete" for r in results),
            "usage_partial_runs": sum(r.get("usage_status") == "partial" for r in results),
            "input_tokens_reported": sum(u["input_tokens"] for u in usages if u is not None),
            "output_tokens_reported": sum(u["output_tokens"] for u in usages if u is not None),
            "compactions": sum(len(r.get("context_compactions", [])) for r in results),
        }
    return summary


def recover_report(source, artifacts, output):
    report = json.loads(source.read_text(encoding="utf-8"))
    report["recovered_from"] = str(source.resolve())
    for row in report["runs"]:
        try:
            metrics = trajectory_summary(row["run_id"], report["mode"], artifacts)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            row["usage_recovery"] = type(error).__name__
            continue
        row["result"] = {**(row.get("result") or {}), **metrics}
        row["usage_recovery"] = "local_trajectory"
    report["summary"] = summarize(report)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"recovered-{source.stem}-{uuid.uuid4().hex[:8]}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), "summary": report["summary"]}), flush=True)
    return report


def evaluate(mode, contexts, repeats, output, review_policy="auto"):
    base = "http://localhost:3101"
    with httpx.Client(base_url=base, timeout=20) as client:
        response = client.get("/baseline-tasks")
        response.raise_for_status()
        catalog = response.json()
        if not catalog:
            raise ValueError("No baseline tasks registered")
        output.mkdir(parents=True, exist_ok=True)
        report = {"mode": mode, "repeats": repeats, "task_ids": [t["id"] for t in catalog],
                  "contexts": contexts, "review_policy": review_policy, "memory_enabled": False, "runs": []}
        target = output / f"baseline-{mode}-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
        for repeat in range(repeats):
            for task in catalog:
                # Rotate order so each context occupies each position across repetitions.
                for context in contexts[repeat % len(contexts):] + contexts[:repeat % len(contexts)]:
                    row = {"task_id": task["id"], "context": context, "repeat": repeat, "status": "CLIENT_ERROR"}
                    started = time.monotonic()
                    try:
                        response = client.post("/runs", json={"mode": mode, "baselineId": task["id"],
                            "contextMode": context, "reviewPolicy": review_policy,
                            "memoryEnabled": False, "requestKey": str(uuid.uuid4())})
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
                    row["seconds"] = round(time.monotonic() - started, 2)
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
        report["summary"] = summarize(report)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(target), "summary": report["summary"]}), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--contexts", nargs="+", choices=["full", "compact", "managed"],
                        default=["full", "compact"])
    parser.add_argument("--review-policy", choices=["off", "auto"], default="auto")
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--output", type=Path, default=Path("runtime/validation"))
    parser.add_argument("--recover-report", type=Path, help="Recover usage from local trajectories; makes no API calls")
    parser.add_argument("--artifacts-root", type=Path, default=Path("runtime/artifacts"))
    args = parser.parse_args()
    if args.recover_report:
        recover_report(args.recover_report, args.artifacts_root, args.output)
        raise SystemExit(0)
    report = evaluate("live" if args.live else "demo", args.contexts, args.repeats, args.output,
                      args.review_policy)
    raise SystemExit(0 if all(r["status"] == "SUCCEEDED" for r in report["runs"]) else 1)
