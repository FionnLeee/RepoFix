"""Host-side patch delivery: apply an approved candidate patch to a user's own checkout.

The agent never sees the target path. This module runs on the host, driven by a person, and
only writes files whose current content it can prove equals the run's base version.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .repository import safe_path

HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
BLOB = re.compile(r"^[0-9a-f]{40}$")
MAX_FILES = 200


class DeliveryError(ValueError):
    pass


def patch_files(patch: str) -> dict[str, str]:
    """Map each path touched by a Git patch to added / modified / deleted.

    Renames count as a delete plus an add so every written path is listed explicitly;
    binary and quoted-path entries are refused because the fingerprint could not cover them.
    """
    changes: dict[str, str] = {}
    lines = patch.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("diff --git "):
            continue
        header = HEADER.match(line)
        if not header:
            raise DeliveryError("Patch contains a quoted or malformed path header")
        # A rename header names two paths; the metadata lines below settle both.
        old, new = safe_path(header.group(1)), safe_path(header.group(2))
        kind = "modified"
        for meta in lines[index + 1:index + 8]:
            if meta.startswith(("diff --git ", "@@ ")):
                break
            if meta.startswith("new file mode"):
                kind = "added"
            elif meta.startswith("deleted file mode"):
                kind = "deleted"
            elif meta.startswith("rename from"):
                kind = "renamed"
            elif meta.startswith(("GIT binary patch", "Binary files")):
                raise DeliveryError(f"Binary change to {new} cannot be delivered")
        if kind == "renamed":
            if old in changes or new in changes:
                raise DeliveryError("Patch lists a path twice")
            changes[old], changes[new] = "deleted", "added"
            continue
        if old != new:
            raise DeliveryError(f"Header paths differ without a rename: {old} -> {new}")
        if new in changes:
            raise DeliveryError("Patch lists a path twice")
        changes[new] = kind
    if not changes:
        raise DeliveryError("Patch touches no files")
    if len(changes) > MAX_FILES:
        raise DeliveryError("Patch touches too many files to deliver")
    return changes


def repo_paths(changes: dict[str, str], subdir: str) -> dict[str, str]:
    """Patch paths are relative to the task's subdirectory; the checkout needs repository paths."""
    prefix = f"{subdir}/" if subdir else ""
    return {safe_path(prefix + name): kind for name, kind in changes.items()}


