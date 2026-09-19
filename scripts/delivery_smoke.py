"""End-to-end delivery smoke: a deterministic demo run's patch lands in a real target checkout.

Needs the deployed stack with a worker running. Uses the built-in demo task only (no model
credits). The target is a fresh clone-equivalent of the baseline repository under runtime/,
with unrelated uncommitted edits that must survive the delivery untouched.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "scripts"))
from prepare_baselines import build_repository  # noqa: E402

client = httpx.Client(base_url="http://localhost:3101", timeout=30)
catalog = json.loads((root / "runtime/baseline-catalog.json").read_text(encoding="utf-8"))
task = next(t for t in catalog if t["id"] == "checkout")
target = root / "runtime/delivery-target"
checks = []


def deliver(*args):
    result = subprocess.run([sys.executable, str(root / "scripts/deliver.py"), *args], capture_output=True,
                            text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    return result.returncode, result.stdout + result.stderr


def git(*args):
    return subprocess.check_output(["git", "-C", str(target), *args], text=True, encoding="utf-8")


def delivery_rows(run_id):
    return client.get(f"/runs/{run_id}/deliveries").raise_for_status().json()


shutil.rmtree(target, ignore_errors=True)
target.mkdir(parents=True)
commit = build_repository(target)
assert commit == task["spec"]["commit"], "the target must be at the task's fixed base version"
(target / "NOTES.txt").write_text("my unrelated uncommitted note\n", encoding="utf-8")
other = next(t["id"] for t in catalog if t["id"] != "checkout")
other_file = next(iter((target / other).glob("*.py")))
other_file.write_text(other_file.read_text(encoding="utf-8") + "# local edit outside the task\n", encoding="utf-8")

run = client.post("/runs", json={"mode": "demo", "baselineId": "checkout", "reviewPolicy": "off",
                                 "requestKey": f"delivery-smoke-{uuid.uuid4()}"}).raise_for_status().json()
deadline = time.monotonic() + 300
while run["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
    assert time.monotonic() < deadline, "demo run did not finish"
    time.sleep(2)
    run = client.get(f"/runs/{run['id']}").raise_for_status().json()
assert run["status"] == "SUCCEEDED" and run["result"]["verification"]["passed"] is True, run["status"]
patch = run["result"]["patch"]
checks.append("demo_run_produced_an_accepted_candidate")

# Applying before any approval exists must be impossible: there is no delivery to apply.
code, out = deliver("apply", "--delivery", str(uuid.uuid4()))
assert code == 2 and "执行器记录" in out, out
code, out = deliver("prepare", "--run", run["id"], "--target", str(target))
assert code == 0, out
delivery_id = re.search(r"已登记交付 ([0-9a-f-]{36})", out).group(1)
row = client.get(f"/deliveries/{delivery_id}").raise_for_status().json()
assert row["status"] == "PENDING" and row["targetHead"] == commit and row["baseCommit"] == commit
assert all(f["path"].startswith("checkout/") for f in row["files"]) and "executorSha256" not in row
assert git("status", "--porcelain").count("\n") == 2, "prepare must not write to the target"
checks.append("prepare_registers_the_target_survey_without_writing")

code, out = deliver("apply", "--delivery", delivery_id)
assert code == 3 and "尚未批准" in out, out
client.post(f"/runs/{run['id']}/deliveries/{delivery_id}/decide",
            json={"decision": "approve", "patchSha256": row["patchSha256"],
                  "targetFingerprint": row["targetFingerprint"]}).raise_for_status()
code, out = deliver("apply", "--delivery", delivery_id)
assert code == 0 and "APPLIED" in out, out
applied = client.get(f"/deliveries/{delivery_id}").raise_for_status().json()
assert applied["status"] == "APPLIED" and applied["receipt"]["files_written"] == len(row["files"])
# The patch is present exactly (reverse-applying it cleanly proves that) and unrelated edits survived.
subprocess.run(["git", "-C", str(target), "apply", "--check", "-R", "--directory=checkout", "-"],
               input=patch, text=True, encoding="utf-8", check=True)
assert (target / "NOTES.txt").read_text(encoding="utf-8") == "my unrelated uncommitted note\n"
assert other_file.read_text(encoding="utf-8").endswith("# local edit outside the task\n")
checks.append("approved_patch_applied_and_unrelated_edits_preserved")

before = len(client.get(f"/runs/{run['id']}").raise_for_status().json()["events"])
code, out = deliver("apply", "--delivery", delivery_id)
assert code == 0 and "不再重复写入" in out, out
assert len(client.get(f"/runs/{run['id']}").raise_for_status().json()["events"]) == before
code, out = deliver("prepare", "--run", run["id"], "--target", str(target))
assert code == 0 and "无需交付" in out and len(delivery_rows(run["id"])) == 1, out
checks.append("repeating_an_applied_delivery_writes_nothing_and_registers_nothing")

# A target that moved after approval is refused without touching it.
git("checkout", "--", "checkout")
git("clean", "-fdq", "--", "checkout")
code, out = deliver("prepare", "--run", run["id"], "--target", str(target))
assert code == 0, out
second = re.search(r"已登记交付 ([0-9a-f-]{36})", out).group(1)
second_row = client.get(f"/deliveries/{second}").raise_for_status().json()
client.post(f"/runs/{run['id']}/deliveries/{second}/decide",
            json={"decision": "approve", "patchSha256": second_row["patchSha256"],
                  "targetFingerprint": second_row["targetFingerprint"]}).raise_for_status()
touched = target / next(f["path"] for f in second_row["files"] if f["change"] == "modified")
touched.write_text("# edited after approval\n" + touched.read_text(encoding="utf-8"), encoding="utf-8")
code, out = deliver("apply", "--delivery", second)
assert code == 1 and "INVALIDATED" in out, out
assert touched.read_text(encoding="utf-8").startswith("# edited after approval\n")
assert client.get(f"/deliveries/{second}").raise_for_status().json()["status"] == "INVALIDATED"
checks.append("target_changed_after_approval_is_refused_untouched")

# Half-and-half targets are never guessed at: prepare stops before registering anything.
code, out = deliver("prepare", "--run", run["id"], "--target", str(target))
assert code == 2 and "手动处理" in out and len(delivery_rows(run["id"])) == 2, out
checks.append("mixed_target_state_is_reported_not_delivered")

report = {"checks": checks, "run_id": run["id"], "deliveries": [delivery_id, second], "target": str(target)}
(root / "runtime/validation").mkdir(parents=True, exist_ok=True)
(root / "runtime/validation/delivery-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
