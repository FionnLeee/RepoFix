"""Freeze and execute paired Reviewer on/off SWE-bench attempts, with no gold-derived inputs.

The local cached-image sample is a convenience sample, not a public leaderboard estimate.
All original instances remain in the denominator. Provider failures have no automatic retry.
"""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import swebench as cli
import swebench_subset as subset
from repopilot import swebench
from repopilot.model_policy import require_authorized_model


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def docker_json(*args):
    return json.loads(subprocess.check_output(["docker", *args], text=True, encoding="utf-8"))


def worker_config():
    ids = subprocess.check_output(["docker", "compose", "ps", "-q", "worker"], cwd=cli.ROOT,
                                   text=True).split()
    if not ids:
        raise ValueError("No deployed workers")
    configs = []
    for item in docker_json("inspect", *ids):
        env = dict(value.split("=", 1) for value in item["Config"]["Env"])
        policy = env.get("MODEL_POLICY", "free-quota")
        configs.append({"image": item["Image"], "model": require_authorized_model(
                            env["MODEL_NAME"], env["MODEL_BASE_URL"], policy), "model_policy": policy,
                        "thinking": "disabled" if policy == "official-deepseek" else "provider-default",
                        "endpoint_sha256": hashlib.sha256(env["MODEL_BASE_URL"].encode()).hexdigest()})
    if any(item != configs[0] for item in configs):
        raise ValueError("Workers have different images/model configurations")
    return configs[0]


def freeze(instances, folder):
    if len(set(instances)) != len(instances):
        raise ValueError("Duplicate instances")
    config = worker_config()
    cases = []
    for index, instance in enumerate(instances):
        payload = swebench.task_from_instance(cli.load_instance(instance))
        assert payload["spec"]["allowedPaths"] == []
        payload.update(reviewBudget="shared", reviewRounds=2, approvalPolicy="auto")
        image = docker_json("image", "inspect", payload["spec"]["sandboxImage"])[0]["Id"]
        cases.append({"instance_id": instance, "payload": payload, "payload_sha256": sha(payload),
                      "image_id": image, "order": ["off", "auto"] if index % 2 == 0 else ["auto", "off"],
                      "request_keys": {arm: str(uuid.uuid4()) for arm in ("off", "auto")}})
    frozen = {"schema": 1, "batch_id": folder.name, "created_at": time.time(), "worker": config,
              "selection": "explicit locally cached SWE-bench Lite instance images, convenience sample",
              "gold_screening": False, "hidden_test_hints": False, "memory_enabled": False,
              "shared_limits": {"total_coder_reviewer_calls": 60, "wall_seconds": 900,
                                "max_output_tokens_per_call": 1600},
              "retry_policy": "none; provider failures retained separately and in original denominator",
              "cases": cases}
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "frozen.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    return frozen


def execute(folder):
    frozen = json.loads((folder / "frozen.json").read_text(encoding="utf-8"))
    if worker_config() != frozen["worker"]:
        raise ValueError("Deployed model/image differs from frozen manifest")
    target = folder / "manifest.json"
    report = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {
        "batch_id": frozen["batch_id"], "frozen_sha256": sha(frozen), "attempts": [], "arms": {}}
    if report["frozen_sha256"] != sha(frozen):
        raise ValueError("Frozen manifest changed")

    def save():
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    save()
    # Submit the fixed schedule up front. Existing request keys make a controller restart
    # idempotent; workers still obey the shared Redis concurrency cap.
    for case in frozen["cases"]:
        for arm in case["order"]:
            if any(row["instance_id"] == case["instance_id"] and row["arm"] == arm for row in report["attempts"]):
                continue
            payload = {**case["payload"], "reviewPolicy": arm, "requestKey": case["request_keys"][arm]}
            detail = cli.api("/runs", payload)
            report["attempts"].append({"instance_id": case["instance_id"], "arm": arm, "run_id": detail["id"]})
            save()
    for case in frozen["cases"]:
        if docker_json("image", "inspect", case["payload"]["spec"]["sandboxImage"])[0]["Id"] != case["image_id"]:
            raise ValueError("Instance image changed")
        for arm in case["order"]:
            row = next((row for row in report["attempts"] if row["instance_id"] == case["instance_id"] and row["arm"] == arm), None)
            if row is None:
                payload = {**case["payload"], "reviewPolicy": arm, "requestKey": case["request_keys"][arm]}
                detail = cli.api("/runs", payload)
                row = {"instance_id": case["instance_id"], "arm": arm, "run_id": detail["id"]}
                report["attempts"].append(row)
                save()
            deadline = time.monotonic() + 1800
            while True:
                detail = cli.api("/runs/" + row["run_id"])
                if detail["status"] in swebench.TERMINAL:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Attempt still active; resume this exact manifest later")
                time.sleep(10)
            result = detail.get("result") or {}
            row.update(status=detail["status"], result=result)
            (folder / (row["run_id"] + ".json")).write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            save()
            print(json.dumps({"instance": row["instance_id"], "arm": arm, "status": row["status"],
                              "calls": result.get("model_calls"), "error": result.get("error", "")[:160]}, ensure_ascii=False), flush=True)
            if "free quotas" in result.get("error", ""):
                raise ValueError("Model allowlist refused the run")
            if result.get("provenance") and result["provenance"].get("model") != frozen["worker"]["model"]:
                raise ValueError("Actual model differs from frozen model")
    judge(folder)


def judge(folder):
    """Offline evaluation only: never submits a run or requests a model."""
    frozen = json.loads((folder / "frozen.json").read_text(encoding="utf-8"))
    target = folder / "manifest.json"
    report = json.loads(target.read_text(encoding="utf-8"))
    if report["frozen_sha256"] != sha(frozen):
        raise ValueError("Frozen manifest changed")
    instances = [case["instance_id"] for case in frozen["cases"]]
    for arm in ("off", "auto"):
        if arm in report["arms"]:
            subset.publish_evaluations(report["arms"][arm])
            continue
        rows = [row for row in report["attempts"] if row["arm"] == arm]
        if len(rows) != len(instances) or {row["instance_id"] for row in rows} != set(instances):
            raise ValueError("Each frozen instance needs exactly one attempt per arm")
        predictions, skipped, bindings = swebench.batch_predictions({row["instance_id"]: row["run_id"] for row in rows}, cli.api)
        prediction_path = folder / (arm + ".jsonl")
        prediction_path.write_text("".join(json.dumps(row) + "\n" for row in predictions), encoding="utf-8")
        arm_report = {"batch_id": frozen["batch_id"] + "-" + arm, "bindings": bindings, "skipped": skipped}
        (folder / (arm + "-bindings.json")).write_text(json.dumps(arm_report, indent=2), encoding="utf-8")
        verdict = subset.harness(instances, prediction_path, arm_report["batch_id"], report_dir=folder)
        arm_report["verdict"] = verdict
        report["arms"][arm] = arm_report
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        subset.publish_evaluations(arm_report)
    print(json.dumps({arm: value["verdict"].get("resolved_instances") for arm, value in report["arms"].items()}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", nargs="+")
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--judge-only", action="store_true")
    args = parser.parse_args()
    if args.freeze:
        freeze(args.freeze, args.folder)
    if args.run:
        execute(args.folder)
    elif args.judge_only:
        judge(args.folder)
