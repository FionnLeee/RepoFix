"""SWE-bench 接线：实例翻译成任务、运行导出成预测、官方报告汇总成记录。

本脚本不判定任何东西。官方 harness 在官方镜像里用实例自带的测试补丁判定 FAIL_TO_PASS／
PASS_TO_PASS，RepoPilot 只负责产出补丁；两者的结论不可互换，所以这里只做翻译与汇总。

  preflight  检查链路是否就绪，并把缺口与所需资源写清楚
  prepare    把一个实例翻译成 RepoPilot 运行（--submit 直接提交）
  export     把已完成的实例运行写成官方 predictions.jsonl
  import     把官方报告汇总进本地记录

官方 harness 已在本机完成十实例样本（3/10 resolved，覆盖七个仓库）；新增 live 运行仍需有效的免费模型凭据，
preflight 会提示模型名是否在账号的免费清单里。完整的本地记录与命令见 docs/evidence/M5-swebench.md。
"""

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/agent-worker"))
from repopilot import swebench  # noqa: E402
from repopilot.model_policy import require_authorized_model  # noqa: E402

API = os.getenv("CONTROL_API_URL", "http://localhost:3101")
CACHE = Path(os.getenv("SWEBENCH_CACHE", str(ROOT / "runtime" / "swebench")))
VALIDATION = ROOT / "runtime" / "validation"
HARNESS_IMAGE = os.getenv("SWEBENCH_IMAGE", "repopilot-swebench")
SAMPLE = {
    "instance_id": "sample__sample-1", "repo": "psf/requests", "base_commit": "0" * 40,
    "problem_statement": "示例：用本地缓存的实例文件替换它。",
    "image": "swebench/sweb.eval.x86_64.psf_1776_requests-3362:latest",
    "patch": "diff --git a/requests/models.py b/requests/models.py\n--- a/requests/models.py\n+++ b/requests/models.py\n",
    "test_patch": "diff --git a/tests/test_requests.py b/tests/test_requests.py\n",
    "FAIL_TO_PASS": '["tests/test_requests.py::TestRequests::test_x"]', "PASS_TO_PASS": "[]",
}


def api(path, payload=None):
    request = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"content-type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def cached_instances():
    if not CACHE.is_dir():
        return []
    return sorted(path.stem for path in CACHE.glob("*.json"))


def load_instance(instance_id):
    path = CACHE / f"{instance_id}.json"
    if not path.is_file():
        raise SystemExit(f"缺少本地实例文件 {path}；先按 docs/evidence/M5-swebench.md 里的命令缓存实例。")
    return json.loads(path.read_text(encoding="utf-8"))


