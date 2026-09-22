import hashlib
import json
import threading
import uuid

import docker
import pytest
from minisweagent.models.test_models import DeterministicModel, make_output
from repopilot import image_workspace, recovery
from repopilot.runtime import Sandbox, TracedAgent


@pytest.fixture
def image(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM repopilot-worker:latest\nWORKDIR /testbed\n"
        "RUN git init -q && printf 'old\\n' > old.py && printf 'gone\\n' > gone.py && "
        "git add . && git -c user.name=test -c user.email=test@example.invalid commit -qm base\n")
    client = docker.from_env()
    built, _ = client.images.build(path=str(tmp_path), rm=True)
    yield built.id
    client.images.remove(built.id, force=True)
    client.close()


@pytest.mark.docker
def test_image_new_generation_recovers_bytes_messages_and_budget(image, tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "task": "fix", "mode": "demo", "spec": {"workspaceMode": "image"}}
    events = []
    outputs = [make_output("edit", [{"command": "printf 'new\\n' > old.py; rm gone.py; printf '\\000\\377' > binary.bin"}]),
               make_output("submit", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])]

    def agent_for(sandbox, current):
        agent = TracedAgent(DeterministicModel(outputs=outputs), sandbox,
                            emit=lambda kind, data: events.append((kind, data)), cancelled=threading.Event(),
                            system_template="", instance_template="{{task}}", step_limit=2,
                            output_path=tmp_path / "trajectory.json")
        source = image_workspace.initialize(sandbox)
        agent.enable_checkpoints(current, source)
        return agent, source

    sandbox = Sandbox(run["id"], lambda *_: None, threading.Event(), files={}, image=image,
                      workspace="/testbed", writable=True, user="0:0")
    try:
        agent, source = agent_for(sandbox, run)
        agent.extra_template_vars = {"task": "fix"}
        agent.add_messages({"role": "user", "content": "fix"})
        agent.step()
        agent.save(tmp_path / "trajectory.json")
        reference = agent.checkpoints.latest
        assert reference["phase"] == "ready" and reference["model_calls"] == 1
        candidate, names, patch = image_workspace.candidate(sandbox, tmp_path)
        assert set(names) == {"old.py", "gone.py", "binary.bin"}
        assert candidate == {"old.py": "new\n"}
        records = json.loads((tmp_path / "file-changes.json").read_text())["files"]
        blob = next(row["candidate"]["blob"] for row in records if row["path"] == "binary.bin")
        assert (tmp_path / blob).read_bytes() == b"\0\xff"
        # A half-applied later command must not leak into the fresh sandbox.
        sandbox.execute({"command": "echo corrupt > old.py"})
    finally:
        sandbox.close()
    next_run = {**run, "generation": 2}
    restored = recovery.load_resume(next_run, reference, source)
    fresh = Sandbox(run["id"], lambda *_: None, threading.Event(), files={}, image=image,
                    workspace="/testbed", writable=True, user="0:0")
    try:
        resumed, _ = agent_for(fresh, next_run)
        image_workspace.restore(fresh, restored.files)
        resumed.restore_checkpoint(reference, allow_previous_generation=True)
        assert resumed.n_calls == 1 and resumed.env.index == 1
        assert image_workspace.candidate(fresh)[2] == patch
        assert resumed.run("fix")["exit_status"] == "Submitted"
        assert resumed.n_calls == 2
        assert hashlib.sha256(image_workspace.capture(fresh)).hexdigest() == hashlib.sha256(patch.encode()).hexdigest()
    finally:
        fresh.close()


@pytest.mark.docker
def test_image_artifacts_keep_more_than_sixty_files_and_large_text(image, tmp_path):
    sandbox = Sandbox(str(uuid.uuid4()), lambda *_: None, threading.Event(), files={}, image=image,
                      workspace="/testbed", writable=True, user="0:0")
    try:
        image_workspace.initialize(sandbox)
        sandbox.execute({"command": "python -c \"from pathlib import Path; [Path(f'f{i}.py').write_text('x') for i in range(65)]; Path('large.py').write_text('y'*120000)\""})
        candidate, changed, _ = image_workspace.candidate(sandbox, tmp_path)
        assert len(changed) == 66 and len(candidate["large.py"]) == 120000
        assert len(json.loads((tmp_path / "file-changes.json").read_text())["files"]) == 66
    finally:
        sandbox.close()


def test_paid_models_rejected_before_network(monkeypatch):
    import litellm
    from repopilot.model_policy import FREE_MODELS, require_free_model
    from repopilot.review import ModelReviewer
    monkeypatch.setattr(litellm, "completion", lambda **_: pytest.fail("network must not be reached"))
    for name in FREE_MODELS:
        assert require_free_model(name) == name
    with pytest.raises(ValueError, match="free quotas"):
        ModelReviewer("deepseek-v4-flash", "https://example.invalid", "x").complete([])


@pytest.mark.docker
def test_prepared_image_is_reset_to_dataset_base_before_agent_reads(image):
    sandbox = Sandbox(str(uuid.uuid4()), lambda *_: None, threading.Event(), files={}, image=image,
                      workspace="/testbed", writable=True, user="0:0")
    try:
        base = image_workspace.git(sandbox, "rev-parse", "HEAD").decode().strip()
        sandbox.execute({"command": "echo later > old.py; git add .; git -c user.name=test -c user.email=test@example.invalid commit -qm preparation"})
        assert image_workspace.git(sandbox, "rev-parse", "HEAD").decode().strip() != base
        image_workspace.initialize(sandbox, base)
        assert image_workspace.git(sandbox, "show", "HEAD:old.py") == b"old\n"
        assert image_workspace.capture(sandbox, True) == b""
    finally:
        sandbox.close()
