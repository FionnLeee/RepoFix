"""Verify frozen interview cohorts and emit a derived, read-only metrics file.

This does not run an agent or evaluator. The Mini CSV is an explicit frozen index;
each row is checked against its manifest and the archived result.patch.
"""

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def mini_metrics(csv_path, artifact_root):
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 70:
        raise ValueError(f"Expected 70 frozen Mini attempts, got {len(rows)}")
    manifests = {}
    source_hashes = {str(csv_path): sha256(csv_path)}
    seen = set()
    first = []
    for row in rows:
        issue, run_id = row["instance_id"], row["run_id"]
        key = (issue, row["cohort"], row["reviewer"])
        if key in seen:
            raise ValueError(f"Duplicate Mini row: {key}")
        seen.add(key)
        manifest_path = Path(row["verdict_manifest"])
        if manifest_path not in manifests:
            manifests[manifest_path] = read_json(manifest_path)
            source_hashes[str(manifest_path)] = sha256(manifest_path)
        manifest = manifests[manifest_path]
        arm = manifest.get("arms", {}).get(row["reviewer"], manifest)
        bindings = [item for item in arm.get("bindings", []) if item["instance_id"] == issue]
        if len(bindings) != 1 or bindings[0]["run_id"] != run_id:
            raise ValueError(f"Mini manifest binding mismatch: {issue}/{run_id}")
        result_path = artifact_root / run_id / "result.json"
        result = read_json(result_path)
        source_hashes[str(result_path)] = sha256(result_path)
        patch = result.get("patch") or ""
        patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        if patch_hash != bindings[0]["patch_sha256"]:
            raise ValueError(f"Mini patch hash mismatch: {issue}/{run_id}")
        if (patch != "") != (row["patch_nonempty"].lower() == "true"):
            raise ValueError(f"Mini patch presence mismatch: {issue}/{run_id}")
        if result.get("model_calls") != int(row["model_calls"]):
            raise ValueError(f"Mini model-call count mismatch: {issue}/{run_id}")
        usage = result.get("usage") or {}
        if usage.get("input_tokens", 0) + usage.get("output_tokens", 0) != int(row["reported_tokens"]):
            raise ValueError(f"Mini reported-token mismatch: {issue}/{run_id}")
        recorded_cap = (result.get("provenance") or {}).get("step_limit")
        if recorded_cap is not None and recorded_cap != int(row["call_cap"]):
            raise ValueError(f"Mini call-cap mismatch: {issue}/{run_id}")
        resolved = issue in arm["verdict"]["resolved_ids"]
        if resolved != (row["official_verdict"] == "resolved"):
            raise ValueError(f"Mini verdict mismatch: {issue}/{run_id}")
        if row["cohort"] != "100_retry" and row["reviewer"] == "off":
            first.append(row)
    if len(first) != 50 or len({row["instance_id"] for row in first}) != 50:
        raise ValueError("Mini first attempt must cover 50 different issues")
    caps = Counter(int(row["call_cap"]) for row in first)
    if caps != {60: 30, 100: 20}:
        raise ValueError(f"Unexpected Mini first-attempt call caps: {caps}")
    retries = [row for row in rows if row["cohort"] == "100_retry"]
    failed_first = {row["instance_id"] for row in first if row["official_verdict"] != "resolved"}
    if len(retries) != 10 or any(row["instance_id"] not in failed_first for row in retries):
        raise ValueError("Mini retry cohort is incomplete or unbound")
    return {
        "attempts": len(rows),
        "first_attempt_issues": len(first),
        "first_attempt_resolved": sum(row["official_verdict"] == "resolved" for row in first),
        "first_attempt_empty_patches": sum(row["patch_nonempty"].lower() != "true" for row in first),
        "first_attempt_call_caps": dict(sorted(caps.items())),
        "reviewer_auto_attempts": sum(row["reviewer"] == "auto" for row in rows),
        "retry_attempts": len(retries),
    }, source_hashes


def aider_metrics(path):
    data = read_json(path)
    rows = data["runs"]
    if len(rows) != 34 or {row["id"] for row in rows} != set(data["selection"]):
        raise ValueError("Aider Python 34 selection/run mismatch")
    return {
        "attempts": len(rows),
        "public_candidate_passed": sum(row["candidate_passed"] is True for row in rows),
        "strict_platform_succeeded": sum(row["status"] == "SUCCEEDED" for row in rows),
        "usage_complete": all(row["usage_status"] == "complete" for row in rows),
    }


def context_metrics(path):
    data = read_json(path)
    rows = data["runs"]
    if len(rows) != 27 or data["mode"] != "live":
        raise ValueError("Context comparison must contain 27 live attempts")
    result = {}
    for context in ("full", "compact", "managed"):
        group = [row for row in rows if row["context"] == context]
        if len(group) != 9 or len({(row["task_id"], row["repeat"]) for row in group}) != 9:
            raise ValueError(f"Incomplete context arm: {context}")
        tokens = sum(sum((row["result"].get("usage") or {}).get(k, 0) for k in
                         ("input_tokens", "output_tokens")) for row in group)
        succeeded = sum(row["status"] == "SUCCEEDED" for row in group)
        reported = data["summary"][context]
        if (succeeded != reported["succeeded"] or
                tokens != reported["input_tokens_reported"] + reported["output_tokens_reported"]):
            raise ValueError(f"Context report summary mismatch: {context}")
        result[context] = {"attempts": 9, "succeeded": succeeded, "reported_tokens": tokens}
    return result


def build(mini_csv, artifact_root, aider_runs, context_report):
    mini, hashes = mini_metrics(mini_csv, artifact_root)
    for path in (aider_runs, context_report):
        hashes[str(path)] = sha256(path)
    return {
        "schema_version": 1,
        "kind": "derived_interview_metrics_no_model_calls",
        "source_sha256": dict(sorted(hashes.items())),
        "mini_verified_50": mini,
        "aider_python_34": aider_metrics(aider_runs),
        "context_27": context_metrics(context_report),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("mini-csv", "artifact-root", "aider-runs", "context-report", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    summary = build(args.mini_csv, args.artifact_root, args.aider_runs, args.context_report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {summary['mini_verified_50']['first_attempt_issues']} Mini issues; wrote {args.output}")


if __name__ == "__main__":
    main()