def installed(package, python=None):
    """Ask the distribution, not the import system: this file is itself named swebench.py."""
    if python and python != sys.executable:
        probe = subprocess.run([python, "-c", f"import importlib.metadata as m; print(m.version('{package}'))"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        return probe.stdout.strip() if probe.returncode == 0 else None
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def harness_python():
    """The interpreter that runs the official harness, which usually is not this one."""
    candidates = [os.getenv("SWEBENCH_PYTHON")]
    workspace = ROOT.parents[3] if len(ROOT.parents) > 3 else None
    if workspace:
        candidates.append(str(workspace / ".venvs" / "swebench" / "Scripts" / "python.exe"))
        candidates.append(str(workspace / ".venvs" / "swebench" / "bin" / "python"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return sys.executable


def docker_out(*args):
    try:
        completed = subprocess.run(["docker", *args], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120)
        return completed.returncode, completed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def configured_model():
    """The model the worker will really use: .env decides, not whatever this shell exports."""
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("MODEL_NAME="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return os.getenv("MODEL_NAME", "")


def preflight():
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        health = api("/health")
        check("control_plane", True, json.dumps(health, ensure_ascii=False))
    except (urllib.error.URLError, OSError) as error:
        check("control_plane", False, str(error))
    try:
        task = swebench.task_from_instance(SAMPLE)
        check("instance_translation", task["spec"]["verificationMode"] == "harness",
              f"allowedPaths={task['spec']['allowedPaths']}")
    except Exception as error:  # noqa: BLE001 - the report must survive any failure
        check("instance_translation", False, str(error))
    # The official harness runs as a Linux process: on Windows it writes CRLF evaluation
    # scripts, which the container's shell reads as part of every test selector.
    code, _ = docker_out("image", "inspect", HARNESS_IMAGE)
    python = harness_python()
    host_version = installed("swebench", python)
    check("official_harness", code == 0 or host_version is not None,
          f"镜像 {HARNESS_IMAGE} 可用" if code == 0
          else f"没有镜像 {HARNESS_IMAGE}；宿主环境有 swebench {host_version}，但 Windows 上不要用它跑评测"
          if host_version else f"既没有镜像 {HARNESS_IMAGE}，宿主也没有 swebench")
    cache = CACHE / "swe-bench-lite-test.json"
    try:
        rows = len(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        rows = 0
    check("local_dataset", rows > 0,
          f"{cache.relative_to(ROOT)} 有 {rows} 个实例" if rows
          else f"缺少 {cache.relative_to(ROOT)}（harness 读本地文件时要 JSON 数组，容器里连不上 Hub）")
    cached = cached_instances()
    check("instance_cache", bool(cached), f"{len(cached)} 个本地实例：{', '.join(cached[:5])}" if cached
          else f"{CACHE} 为空；缓存实例 JSON 后 prepare 才能用")
    # The endpoint's catalogue lists what can be called, not what is free: only the model names
    # the account's own quota page covers are free, and everything else is billed by usage.
    model = configured_model()
    from dotenv import dotenv_values
    settings = dotenv_values(ROOT / ".env")
    policy = settings.get("MODEL_POLICY", "free-quota")
    try:
        require_authorized_model(model, settings.get("MODEL_BASE_URL", ""), policy)
        check("live_model_name", True, f"MODEL_NAME={model}；授权策略={policy}（official-deepseek 按官方价格计费）")
    except ValueError as error:
        check("live_model_name", False, str(error))
    code, listing = docker_out("images", "--filter", "reference=swebench/sweb.eval.*", "--format", "{{.Size}}")
    free = shutil.disk_usage(ROOT).free / 1e9
    check("disk_headroom", free >= 10,
          f"{free:.1f} GB 可用，已缓存 {len(listing.splitlines())} 个实例镜像；单实例落盘约 4.2 GB，"
          "固定子集需要先腾出数十 GB")
    check("delegated_verification_path", True,
          "由 pytest 集成用例覆盖（test_a_harness_verified_run_delivers_a_patch_without_local_acceptance）")
    advisory = {"disk_headroom"}
    report = {"checks": checks, "ok": all(entry["ok"] for entry in checks if entry["name"] not in advisory),
              "note": "本报告只说明接线是否就绪；模型成绩需要真实运行后才有。"}
    VALIDATION.mkdir(parents=True, exist_ok=True)
    (VALIDATION / "swebench-preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def prepare(args):
    instance = json.loads(Path(args.from_file).read_text(encoding="utf-8")) if args.from_file \
        else load_instance(args.instance)
    paths = [p.strip() for p in args.allowed_paths.split(",") if p.strip()] or None
    payload = swebench.task_from_instance(instance, allowed_paths=paths, context_mode=args.context_mode,
                                          review_policy=args.review_policy, workspace_mode=args.workspace_mode)
    payload["requestKey"] = f"swebench-{instance['instance_id']}-{uuid.uuid4()}"
    if not args.submit:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return {"submitted": False, "instance_id": instance["instance_id"]}
    run = api("/runs", payload)
    return {"submitted": True, "instance_id": instance["instance_id"], "run_id": run["id"]}


def export(args):
    runs = [api(f"/runs/{run_id}") for run_id in args.run_id]
    rows, skipped = swebench.export_predictions(runs, args.model)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    manifest = {"predictions": len(rows), "skipped": skipped, "model_name_or_path": args.model,
                "file": str(target.resolve().relative_to(ROOT))}
    (VALIDATION / "swebench-predictions.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                          encoding="utf-8")
    return manifest


def import_report(args):
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    summary = swebench.summarise_report(report)
    summary["source"] = str(Path(args.report))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--instance")
    prepare_parser.add_argument("--from-file")
    prepare_parser.add_argument("--allowed-paths", default="")
    prepare_parser.add_argument("--context-mode", default="full")
    prepare_parser.add_argument("--workspace-mode", default="image", choices=["image", "snapshot"])
    prepare_parser.add_argument("--review-policy", default="auto")
    prepare_parser.add_argument("--submit", action="store_true")
    export_parser = sub.add_parser("export")
    export_parser.add_argument("--run-id", action="append", required=True,
                               help="精确选择本轮 run ID；可重复，不能按历史非空补丁挑选")
    export_parser.add_argument("--out", default=str(VALIDATION / "swebench-predictions.jsonl"))
    export_parser.add_argument("--model", default="repopilot")
    import_parser = sub.add_parser("import")
    import_parser.add_argument("--report", required=True)
    import_parser.add_argument("--out", default=str(VALIDATION / "swebench-report.json"))
    args = parser.parse_args()
    if args.command == "preflight":
        report = preflight()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "prepare":
        if not args.instance and not args.from_file:
            parser.error("prepare 需要 --instance 或 --from-file")
        print(json.dumps(prepare(args), ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        print(json.dumps(export(args), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(import_report(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
