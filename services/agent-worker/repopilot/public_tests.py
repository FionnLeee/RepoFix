"""Bounded development-test discovery from a public base checkout only.

This is a read-only preflight for future tasks. It neither reads SWE-bench gold/test_patch
nor changes archived Mini attempts or their score.
"""

import subprocess
from pathlib import Path


def tracked_files(checkout, base_commit):
    checkout = Path(checkout)
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True,
                          text=True, timeout=5, check=True).stdout.strip()
    if head != base_commit:
        raise ValueError("Public checkout is not at the requested base commit")
    names = subprocess.run(["git", "-C", str(checkout), "ls-tree", "-r", "--name-only", "HEAD"],
                           capture_output=True, text=True, timeout=5, check=True).stdout.splitlines()
    return set(names)


def discover(checkout, base_commit, repo, explicit=None):
    files = tracked_files(checkout, base_commit)
    if explicit:
        return {"command": explicit, "source": "user explicit command", "base_commit": base_commit,
                "reason": "Explicit user choice", "timeout_seconds": 25, "smoke_only": False}
    if repo == "django/django" and "tests/runtests.py" in files and "tests/utils_tests/__init__.py" in files:
        return {"command": "python tests/runtests.py utils_tests --parallel 1", "source": "tests/runtests.py",
                "base_commit": base_commit, "reason": "Tracked Django runner and small public smoke suite",
                "timeout_seconds": 25, "smoke_only": True}
    if repo == "sphinx-doc/sphinx":
        candidates = sorted(name for name in files if name.startswith("tests/test_") and name.endswith(".py"))
        if candidates:
            return {"command": f"python -m pytest -q {candidates[0]}", "source": candidates[0],
                    "base_commit": base_commit, "reason": "Tracked Sphinx test file; pytest availability checked at execution",
                    "timeout_seconds": 25, "smoke_only": True}
    return {"command": None, "source": None, "base_commit": base_commit,
            "reason": "No bounded public test entry could be confirmed", "timeout_seconds": 25,
            "smoke_only": True}


def classify(returncode, output):
    text = output.lower()
    if returncode in (124, 137):
        return "timeout"
    if "no module named pytest" in text or "modulenotfounderror" in text:
        return "missing_dependency"
    if "no tests ran" in text or "collected 0 items" in text:
        return "no_tests_collected"
    if "importerror" in text or "error collecting" in text:
        return "collection_error"
    if returncode == 0:
        return "smoke_passed"
    return "test_failed"
