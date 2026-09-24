"""Pinned Git deltas for image workspaces; raw blobs are retained independently of UI limits.

Recovery covers tracked and non-ignored repository files. Ignored build caches and changes
to the container's installed environment are discarded when a new pinned image is started.
"""

import base64
import hashlib
import json
import uuid

from repofix.repository import validate_files


def git(sandbox, *args, optional=False):
    result = sandbox.container.exec_run(
        ["git", "-c", "core.fileMode=false", "-c", "core.autocrlf=false", "-c", "core.hooksPath=/dev/null",
         "-c", "safe.directory=" + sandbox.workspace, "-C", sandbox.workspace, *args], user=sandbox.user)
    if result.exit_code and not optional:
        raise ValueError("Image repository operation failed: " + result.output.decode(errors="replace")[-300:])
    return None if result.exit_code else result.output


def initialize(sandbox, expected_commit=None):
    base = git(sandbox, "rev-parse", "HEAD").decode().strip()
    if expected_commit is not None and base != expected_commit:
        # Official instance images may end at an extra preparation commit. The agent must
        # see exactly the dataset's base tree, not those later files or evaluator changes.
        git(sandbox, "reset", "--hard", expected_commit)
        git(sandbox, "clean", "-fd")
        base = git(sandbox, "rev-parse", "HEAD").decode().strip()
        if base != expected_commit:
            raise ValueError("Image HEAD differs from the frozen base commit")
    sandbox.image_base = base
    sandbox.checkpoint_reader = lambda quiescent=False: checkpoint_files(sandbox, quiescent)
    return {"image-base.json": json.dumps({"commit": base, "image": sandbox.container.image.id}, sort_keys=True)}


def capture(sandbox, quiescent=False):
    if quiescent:
        result = sandbox.container.exec_run(["python", "-I", "-c", """
import os
ancestors = {1}
pid = os.getpid()
while pid > 1 and pid not in ancestors:
    ancestors.add(pid)
    with open('/proc/%d/stat' % pid) as f: pid = int(f.read().rsplit(')', 1)[1].split()[1])
for name in os.listdir('/proc'):
    if name.isdigit() and int(name) not in ancestors:
        try:
            with open('/proc/%s/stat' % name) as f: state = f.read().rsplit(')', 1)[1].split()[0]
        except FileNotFoundError:
            continue
        assert state == 'Z', 'Workspace has a live background process'
"""])
        if result.exit_code:
            raise ValueError("Image checkpoint requires a quiescent workspace")
    if git(sandbox, "rev-parse", "HEAD").decode().strip() != sandbox.image_base:
        raise ValueError("Agent changed the pinned repository HEAD")
    git(sandbox, "add", "-A", "--", ".")
    return git(sandbox, "diff", "--cached", "--binary", "--full-index", "--no-renames",
               "--no-ext-diff", "--no-textconv", sandbox.image_base, "--")


def checkpoint_files(sandbox, quiescent=False):
    encoded = base64.b64encode(capture(sandbox, quiescent)).decode()
    files = {f"delta/{i:06d}": encoded[start:start + 90000]
             for i, start in enumerate(range(0, len(encoded), 90000))}
    files["image-base"] = sandbox.image_base
    return validate_files(files)


def restore(sandbox, files):
    validate_files(files)
    if files.get("image-base") != sandbox.image_base or any(
            name != "image-base" and not name.startswith("delta/") for name in files):
        raise ValueError("Image checkpoint base mismatch")
    patch = base64.b64decode("".join(files[name] for name in sorted(files) if name.startswith("delta/")), validate=True)
    if patch:
        filename = "repofix-restore-" + uuid.uuid4().hex + ".patch"
        try:
            # Docker archive extraction does not reliably target tmpfs mounts; write through
            # exec in bounded argv chunks instead. The random path never traverses the repo.
            encoded = base64.b64encode(patch).decode()
            for start in range(0, len(encoded), 16000):
                result = sandbox.container.exec_run(["python", "-I", "-c",
                    "import base64,sys; open(sys.argv[1],sys.argv[2]).write(base64.b64decode(sys.argv[3]))",
                    "/tmp/" + filename, "xb" if start == 0 else "ab", encoded[start:start + 16000]])
                if result.exit_code:
                    raise ValueError("Cannot transfer checkpoint delta")
            git(sandbox, "update-index", "--refresh")
            git(sandbox, "apply", "--check", "--index", "/tmp/" + filename)
            git(sandbox, "apply", "--index", "/tmp/" + filename)
        finally:
            sandbox.container.exec_run(["rm", "-f", "--", "/tmp/" + filename])
    if checkpoint_files(sandbox, True) != files:
        raise ValueError("Image delta replay differs from registered checkpoint")


def candidate(sandbox, folder=None, source=None):
    patch = capture(sandbox)
    names = [name.decode("utf-8") for name in git(
        sandbox, "diff", "--cached", "--name-only", "-z", "--no-renames", sandbox.image_base, "--").split(b"\0") if name]
    before, after, records = {}, {}, []
    for name in names:
        row = {"path": name}
        for side, ref, preview in (("base", sandbox.image_base + ":" + name, before),
                                   ("candidate", ":" + name, after)):
            raw = git(sandbox, "show", ref, optional=True)
            if raw is None:
                row[side] = None
                continue
            sha = hashlib.sha256(raw).hexdigest()
            row[side] = {"sha256": sha, "bytes": len(raw), "blob": "blobs/" + sha}
            if folder is not None:
                (folder / "blobs").mkdir(exist_ok=True)
                (folder / "blobs" / sha).write_bytes(raw)
            try:
                text = raw.decode("utf-8")
                if "\x00" not in text:
                    preview[name] = text
            except UnicodeDecodeError:
                pass
        row["status"] = "added" if row["base"] is None else "deleted" if row["candidate"] is None else "modified"
        records.append(row)
    if source is not None:
        source.clear()
        source.update(before)
    if folder is not None:
        for filename, data in (("source.json", before), ("candidate.json", after),
                               ("file-changes.json", {"files": records, "complete": True,
                                                      "patch_sha256": hashlib.sha256(patch).hexdigest()})):
            (folder / filename).write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    return after, names, patch.decode("utf-8")
