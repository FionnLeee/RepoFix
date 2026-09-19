"""The host-side applier writes only what a person approved, and only onto the version they saw."""

import os
import subprocess
from pathlib import Path

import pytest
from repopilot import delivery
from repopilot.delivery import Checkout, DeliveryError, deliver, fingerprint, patch_files, repo_paths, survey

ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(repo: Path, *args, data=None):
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(repo), *args], input=data, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV).stdout.decode()


def make_repo(root: Path, files: dict[str, str]) -> str:
    root.mkdir()
    git(root, "init", "-q")
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content.encode())
    git(root, "add", "--", ".")
    git(root, "commit", "-q", "-m", "base")
    return git(root, "rev-parse", "HEAD").strip()


def make_patch(root: Path, base: str, changes: dict[str, str | None]) -> str:
    """Produce the patch the worker would have produced for ``changes`` (None deletes)."""
    for name, content in changes.items():
        if content is None:
            (root / name).unlink()
        else:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(content.encode())
    git(root, "add", "-A", "--", ".")
    patch = git(root, "diff", "--cached", "--no-ext-diff", base, "--")
    git(root, "reset", "-q", "--hard", base)
    return patch


BASE = {"task1/calc.py": "def add(a, b):\n    return a - b\n", "task1/README": "keep\n", "other.txt": "unrelated\n"}
FIX = {"task1/calc.py": "def add(a, b):\n    return a + b\n", "task1/new.py": "NEW = 1\n", "task1/README": None}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "target"
    base = make_repo(root, BASE)
    patch = make_patch(root, base, FIX)
    # The worker's patch is relative to the task subdirectory; strip the prefix to imitate it.
    return root, base, patch.replace("a/task1/", "a/").replace("b/task1/", "b/")


def test_patch_files_classifies_added_modified_deleted_and_prefixes_subdir(repo):
    _, _, patch = repo
    changes = patch_files(patch)
    assert changes == {"calc.py": "modified", "new.py": "added", "README": "deleted"}
    assert repo_paths(changes, "task1") == {"task1/calc.py": "modified", "task1/new.py": "added",
                                            "task1/README": "deleted"}


def test_patch_files_refuses_binary_and_quoted_paths():
    with pytest.raises(DeliveryError):
        patch_files('diff --git "a/we ird" "b/we ird"\n')
    with pytest.raises(DeliveryError):
        patch_files("diff --git a/x.bin b/x.bin\nindex 0000000..1111111\nGIT binary patch\n")
    with pytest.raises(DeliveryError):
        patch_files("nothing here\n")


def test_survey_reports_base_state_expected_blobs_and_fingerprint(repo):
    root, base, patch = repo
    report = survey(Checkout(root), base, patch, "task1")
    assert report["state"] == "base" and report["head"] == base
    by_path = {f["path"]: f for f in report["files"]}
    assert by_path["task1/new.py"]["before"] is None and by_path["task1/new.py"]["after"]
    assert by_path["task1/README"]["after"] is None and by_path["task1/README"]["before"]
    assert by_path["task1/calc.py"]["before"] != by_path["task1/calc.py"]["after"]
    assert report["fingerprint"] == fingerprint(base, report["observed"])
    # The scratch index never touched the work tree.
    assert git(root, "status", "--porcelain") == ""


def test_survey_rejects_a_checkout_without_the_base_commit(repo, tmp_path):
    root, _, patch = repo
    with pytest.raises(DeliveryError, match="base commit"):
        survey(Checkout(root), "f" * 40, patch, "task1")
    with pytest.raises(DeliveryError, match="Git work tree"):
        Checkout(tmp_path)


def test_deliver_applies_only_when_the_fingerprint_still_matches(repo):
    root, base, patch = repo
    (root / "other.txt").write_text("my local edit\n")  # unrelated uncommitted work must survive
    report = survey(Checkout(root), base, patch, "task1")
    result = deliver(Checkout(root), patch, "task1", report["files"], report["fingerprint"])
    assert result["status"] == "APPLIED" and result["receipt"]["files_written"] == 3
    assert (root / "task1/calc.py").read_text() == FIX["task1/calc.py"]
    assert (root / "task1/new.py").read_text() == "NEW = 1\n" and not (root / "task1/README").exists()
    assert (root / "other.txt").read_text() == "my local edit\n"
    assert survey(Checkout(root), base, patch, "task1")["state"] == "applied"
    # Running the same approved delivery again writes nothing and says so.
    again = deliver(Checkout(root), patch, "task1", report["files"], report["fingerprint"])
    assert again["status"] == "APPLIED" and again["receipt"]["already_applied"] and again["receipt"]["files_written"] == 0


def test_deliver_refuses_a_target_changed_after_approval(repo):
    root, base, patch = repo
    report = survey(Checkout(root), base, patch, "task1")
    (root / "task1/calc.py").write_text("def add(a, b):\n    return a * b\n")
    result = deliver(Checkout(root), patch, "task1", report["files"], report["fingerprint"])
    assert result["status"] == "INVALIDATED"
    assert (root / "task1/calc.py").read_text() == "def add(a, b):\n    return a * b\n"
    assert not (root / "task1/new.py").exists()


def test_deliver_refuses_when_head_moved_even_if_files_match(repo):
    root, base, patch = repo
    report = survey(Checkout(root), base, patch, "task1")
    (root / "other.txt").write_text("committed elsewhere\n")
    git(root, "commit", "-q", "-am", "move head")
    result = deliver(Checkout(root), patch, "task1", report["files"], report["fingerprint"])
    assert result["status"] == "INVALIDATED"
    # A fresh survey on the moved HEAD is still safe: the affected files match the base blobs.
    fresh = survey(Checkout(root), base, patch, "task1")
    assert fresh["state"] == "base" and fresh["head"] != base


def test_deliver_reports_mixed_state_instead_of_guessing(repo):
    root, base, patch = repo
    report = survey(Checkout(root), base, patch, "task1")
    (root / "task1/new.py").write_text("someone else's file\n")
    mixed = survey(Checkout(root), base, patch, "task1")
    assert mixed["state"] == "mixed"
    # deliver() with the stale approval fingerprint refuses; with the fresh one the check fails safely.
    assert deliver(Checkout(root), patch, "task1", report["files"], report["fingerprint"])["status"] == "INVALIDATED"
    result = deliver(Checkout(root), patch, "task1", mixed["files"], mixed["fingerprint"])
    assert result["status"] == "NEEDS_ATTENTION" and "git apply" in result["receipt"]["reason"]
    assert (root / "task1/calc.py").read_text() == BASE["task1/calc.py"]


def test_symlinked_target_file_is_refused(repo):
    root, base, patch = repo
    (root / "task1/calc.py").unlink()
    try:
        os.symlink(root / "other.txt", root / "task1/calc.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(DeliveryError, match="symbolic link"):
        survey(Checkout(root), base, patch, "task1")


def test_fingerprint_is_order_independent_and_covers_head():
    a = fingerprint("h", {"x": "1", "y": None})
    assert a == fingerprint("h", {"y": None, "x": "1"})
    assert a != fingerprint("g", {"x": "1", "y": None})
    assert delivery.MAX_FILES == 200
