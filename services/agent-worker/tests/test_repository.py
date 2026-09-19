import json
import os
import threading
import uuid
from pathlib import Path

import pytest
from minisweagent.models.test_models import DeterministicModel
from repopilot.baseline import task_definitions
from repopilot.repository import RepositoryTask, safe_path
from repopilot.repository_runtime import (
    execute_repository_run,
    make_patch_and_reapply,
    read_tree,
    replay_patch,
    verify,
)
from repopilot.runtime import Sandbox, TracedAgent


@pytest.mark.docker
def test_an_image_workspace_works_inside_the_images_own_repository(tmp_path, monkeypatch):
    """SWE-bench 式工作区：仓库在镜像里，候选来自镜像内的 git 差异，判定交给官方 harness。"""
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    task = next(t for t in task_definitions() if t["id"] == "checkout")
    context = tmp_path / "image"
    context.mkdir()
    for name, content in task["files"].items():
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (context / "Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n"
        "WORKDIR /testbed\nCOPY . /testbed\n"
        "RUN git init -q && git add -A && git -c user.email=t@example.invalid -c user.name=t commit -qm base\n"
        "RUN chmod -R 777 /testbed\n", encoding="utf-8")
    import docker

    client = docker.from_env(timeout=120)
    image, _ = client.images.build(path=str(context), tag="repopilot-image-workspace-test:latest", rm=True)
    try:
        spec = RepositoryTask(source="registered:baseline-v1", commit="a" * 40, allowedPaths=sorted(task["reference"]),
                              verificationMode="harness", workspaceMode="image",
                              sandboxImage="repopilot-image-workspace-test:latest", workspacePath="/testbed",
                              testCommand="python -m unittest discover -v")
        run = {"id": str(uuid.uuid4()), "generation": 1, "mode": "demo", "baselineId": "checkout",
               "task": task["task"], "spec": spec.model_dump(), "reviewPolicy": "off"}
        events = []
        class CountingQuota:
            calls = 0
            held = False
            def acquire(self):
                assert not self.held
                self.held = True
                self.calls += 1
                return 0
            def release(self):
                assert self.held
                self.held = False
        quota = CountingQuota()
        result = execute_repository_run(run, lambda kind, data, status=None: events.append(kind), threading.Event(),
                                        quota=quota)
        assert quota.calls == result["model_calls"] and quota.calls > 0 and not quota.held
        assert result["verification"] == {"passed": None, "delegated": "swebench-harness", "output": "",
                                          "baseline": None, "candidate": None, "patch_replayed": None}
        assert "REPOSITORY_READY" in events and "CANDIDATE" in events and "VERIFICATION_DELEGATED" in events
        # The candidate is the repository's own diff, and the patch is repo-root relative.
        assert set(result["changed_files"]) == {"checkout.py", "money.py"}
        assert result["patch"].startswith("diff --git a/checkout.py")
        assert "+    return round(subtotal + (0 if subtotal >= 150 else 10), 2)" in result["patch"]
        assert result["provenance"]["workspace_mode"] == "image"
        assert result["provenance"]["workspace_path"] == "/testbed"
        # An image workspace has no snapshot to bind an approval to: it says so instead of pausing.
        assert "APPROVAL_POLICY_IGNORED" in events and "APPROVAL_WAIT" not in events
    finally:
        client.images.remove(image.id, force=True)
        client.close()


def test_harness_verified_tasks_carry_no_acceptance_tests():
    base = {"source": "registered:test", "commit": "a" * 40, "allowedPaths": ["src/a.py"]}
    assert RepositoryTask.model_validate({**base, "verificationMode": "harness"}).verificationFiles == {}
    assert RepositoryTask.model_validate({**base, "verificationFiles": {"test_a.py": "x"}}).verificationMode == "tests"
    for change in ({"verificationMode": "harness", "verificationFiles": {"test_a.py": "x"}}, {}):
        with pytest.raises(ValueError):
            RepositoryTask.model_validate({**base, **change})


