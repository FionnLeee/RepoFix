"""Small frozen real-model reviewer calibration; labels never enter review messages."""

import argparse
import difflib
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from repofix import review
from repofix.model_policy import require_authorized_model
from repofix.quota import ModelQuota


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    model = require_authorized_model(os.environ["MODEL_NAME"], os.environ.get("MODEL_BASE_URL", ""))
    cases = []
    for name, task, old, clean, broken in [
        ("shipping", "shipping(total) returns 0 when total >= 150, otherwise 10.",
         "def shipping(total):\n    return 10\n",
         "def shipping(total):\n    return 0 if total >= 150 else 10\n",
         "def shipping(total):\n    return 0 if total > 150 else 10\n"),
        ("discount", "price(amount, percent) applies a percentage discount, rounds to two decimals, and rejects percent outside [0,100] with ValueError.",
         "def price(amount, percent):\n    return amount\n",
         "def price(amount, percent):\n    if not 0 <= percent <= 100:\n        raise ValueError('percent')\n    return round(amount * (1 - percent / 100), 2)\n",
         "def price(amount, percent):\n    return round(amount * (1 - percent / 100), 2)\n"),
    ]:
        for label, candidate in (("defect", broken), ("clean", clean)):
            patch = "diff --git a/example.py b/example.py\n" + "".join(difflib.unified_diff(
                old.splitlines(True), candidate.splitlines(True), fromfile="a/example.py", tofile="b/example.py"))
            cases.append({"id": name + "-" + label, "label": label, "task": task,
                          "base": old, "candidate": candidate, "patch": patch})
    frozen = {"model": model, "max_calls_per_case": 1, "max_output_tokens": 1600,
              "temperature": 0, "cases": cases, "retry_policy": "none"}
    args.out.mkdir(parents=True, exist_ok=False)
    serialized = json.dumps(frozen, sort_keys=True, ensure_ascii=True)
    (args.out / "frozen.json").write_text(serialized, encoding="utf-8")
    report = {"frozen_sha256": hashlib.sha256(serialized.encode()).hexdigest(), "model": model, "cases": []}
    quota = ModelQuota.from_env()
    for case in cases:
        row = {"id": case["id"], "label": case["label"]}
        started = time.monotonic()
        try:
            quota.acquire()
            try:
                row["review"] = review.perform(review.reviewer_for({"mode": "live"}, {}, {}, []),
                    {"task": case["task"]}, SimpleNamespace(allowedPaths=["example.py"], testCommand="not run"),
                    {"example.py": case["candidate"]}, ["example.py"], case["patch"], "No development tests supplied.",
                    0, base={"example.py": case["base"]})
            finally:
                quota.release()
        except Exception as error:
            row["error_type"] = type(error).__name__
        row["seconds"] = round(time.monotonic() - started, 2)
        report["cases"].append(row)
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": row["id"], "error": row.get("error_type"),
                          "blocking": len(review.blocking(row.get("review", {}).get("findings", [])))}), flush=True)
        if row.get("error_type"):
            break  # Do not spend remaining requests during a provider/quota outage.


if __name__ == "__main__":
    main()
