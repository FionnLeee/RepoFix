"""Host-side patch executor: survey a target checkout, register it for approval, then apply.

Runs on the person's machine, never inside the platform. ``prepare`` writes nothing to the
target; ``apply`` writes only after the page approval and only while the target still matches
the surveyed version. Executor state lives in runtime/deliveries/<delivery-id>.json.
"""

import argparse
import hashlib
import json
import secrets
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/agent-worker"))
from repopilot.delivery import Checkout, DeliveryError, deliver, survey  # noqa: E402

STATE_DIR = ROOT / "runtime/deliveries"
TERMINAL = ("REJECTED", "APPLIED", "INVALIDATED", "NEEDS_ATTENTION")


def fail(message: str, code: int = 2):
    print(f"错误：{message}", file=sys.stderr)
    sys.exit(code)


def api(client: httpx.Client, method: str, path: str, payload=None):
    response = client.request(method, path, json=payload)
    if response.status_code >= 400:
        try:
            message = response.json().get("message")
        except ValueError:
            message = response.text
        fail(f"{method} {path} -> {response.status_code}: {message}")
    return response.json()


def load_run(client: httpx.Client, run_id: str) -> dict:
    run = api(client, "GET", f"/runs/{run_id}")
    spec, result = run.get("spec") or {}, run.get("result") or {}
    if not spec.get("commit"):
        fail("只有固定版本的仓库任务可以交付")
    if run["status"] != "SUCCEEDED" or not result.get("patch"):
        fail(f"任务状态 {run['status']}，没有可交付的非空补丁")
    return run


def prepare(args):
    client = httpx.Client(base_url=args.api, timeout=30)
    run = load_run(client, args.run)
    spec, patch = run["spec"], run["result"]["patch"]
    try:
        checkout = Checkout(Path(args.target))
        report = survey(checkout, spec["commit"], patch, spec.get("subdir") or "")
        if report["state"] == "base":
            checkout.apply(patch, spec.get("subdir") or "", check_only=True)
    except DeliveryError as error:
        fail(str(error))
    if report["state"] == "applied":
        print("目标已经包含这份候选补丁的全部内容，无需交付。")
        return
    if report["state"] == "mixed":
        fail("受影响的文件既不是基线版本也不是候选版本，请先手动处理目标中的改动：\n  "
             + "\n  ".join(f"{f['path']}: 当前 {report['observed'][f['path']] or '不存在'}" for f in report["files"]))
    token, delivery_id = secrets.token_hex(32), str(uuid.uuid4())
    body = {"id": delivery_id, "targetPath": str(checkout.root), "targetHead": report["head"],
            "baseCommit": spec["commit"], "patchSha256": hashlib.sha256(patch.encode()).hexdigest(),
            "targetFingerprint": report["fingerprint"], "executorSha256": hashlib.sha256(token.encode()).hexdigest(),
            "files": report["files"]}
    row = api(client, "POST", f"/runs/{run['id']}/deliveries", body)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {"delivery_id": delivery_id, "run_id": run["id"], "api": args.api, "target": str(checkout.root),
             "subdir": spec.get("subdir") or "", "base_commit": spec["commit"], "patch_sha256": body["patchSha256"],
             "target_fingerprint": report["fingerprint"], "files": report["files"], "executor_token": token}
    (STATE_DIR / f"{delivery_id}.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"已登记交付 {delivery_id}（状态 {row['status']}，{row['expiresAt']} 前有效）")
    print(f"  目标：{checkout.root}  HEAD {report['head'][:12]}"
          + ("" if report["head"] == spec["commit"] else f"（与基线 {spec['commit'][:12]} 不同，但受影响文件与基线一致）"))
    for file in report["files"]:
        print(f"  {file['change']:<9}{file['path']}")
    print(f"请在页面（{args.web}/?run={run['id']}）核对 diff 与目标指纹后批准，然后运行：")
    print(f"  python scripts/deliver.py apply --delivery {delivery_id}")


def load_state(delivery_id: str) -> dict:
    path = STATE_DIR / f"{delivery_id}.json"
    if not path.exists():
        fail(f"本机没有交付 {delivery_id} 的执行器记录；只有登记它的机器可以应用")
    return json.loads(path.read_text(encoding="utf-8"))


def apply(args):
    state = load_state(args.delivery)
    client = httpx.Client(base_url=state["api"], timeout=30)
    deadline = time.monotonic() + args.wait
    row = api(client, "GET", f"/deliveries/{state['delivery_id']}")
    while row["status"] == "PENDING":
        if time.monotonic() >= deadline:
            fail("交付尚未批准；在页面批准后再运行，或加 --wait 秒数等待", code=3)
        time.sleep(3)
        row = api(client, "GET", f"/deliveries/{state['delivery_id']}")
    if row["status"] == "APPLIED":
        print("该交付已经应用过，不再重复写入。")
        return
    if row["status"] in TERMINAL:
        fail(f"交付已结束：{row['status']} {json.dumps(row.get('receipt'), ensure_ascii=False)}", code=1)
    if row["status"] == "APPROVED":
        row = api(client, "POST", f"/deliveries/{state['delivery_id']}/claim", {"executorToken": state["executor_token"]})
        if row["status"] == "APPLIED":
            print("该交付已经应用过，不再重复写入。")
            return
    if row["status"] != "APPLYING":
        fail(f"交付状态 {row['status']} 不允许应用", code=1)
    run = load_run(client, state["run_id"])
    patch = run["result"]["patch"]
    if hashlib.sha256(patch.encode()).hexdigest() != state["patch_sha256"] or state["patch_sha256"] != row["patchSha256"]:
        outcome = {"status": "INVALIDATED", "receipt": {"reason": "任务补丁与登记时不一致，未写入任何文件"}}
    else:
        try:
            outcome = deliver(Checkout(Path(state["target"])), patch, state["subdir"], state["files"],
                              row["targetFingerprint"])
        except DeliveryError as error:
            outcome = {"status": "NEEDS_ATTENTION", "receipt": {"reason": f"读取目标失败：{error}"}}
    final = api(client, "POST", f"/deliveries/{state['delivery_id']}/finish",
                {"executorToken": state["executor_token"], **outcome})
    print(f"交付 {state['delivery_id']}：{final['status']}")
    print(json.dumps(final.get("receipt"), ensure_ascii=False, indent=2))
    if final["status"] != "APPLIED":
        sys.exit(1)


def status(args):
    state = load_state(args.delivery)
    row = httpx.Client(base_url=state["api"], timeout=30).get(f"/deliveries/{state['delivery_id']}").raise_for_status().json()
    print(json.dumps(row, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="核对目标 checkout 并登记等待审批的交付")
    p.add_argument("--run", required=True)
    p.add_argument("--target", required=True, help="目标 Git 仓库根目录")
    p.add_argument("--api", default="http://localhost:3101")
    p.add_argument("--web", default="http://localhost:3100")
    p.set_defaults(func=prepare)
    a = commands.add_parser("apply", help="在页面批准后应用补丁；重复运行不会重复写入")
    a.add_argument("--delivery", required=True)
    a.add_argument("--wait", type=float, default=0, help="等待审批的秒数")
    a.set_defaults(func=apply)
    s = commands.add_parser("status")
    s.add_argument("--delivery", required=True)
    s.set_defaults(func=status)
    parsed = parser.parse_args()
    parsed.func(parsed)