@pytest.mark.docker
def test_a_harness_verified_run_delivers_a_patch_without_local_acceptance(tmp_path, monkeypatch):
    """SWE-bench 式运行：本机只证明补丁能干净地重放到固定提交，判定交给官方 harness。"""
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    registered = Path(os.getenv("REPOSITORY_ROOT", "runtime/repositories")) / "baseline-v1"
    snapshot = json.loads(next(registered.glob("*.json")).read_text(encoding="utf-8"))
    task = next(t for t in task_definitions() if t["id"] == "checkout")
    spec = RepositoryTask(source="registered:baseline-v1", commit=snapshot["commit"], subdir="checkout",
                          allowedPaths=sorted(task["reference"]), verificationMode="harness",
                          testCommand="python -m unittest discover -v")
    run = {"id": str(uuid.uuid4()), "generation": 1, "mode": "demo", "baselineId": "checkout",
           "task": task["task"], "spec": spec.model_dump(), "reviewPolicy": "off"}
    events = []
    result = execute_repository_run(run, lambda kind, data, status=None: events.append(kind), threading.Event())
    assert result["verification"] == {"passed": None, "delegated": "swebench-harness", "output": "",
                                      "baseline": None, "candidate": None, "patch_replayed": True}
    assert result["patch"] and "VERIFICATION_DELEGATED" in events and "CANDIDATE" in events
    assert result["changed_files"] and "ACCEPTANCE" not in " ".join(events)


def test_repository_contract_rejects_traversal_and_arbitrary_hosts():
    for path in ("../x", "/etc/passwd", "a/../../b", "a\\b", ".git/config", "a//b", "A/.GIT/config"):
        with pytest.raises(ValueError):
            safe_path(path)
    spec = {"source": "registered:test", "commit": "a" * 40, "allowedPaths": ["src/a.py"],
            "verificationFiles": {"test_a.py": "import unittest"}}
    for change in ({"source": "http://127.0.0.1/secret"}, {"commit": "main"}, {"allowedPaths": ["../x"]},
                   {"source": "https://github.com@evil.test/o/r"}, {"verificationFiles": {"../test_a.py": "x"}}):
        with pytest.raises(ValueError):
            RepositoryTask.model_validate({**spec, **change})


def test_patch_roundtrip_handles_multiple_files_additions_deletions_and_no_newline(tmp_path):
    before = {"src/a.py": "x = 1\n", "remove.py": "old", "stable.py": "unchanged\r\n"}
    after = {"src/a.py": "x = 2\n", "new.py": "new", "stable.py": "unchanged\r\n"}
    patch, replayed = make_patch_and_reapply(before, after, tmp_path)
    assert replayed == after
    assert replay_patch(before, patch, tmp_path) == after
    assert "src/a.py" in patch and "remove.py" in patch and "new.py" in patch
    assert "stable.py" not in patch


def test_compaction_preserves_task_recent_messages_and_full_audit():
    agent = TracedAgent(DeterministicModel(outputs=[]), object(), emit=lambda *_: None,
                        cancelled=threading.Event(), context_mode="compact", system_template="", instance_template="")
    messages = [{"role": "system", "content": "hard constraints"}, {"role": "user", "content": "task"}]
    messages += [{"role": "user", "content": f"output {i}: " + "x" * 1800} for i in range(10)]
    agent.add_messages(*messages)
    agent.compact_context()
    assert agent.messages[:2] == messages[:2]
    assert agent.messages[-4:] == messages[-4:]
    assert agent.full_messages == messages
    assert agent.compactions[0]["after_characters"] < agent.compactions[0]["before_characters"]
    assert agent.n_calls == 0


@pytest.mark.docker
def test_candidate_rejects_ancestor_symlink_and_keeps_other_files():
    sandbox = Sandbox(str(uuid.uuid4()), lambda *_: None, threading.Event(), files={"src/a.py": "x=1\n"})
    try:
        assert read_tree(sandbox) == {"src/a.py": "x=1\n"}
        sandbox.execute({"command": "mv src old; ln -s /tmp src"})
        with pytest.raises(ValueError):
            read_tree(sandbox)
    finally:
        sandbox.close()


@pytest.mark.docker
def test_independent_verification_rejects_original_and_accepts_reference():
    task = json.loads(Path("benchmarks/tasks.json").read_text(encoding="utf-8"))[0]
    spec = RepositoryTask(source="registered:test", commit="a" * 40, allowedPaths=list(task["reference"]),
                          verificationFiles=task["verification"])
    sandbox = Sandbox(str(uuid.uuid4()), lambda *_: None, threading.Event())
    image = sandbox.container.image.id
    sandbox.close()
    before = verify(task["files"], spec, str(uuid.uuid4()), threading.Event(), image)
    after = verify({**task["files"], **task["reference"]}, spec, str(uuid.uuid4()), threading.Event(), image)
    assert before["returncode"] != 0 and before["report"]["failures"] > 0
    assert after["returncode"] == 0 and after["report"]["tests"] == 5
    assert before["report"]["ids"] == after["report"]["ids"]
