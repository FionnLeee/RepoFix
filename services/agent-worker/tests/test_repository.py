import json
import threading
import uuid
from pathlib import Path

import pytest
from minisweagent.models.test_models import DeterministicModel
from repopilot.repository import RepositoryTask, safe_path
from repopilot.repository_runtime import make_patch_and_reapply, read_tree, replay_patch, verify
from repopilot.runtime import Sandbox, TracedAgent


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
