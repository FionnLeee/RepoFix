"""Export tracked UTF-8 blobs from an exact Git commit; never executes repo code."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/agent-worker"))
from repopilot.repository import digest, validate_files  # noqa: E402


def register(repo: Path, repo_id: str, commit: str, output: Path):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", repo_id) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Repository id and full lowercase 40-character commit are required")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], timeout=30)

    if git("cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("Expected a commit object")
    files = {}
    for entry in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("Registered snapshots do not support symlinks or submodules")
        if int(git("cat-file", "-s", oid)) > 100000:
            raise ValueError("File exceeds 100 KB")
        files[path.decode("utf-8")] = git("cat-file", "blob", oid).decode("utf-8")
        validate_files(files)
    validate_files(files)
    target = output / repo_id / f"{commit}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"commit": commit, "digest": digest(files), "files": files}
    if target.exists():
        if json.loads(target.read_text(encoding="utf-8")) != data:
            raise ValueError("Refusing to replace an existing immutable snapshot")
    else:
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
        temporary.replace(target)
    return {"source": f"registered:{repo_id}", "commit": commit, "digest": data["digest"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, default=Path("runtime/repositories"))
    args = parser.parse_args()
    print(json.dumps(register(args.repo, args.id, args.commit, args.output)))