class Checkout:
    """Read-mostly Git access to the user's target checkout; every path is repository-relative."""

    def __init__(self, root: Path):
        self.root = Path(root)
        if not self.root.is_dir():
            raise DeliveryError(f"Target is not a directory: {root}")
        try:
            top = self.git("rev-parse", "--show-toplevel").strip()
        except DeliveryError as error:
            raise DeliveryError(f"Target is not a Git work tree: {root}") from error
        if Path(top).resolve() != self.root.resolve():
            raise DeliveryError("Target must be the root of a Git work tree, not a subdirectory")

    def git(self, *args: str, data: bytes | None = None, env: dict | None = None) -> str:
        merged = os.environ.copy()
        merged.update(env or {})
        result = subprocess.run(["git", "-C", str(self.root), *args], input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60, env=merged)
        if result.returncode:
            raise DeliveryError(result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
        return result.stdout.decode("utf-8", "replace")

    def head(self) -> str:
        return self.git("rev-parse", "--verify", "HEAD").strip()

    def has_commit(self, commit: str) -> bool:
        try:
            return self.git("cat-file", "-t", commit).strip() == "commit"
        except DeliveryError:
            return False

    def base_blob(self, commit: str, path: str) -> str | None:
        try:
            value = self.git("rev-parse", "--verify", "-q", f"{commit}:{path}").strip()
        except DeliveryError:
            return None
        return value if BLOB.match(value) else None

    def working_blob(self, path: str) -> str | None:
        """Blob id of the working-tree file as Git would store it (clean filters applied)."""
        target = self.root / path
        if target.is_symlink():
            raise DeliveryError(f"{path} is a symbolic link; refusing to deliver through it")
        if not target.exists():
            return None
        if not target.is_file():
            raise DeliveryError(f"{path} is not a regular file")
        return self.git("hash-object", f"--path={path}", "--", str(target)).strip()

    def expected_blobs(self, commit: str, patch: str, subdir: str, paths: list[str]) -> dict[str, str | None]:
        """Apply the patch to a scratch index seeded from the base commit; never touches the work tree."""
        with tempfile.TemporaryDirectory() as temporary:
            index = str(Path(temporary) / "index")
            env = {"GIT_INDEX_FILE": index}
            self.git("read-tree", commit, env=env)
            directory = [f"--directory={subdir}"] if subdir else []
            self.git("apply", "--cached", "--whitespace=nowarn", *directory, "-", data=patch.encode(), env=env)
            listed = self.git("ls-files", "-s", "--", *paths, env=env)
        after: dict[str, str | None] = {path: None for path in paths}
        for line in listed.splitlines():
            meta, name = line.split("\t", 1)
            after[name] = meta.split()[1]
        return after

    def apply(self, patch: str, subdir: str, check_only: bool = False) -> None:
        directory = [f"--directory={subdir}"] if subdir else []
        flags = ["--check"] if check_only else []
        self.git("apply", "--whitespace=nowarn", *flags, *directory, "-", data=patch.encode())


def fingerprint(head: str, observed: dict[str, str | None]) -> str:
    payload = {"head": head, "files": {name: observed[name] for name in sorted(observed)}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def survey(checkout: Checkout, commit: str, patch: str, subdir: str) -> dict:
    """Describe what applying ``patch`` would do to ``checkout`` and whether that is safe now."""
    if not checkout.has_commit(commit):
        raise DeliveryError(f"Target checkout does not contain the base commit {commit}")
    changes = repo_paths(patch_files(patch), subdir)
    paths = sorted(changes)
    before = {path: checkout.base_blob(commit, path) for path in paths}
    for path, kind in changes.items():
        if kind == "added" and before[path] is not None:
            raise DeliveryError(f"{path} is new in the patch but exists at the base commit")
        if kind != "added" and before[path] is None:
            raise DeliveryError(f"{path} is missing from the base commit")
    after = checkout.expected_blobs(commit, patch, subdir, paths)
    observed = {path: checkout.working_blob(path) for path in paths}
    head = checkout.head()
    files = [{"path": path, "change": changes[path], "before": before[path], "after": after[path]} for path in paths]
    return {"head": head, "files": files, "observed": observed, "fingerprint": fingerprint(head, observed),
            "state": classify(files, observed)}


def classify(files: list[dict], observed: dict[str, str | None]) -> str:
    """``base`` = safe to apply, ``applied`` = already delivered, ``mixed`` = a person must look."""
    if all(observed[f["path"]] == f["before"] for f in files):
        return "base"
    if all(observed[f["path"]] == f["after"] for f in files):
        return "applied"
    return "mixed"


def deliver(checkout: Checkout, patch: str, subdir: str, files: list[dict], expected_fingerprint: str) -> dict:
    """Apply the patch only if the affected files still match the fingerprint the person approved."""
    paths = [f["path"] for f in files]
    head = checkout.head()
    observed = {path: checkout.working_blob(path) for path in paths}
    if classify(files, observed) == "applied":
        # An earlier attempt (or the person) already put exactly the candidate content there.
        return {"status": "APPLIED", "receipt": {"head": head, "observed": observed, "state": "applied",
                                                 "files_written": 0, "already_applied": True}}
    current = fingerprint(head, observed)
    if current != expected_fingerprint:
        return {"status": "INVALIDATED", "receipt": {"reason": "目标文件或 HEAD 在审批后发生了变化，未写入任何文件",
                                                     "head": head, "observed": observed, "fingerprint": current}}
    try:
        checkout.apply(patch, subdir, check_only=True)
        checkout.apply(patch, subdir)
    except DeliveryError as error:
        # ``--check`` failing means nothing was written; a failure after it needs a person.
        after = {path: checkout.working_blob(path) for path in paths}
        return {"status": "NEEDS_ATTENTION", "receipt": {"reason": f"git apply 失败：{error}", "head": head,
                                                         "observed": after, "state": classify(files, after)}}
    after = {path: checkout.working_blob(path) for path in paths}
    state = classify(files, after)
    if state != "applied":
        return {"status": "NEEDS_ATTENTION", "receipt": {"reason": "应用后的文件内容与预期候选不一致", "head": head,
                                                         "observed": after, "state": state}}
    return {"status": "APPLIED", "receipt": {"head": head, "observed": after, "state": state,
                                             "files_written": len(paths)}}
