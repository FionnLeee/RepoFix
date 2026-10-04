"""Explicit verification tiers; never starts a live model evaluation."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, policy=None):
    env = os.environ.copy()
    if policy:
        env["MODEL_POLICY"] = policy
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=("unit", "build", "smoke", "integration-help"))
    args = parser.parse_args()
    if args.tier == "unit":
        run(["uv", "run", "--no-sync", "ruff", "check", "services", "scripts"])
        for policy in ("free-quota", "official-deepseek"):
            run(["uv", "run", "--no-sync", "pytest", "-m", "not docker", "-q", "-p", "no:cacheprovider"],
                policy=policy)
        run(["node", "--experimental-strip-types", "--test",
             "apps/control-api/test/auth.test.mjs", "apps/control-api/test/showcase-facts.test.mjs",
             "apps/web/tests/showcase-evidence.test.mjs"])
    elif args.tier == "build":
        run([shutil.which("npx.cmd") or shutil.which("npx") or "npx", "--yes", "pnpm@10.17.1", "build"])
    elif args.tier == "smoke":
        print("Deterministic local smoke. Requires a separate running stack; does not enable live models.", flush=True)
        run(["uv", "run", "--no-sync", "python", "scripts/smoke.py"])
    else:
        print("Docker integration tests need the isolated test stack. scripts/protocol_check.py injects failures: "
              "stop its Worker and ensure no in-flight tasks before running it. Do not run it on an active stack.")


if __name__ == "__main__":
    main()
