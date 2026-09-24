import subprocess

import pytest
from repopilot.public_tests import classify, discover


def checkout(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# public base file\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "base"], check=True)
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_public_checkout_selects_bounded_django_runner(tmp_path):
    commit = checkout(tmp_path, ["tests/runtests.py", "tests/utils_tests/__init__.py"])
    entry = discover(tmp_path, commit, "django/django")
    assert entry["command"] == "python tests/runtests.py utils_tests --parallel 1"
    assert entry["source"] == "tests/runtests.py"
    assert entry["smoke_only"] is True
    with pytest.raises(ValueError, match="base commit"):
        discover(tmp_path, "0" * 40, "django/django")


def test_public_checkout_selects_sphinx_and_unknown(tmp_path):
    commit = checkout(tmp_path, ["tests/test_build.py"])
    assert discover(tmp_path, commit, "sphinx-doc/sphinx")["source"] == "tests/test_build.py"
    assert discover(tmp_path, commit, "other/repo")["command"] is None
    assert discover(tmp_path, commit, "other/repo", "make smoke")["command"] == "make smoke"


@pytest.mark.parametrize(("returncode", "output", "expected"), [
    (1, "No module named pytest", "missing_dependency"),
    (5, "no tests ran", "no_tests_collected"),
    (2, "ERROR collecting", "collection_error"),
    (124, "", "timeout"),
    (1, "AssertionError", "test_failed"),
    (0, "1 passed", "smoke_passed"),
])
def test_classify_distinguishes_test_feedback(returncode, output, expected):
    assert classify(returncode, output) == expected
