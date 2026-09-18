"""故障矩阵：按顺序跑一遍已有的故障注入检查，汇总成一张「注入什么 → 期望什么 → 观测到什么」的表。

本脚本只做编排：每条故障都对应一个已有脚本与它的证据文件，脚本需要停／起 Worker 时按它的
要求做。检查项比记录在案的少（或脚本非零退出）即为矩阵里的一行失败，并且不会中止后续行。
真实模型效果不在矩阵内：这里全部是确定性的功能与故障行为。
"""

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "runtime" / "validation"
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv").is_dir() else "python"

STEPS = [
    {"fault": "Worker 失联：租约过期且没有可用检查点", "expected": "判为中断，不再重排",
     "script": ["protocol_check.py"], "artifact": "runtime/validation/protocol.json", "minimum": 21, "workers_stopped": True},
    {"fault": "动作写入允许范围之外", "expected": "停在安全边界等待审批；批准后在新代次恢复并只执行一次",
     "script": ["approval_smoke.py"], "artifact": "runtime/validation/m3-approval.json", "minimum": 6},
    {"fault": "Worker 在执行中被顶掉", "expected": "租约过期即重排队、新代次从检查点恢复、旧尝试不写终态、无容器残留",
     "script": ["recovery_check.py"], "artifact": "runtime/validation/m3-recovery.json"},
    {"fault": "评审报告阻断项", "expected": "有限修订一轮、旧评审过期、复评通过后仍由独立验收判定",
     "script": ["review_smoke.py"], "artifact": "runtime/validation/m4-review.json", "minimum": 6},
    {"fault": "进程内没有收集端", "expected": "span 丢弃，运行不受影响，trace 仍可读回",
     "script": ["trace_check.py"], "artifact": "runtime/validation/m4-trace.json", "minimum": 4},
    {"fault": "并发模型调用超过共享配额", "expected": "峰值并发等于上限，等待者串行通过",
     "runner": ["docker", "compose", "exec", "-T", "worker", "sh", "-c", "python scripts/quota_check.py"],
     "artifact": "runtime/artifacts/validation/quota.json"},
    {"fault": "官方 harness 依赖与磁盘不足", "expected": "预检报告缺口，不假装就绪",
     "script": ["swebench.py", "preflight"], "artifact": "runtime/validation/swebench-preflight.json", "allow_exit": [0, 1]},
]


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=300)


def artifact_summary(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        if isinstance(data.get("checks"), list):
            return {"checks": len(data["checks"]), "failed": data.get("failed")}
        if "ok" in data:
            return {"ok": data["ok"], "checks": len(data.get("checks", []))}
        if "instances" in data:
            return {"instances": data["instances"], "resolved": data.get("resolved")}
        return {key: data[key] for key in list(data)[:6]}
    return {"rows": len(data)}


def run_step(step):
    artifact = ROOT / step["artifact"]
    before = artifact.stat().st_mtime if artifact.exists() else 0
    command = [PYTHON, str(ROOT / "scripts" / step["script"][0]), *step["script"][1:]] if step.get("script") \
        else step["runner"]
    if step.get("workers_stopped"):
        compose("stop", "worker")
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=1800)
        code, tail = completed.returncode, (completed.stdout or completed.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        code, tail = 124, "timed out"
    finally:
        if step.get("workers_stopped"):
            compose("up", "-d", "--scale", "worker=2", "worker")
    summary = artifact_summary(artifact)
    refreshed = artifact.exists() and artifact.stat().st_mtime > before
    minimum = step.get("minimum")
    # A step may legitimately finish non-zero when it is reporting a gap rather than a fault.
    acceptable = step.get("allow_exit", [0])
    ok = code in acceptable and refreshed and (minimum is None or summary.get("checks", 0) >= minimum)
    return {"fault": step["fault"], "expected": step["expected"], "command": " ".join(str(part) for part in command),
            "exit_code": code, "artifact": str(artifact.relative_to(ROOT)), "artifact_refreshed": refreshed,
            "seconds": round(time.monotonic() - started, 1), "observed": summary, "ok": ok, "tail": tail.strip()}


def main():
    steps = [run_step(step) for step in STEPS]
    report = {"steps": steps, "ok": all(step["ok"] for step in steps),
              "note": "确定性故障行为；不含真实模型效果评测。"}
    VALIDATION.mkdir(parents=True, exist_ok=True)
    (VALIDATION / "m5-fault-matrix.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for step in steps:
        print(f"[{'ok  ' if step['ok'] else 'FAIL'}] {step['fault']} -> {step['observed']} ({step['seconds']}s)")
    print(json.dumps({"ok": report["ok"], "failed": [step["fault"] for step in steps if not step["ok"]]},
                     ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
