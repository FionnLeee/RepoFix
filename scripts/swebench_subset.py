"""跑一个可复现的 SWE-bench 子集：筛环境 → 跑 RepoPilot → 官方判定 → 出表。

一条命令走完整条链路，避免手工四步（也避免手工步骤带来的挑选空间）：

  python scripts/swebench_subset.py --instances a b c            # 全流程
  python scripts/swebench_subset.py --instances a b c --screen-only

流程与口径：

1. 镜像不存在就拉取（每个约 4 GB，磁盘是主要约束）。
2. **gold 筛选**：先用实例自带的 gold 补丁跑一遍官方 harness。gold 在本机都过不了的实例，
   任何补丁都不可能被判 resolved，纳入进去只会污染分母——这类实例单列并写明理由。
3. 对筛过的实例各提交一次 RepoPilot 运行（真实模型），等它们到终态。
4. 导出预测：没产出补丁的尝试按"空提交"计入（官方 harness 自己会把它判为未解决）。
5. 一条 harness 命令判定全部实例，报告写入 runtime/validation/。

必须随数字一起写的口径：`allowedPaths` 取自 gold 补丁的文件列表，等于给了执行者提示，
所以结果与公开榜单不可比；样本按"环境可评测性"筛选，不按难度；每实例默认只跑一次。
"""

import argparse
import json
import os
import subprocess
import sys
import time
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


def harness(instance_ids, predictions, run_id, extra=()):
    """Run the official harness in its Linux container over the given instances."""
    command = ["docker", "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", f"{ROOT}:/work", "-w", "/work", HARNESS_IMAGE,
               "--dataset_name", "runtime/swebench/swe-bench-lite-test.json",
               "--predictions_path", rel(predictions), "--max_workers", "1", "--run_id", run_id,
               "--instance_ids", *instance_ids, "--report_dir", rel(VALIDATION), *extra]
    print(f"[harness] {run_id}: {len(instance_ids)} instance(s)", flush=True)
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=7200,
                               env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    found = sorted(VALIDATION.glob(f"*.{run_id}.json"))  # the harness names it <model>.<run_id>.json
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
        pending = [run_id for run_id, status in states.items() if status in ("QUEUED", "RUNNING", "WAITING_APPROVAL")]
        print(f"[runs] {len(run_ids) - len(pending)}/{len(run_ids)} finished", flush=True)
        if not pending:
            return states
        time.sleep(30)
    raise SystemExit("runs did not finish in time")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instances", nargs="+", required=True)
    parser.add_argument("--screen-only", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=None, help="review revision budget for each run")
    args = parser.parse_args()

    report = {"instances": args.instances, "screen": {}, "runs": {}, "verdict": {}, "generative_model_calls": 0}
    pulled, pull_failed = ensure_images(args.instances)
    report["pulled_images"] = pulled
    report["image_failures"] = pull_failed

    screened = [i for i in args.instances if i not in pull_failed]
    gold = harness(screened, gold_predictions(screened, VALIDATION / "swebench-subset-gold.jsonl"),
                   "repopilot-subset-gold")
    screenable = gold["resolved_ids"]
    blocked = sorted(set(screened) - set(screenable))
    report["screen"] = {"screenable": screenable, "blocked": blocked,
                        "note": "gold 在本机也过不了的实例不计入分母；理由见 evidence/M5-swebench.md"}
    print(f"[screen] screenable={screenable} blocked={blocked}", flush=True)
    if args.screen_only or not screenable:
        (VALIDATION / "swebench-subset.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False)[:400])
        return 0

    def submit(instance_id):
        payload = swebench.task_from_instance(
            json.loads((CACHE / f"{instance_id}.json").read_text(encoding="utf-8")))
        if args.max_rounds is not None:
            payload["reviewRounds"] = args.max_rounds
        payload["requestKey"] = f"subset-{instance_id}-{int(time.time() * 1000)}"
        return cli.api("/runs", payload)["id"]

    run_ids = {instance_id: submit(instance_id) for instance_id in screenable}
    report["runs"] = dict(run_ids)
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
    states = {run_id: cli.api(f"/runs/{run_id}")["status"] for run_id in run_ids.values()}

    predictions = VALIDATION / "swebench-subset-predictions.jsonl"
    rows, skipped = swebench.export_predictions(cli.api("/runs"), "repopilot")
    rows = [row for row in rows if row["instance_id"] in judged]
    predictions.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    report["predictions"] = {"file": str(predictions.relative_to(ROOT)), "rows": len(rows),
                             "empty_submissions": [row["instance_id"] for row in rows if not row["model_patch"]],
                             "skipped": [entry for entry in skipped if entry["instance_id"] in judged]}
    verdict = {} if not judged else harness(judged, predictions, "repopilot-subset")
    report["verdict"] = {"note": "判定的分母里不含 gold 过不了的实例与基础设施失败的实例"} if not verdict else {key: verdict.get(key) for key in
                         ("total_instances", "resolved_instances", "unresolved_instances",
                          "empty_patch_instances", "error_instances",
                          "resolved_ids", "unresolved_ids", "empty_patch_ids", "failure_reasons")}
    report["run_states"] = states
    (VALIDATION / "swebench-subset.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["verdict"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
