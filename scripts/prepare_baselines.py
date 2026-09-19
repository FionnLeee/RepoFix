"""Build a deterministic Git repository of three curated multi-file tasks."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from register_repository import register

root = Path(__file__).resolve().parents[1]
tasks = json.loads((root / "benchmarks/tasks.json").read_text(encoding="utf-8"))
runtime = root / "runtime"


def build_repository(repo: Path) -> str:
    """Materialise the baseline tasks as one commit; identical inputs give an identical commit id."""
    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="RepoPilot Baseline", GIT_COMMITTER_NAME="RepoPilot Baseline",
               GIT_AUTHOR_EMAIL="baseline@example.invalid", GIT_COMMITTER_EMAIL="baseline@example.invalid",
               GIT_AUTHOR_DATE="2026-09-16T00:00:00Z", GIT_COMMITTER_DATE="2026-09-16T00:00:00Z")

    def git(*args):
        return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(repo), *args], env=env)

    git("init", "-q")
    for task in tasks:
        for name, content in task["files"].items():
            target = repo / task["id"] / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
    git("add", "--", ".")
    git("commit", "-q", "-m", "baseline tasks v1")
    return git("rev-parse", "HEAD").decode().strip()


if __name__ == "__main__":
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary:
        repo = Path(temporary)
        commit = build_repository(repo)
        registered = register(repo, "baseline-v1", commit, runtime / "repositories")
    catalog = [{"id": t["id"], "title": t["title"], "task": t["task"], "spec": {
        "source": registered["source"], "commit": commit, "subdir": t["id"],
        "allowedPaths": sorted(t["reference"]), "verificationFiles": t["verification"],
        "testCommand": "python -m unittest discover -v",
    }} for t in tasks]
    (runtime / "baseline-catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"tasks": len(catalog), **registered}))
