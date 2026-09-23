"""跑一个可复现的 SWE-bench 子集：筛环境 → 跑 RepoPilot → 官方判定 → 出表。

一条命令走完整条链路，避免手工四步（也避免手工步骤带来的挑选空间）：

  python scripts/swebench_subset.py --instances a b c            # 全流程
  python scripts/swebench_subset.py --instances a b c --screen-only

流程与口径：

1. 镜像不存在就拉取（每个约 4 GB，磁盘是主要约束）。
2. **gold 筛选**：先用实例自带的 gold 补丁跑一遍官方 harness。未通过的实例单列为
   本机暂不可评测；这不证明任何其他补丁都不可能通过。
3. 对筛过的实例各提交一次 RepoPilot 运行（真实模型），等它们到终态。
4. 导出预测：没产出补丁的尝试按"空提交"计入（官方 harness 自己会把它判为未解决）。
5. 一条 harness 命令判定全部实例，报告写入 runtime/validation/。

当前运行不从 gold 或 FAIL_TO_PASS 生成 Agent 提示。样本按本机可评测性筛选，
仍须列出原始样本、排除项、预算与重试；历史带提示的 3/10 不代表当前配置成绩。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/agent-worker"))
sys.path.insert(0, str(ROOT / "scripts"))
import swebench as cli  # noqa: E402  (this directory's CLI helpers)
from repopilot import swebench  # noqa: E402

CACHE = cli.CACHE
VALIDATION = cli.VALIDATION
HARNESS_IMAGE = cli.HARNESS_IMAGE


def rel(path):
    """A path the harness sees inside its container, which mounts the repo at /work."""
    return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")


def harness(instance_ids, predictions, run_id, extra=(), report_dir=None, dataset_json=None):
    """Run the official harness in its Linux container over the given instances."""
    report_dir = report_dir or VALIDATION
    dataset_json = Path(dataset_json) if dataset_json else ROOT / "runtime/swebench/swe-bench-lite-test.json"
    command = ["docker", "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", f"{ROOT}:/work", "-w", "/work", HARNESS_IMAGE,
               "--dataset_name", rel(dataset_json),
               "--predictions_path", rel(predictions), "--max_workers", "1", "--run_id", run_id,
               "--instance_ids", *instance_ids, "--report_dir", rel(report_dir), *extra]
    print(f"[harness] {run_id}: {len(instance_ids)} instance(s)", flush=True)
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=7200,
                               env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    found = sorted(report_dir.glob(f"*.{run_id}.json"))
    report = found[0] if found else None
    if completed.returncode or report is None:
        raise SystemExit(f"harness failed for {run_id}: {completed.stdout[-400:]}{completed.stderr[-400:]}")
    return json.loads(report.read_text(encoding="utf-8"))


def ensure_images(instances):
    pulled, failed = [], []
    for instance_id in instances:
        row = json.loads((CACHE / f"{instance_id}.json").read_text(encoding="utf-8"))
        image = row["image"]
        code, _ = cli.docker_out("image", "inspect", image)
        if code == 0:
            continue
        print(f"[pull] {image}", flush=True)
        code, output = cli.docker_out("pull", "-q", image)
        if code:
            print(f"[pull failed] {instance_id}: {output[-200:]}", flush=True)
        (pulled if code == 0 else failed).append(instance_id)
    return pulled, failed


def gold_predictions(instances, target):
    lines = []
    for instance_id in instances:
        row = json.loads((CACHE / f"{instance_id}.json").read_text(encoding="utf-8"))
        lines.append(json.dumps({"instance_id": instance_id, "model_name_or_path": "repopilot-preflight-gold",
                                 "model_patch": row["patch"]}, ensure_ascii=False))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


INFRA_MARKERS = ("Connection error", "InternalServerError", "ServiceUnavailable", "Timeout",
                 "APIConnectionError", "RateLimitError", "502", "503", "504")


def infrastructure_failure(run):
    """A provider outage is not an agent outcome: it must be retried or reported, never scored."""
    if run.get("status") != "FAILED":
        return None
    error = str((run.get("result") or {}).get("error") or "")
    return error if any(marker in error for marker in INFRA_MARKERS) else None


def wait_for_runs(run_ids, limit=3600):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        states = {run_id: cli.api(f"/runs/{run_id}")["status"] for run_id in run_ids}
        pending = [run_id for run_id, status in states.items() if status not in swebench.TERMINAL]
        print(f"[runs] {len(run_ids) - len(pending)}/{len(run_ids)} finished", flush=True)
        if not pending:
            return states
        time.sleep(30)
    raise SystemExit("runs did not finish in time")


def publish_evaluations(report):
    verdict = report["verdict"]
    resolved = set(verdict.get("resolved_ids") or [])
    unresolved = set(verdict.get("unresolved_ids") or []) | set(verdict.get("empty_patch_ids") or [])
    errors = set(verdict.get("error_ids") or [])
    for binding in report.get("bindings", []):
        instance = binding["instance_id"]
        memberships = [instance in ids for ids in (resolved, unresolved, errors)]
        if sum(memberships) != 1:
            raise ValueError(f"Official report has no unique verdict for {instance}")
        status = "resolved" if memberships[0] else "unresolved" if memberships[1] else "infra_failed"
        cli.api(f"/runs/{binding['run_id']}/evaluation", {
            "instanceId": instance, "patchSha256": binding["patch_sha256"],
            "status": status, "batchId": report["batch_id"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instances", nargs="+")
    parser.add_argument("--publish-batch", type=Path, help="只导入已归档 manifest 的官方结果，不调用模型")
    parser.add_argument("--screen-only", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=None, help="review revision budget for each run")
    args = parser.parse_args()

    if args.publish_batch:
        publish_evaluations(json.loads(args.publish_batch.read_text(encoding="utf-8")))
        return 0
    if not args.instances:
        parser.error("需要 --instances 或 --publish-batch")

    if len(set(args.instances)) != len(args.instances):
        parser.error("实例不能重复")
    batch_id = "subset-" + uuid.uuid4().hex
    folder = VALIDATION / batch_id
    folder.mkdir(parents=True)
    report = {"batch_id": batch_id, "instances": args.instances, "screen": {}, "runs": {},
              "attempts": {}, "verdict": {}, "generative_model_calls": None}

    def save():
        (folder / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    pulled, pull_failed = ensure_images(args.instances)
    report["pulled_images"] = pulled
    report["image_failures"] = pull_failed

    screened = [i for i in args.instances if i not in pull_failed]
    gold = (harness(screened, gold_predictions(screened, folder / "gold.jsonl"),
                    batch_id + "-gold", report_dir=folder) if screened else {"resolved_ids": []})
    screenable = gold["resolved_ids"]
    blocked = sorted(set(screened) - set(screenable))
    report["screen"] = {"screenable": screenable, "blocked": blocked,
                        "note": "gold 未通过者单列；原始样本、排除项和可评测样本均保留"}
    print(f"[screen] screenable={screenable} blocked={blocked}", flush=True)
    if args.screen_only or not screenable:
        save()
        print(json.dumps(report, ensure_ascii=False)[:400])
        return 0

    def submit(instance_id):
        payload = swebench.task_from_instance(
            json.loads((CACHE / f"{instance_id}.json").read_text(encoding="utf-8")))
        if args.max_rounds is not None:
            payload["reviewRounds"] = args.max_rounds
        payload["requestKey"] = str(uuid.uuid4())
        run_id = cli.api("/runs", payload)["id"]
        report["attempts"].setdefault(instance_id, []).append(run_id)
        report["runs"][instance_id] = run_id
        save()
        return run_id

    run_ids = {instance_id: submit(instance_id) for instance_id in screenable}
    states = wait_for_runs(list(run_ids.values()))
    # A provider outage is not an agent outcome. Retry once, then report it separately instead
    # of scoring it as an attempt that produced nothing.
    infra = {}
    for instance_id, run_id in list(run_ids.items()):
        detail = cli.api(f"/runs/{run_id}")
        reason = infrastructure_failure(detail)
        if reason:
            print(f"[retry] {instance_id}: {reason[:80]}", flush=True)
            run_ids[instance_id] = submit(instance_id)
            report.setdefault("retried", {})[instance_id] = reason[:200]
    if report.get("retried"):
        states = wait_for_runs(list(run_ids.values()))
        for instance_id, run_id in run_ids.items():
            reason = infrastructure_failure(cli.api(f"/runs/{run_id}"))
            if reason:
                infra[instance_id] = reason[:200]
    report["infra_failed"] = infra
    judged = [instance_id for instance_id in screenable if instance_id not in infra]
    report["judged"] = judged
    report["runs"] = dict(run_ids)
    states = {run_id: cli.api(f"/runs/{run_id}")["status"] for run_id in run_ids.values()}

    usage = []
    for attempts in report["attempts"].values():
        for run_id in attempts:
            result = cli.api(f"/runs/{run_id}").get("result") or {}
            usage.append({"run_id": run_id, **{k: result.get(k) for k in
                          ("model_calls", "usage", "usage_status", "usage_calls_reported")}})
    report["attempt_usage"] = usage
    calls = [row["model_calls"] for row in usage]
    known = [value for value in calls if type(value) is int]
    report["known_model_calls"] = sum(known)
    report["generative_model_calls"] = sum(known) if len(known) == len(calls) else None

    predictions = folder / "predictions.jsonl"
    rows, skipped, bindings = swebench.batch_predictions({i: run_ids[i] for i in judged}, cli.api)
    report["bindings"] = bindings
    predictions.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    report["predictions"] = {"file": str(predictions.relative_to(ROOT)), "rows": len(rows),
                             "empty_submissions": [row["instance_id"] for row in rows if not row["model_patch"]],
                             "skipped": [entry for entry in skipped if entry["instance_id"] in judged]}
    save()
    verdict = {} if not judged else harness(judged, predictions, batch_id, report_dir=folder)
    report["verdict"] = {"note": "判定的分母里不含 gold 过不了的实例与基础设施失败的实例"} if not verdict else {key: verdict.get(key) for key in
                         ("total_instances", "resolved_instances", "unresolved_instances",
                          "empty_patch_instances", "error_instances",
                          "resolved_ids", "unresolved_ids", "empty_patch_ids", "error_ids", "failure_reasons")}
    report["run_states"] = states
    save()
    if judged:
        publish_evaluations(report)
    print(json.dumps(report["verdict"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
