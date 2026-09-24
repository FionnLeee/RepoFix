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
    monkeypatch.setenv("MODEL_POLICY", "free-quota")
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


@pytest.mark.docker
def test_image_revision_recovery_preserves_review_ledger_and_shared_budget(image, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from repopilot import review
    from repopilot.repository_runtime import review_before_acceptance

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    run = {"id": str(uuid.uuid4()), "generation": 1, "task": "fix", "mode": "demo", "reviewBudget": "shared"}
    folder = tmp_path / run["id"]
    folder.mkdir()
    outputs = [make_output("edit", [{"command": "echo wrong > old.py"}]),
               make_output("submit", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
               make_output("REVIEW-RESPONSE 1: fixed - changed old.py", [{"command": "echo fixed > old.py"}]),
               make_output("submit", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])]
    answers = iter([
        {"findings": [{"file": "old.py", "line": 1, "severity": "blocking", "finding": "wrong"}]},
        {"findings": [], "previous": [{"id": 1, "status": "fixed", "note": "verified fixed content"}]},
    ])
    requests = []

    def complete(messages):
        requests.append(messages)
        return {"content": json.dumps(next(answers)), "cost": 0}

    monkeypatch.setattr(review, "reviewer_for", lambda *args: SimpleNamespace(complete=complete))

    class LostWorker(BaseException):
        pass

    class InterruptedAgent(TracedAgent):
        def save(self, path, *extra):
            result = super().save(path, *extra)
            if self.n_calls == 4:
                raise LostWorker()
            return result

    def build(cls, generation):
        sandbox = Sandbox(run["id"], lambda *_: None, threading.Event(), files={}, image=image,
                          workspace="/testbed", writable=True, user="0:0")
        source = image_workspace.initialize(sandbox)
        agent = cls(DeterministicModel(outputs=outputs), sandbox, emit=lambda *_: None,
                    cancelled=threading.Event(), system_template="", instance_template="{{task}}",
                    step_limit=6, cost_limit=0, output_path=folder / "trajectory.json")
        agent.enable_checkpoints({**run, "generation": generation}, source)
        return agent, sandbox

    def review_loop(agent, sandbox):
        return review_before_acceptance(agent, run, SimpleNamespace(allowedPaths=[], testCommand="true"),
            sandbox, lambda: image_workspace.candidate(sandbox, folder), None, lambda *_: None, "fix")

    first, sandbox = build(InterruptedAgent, 1)
    try:
        assert first.run("fix")["exit_status"] == "Submitted"
        with pytest.raises(LostWorker):
            review_loop(first, sandbox)
        reference = first.checkpoints.latest
        checkpoint = first.checkpoints.load(reference, for_resume=True)
        assert checkpoint.state.review_state["phase"] == "revising"
        assert checkpoint.state.review_state["previous"][0]["coder"]["status"] == "fixed"
    finally:
        sandbox.close()
    resumed, sandbox = build(TracedAgent, 2)
    try:
        image_workspace.restore(sandbox, checkpoint.files)
        resumed.restore_checkpoint(reference, allow_previous_generation=True)
        assert resumed.n_calls == 4 and resumed.review_rounds == 1
        assert resumed.run("fix")["exit_status"] == "Submitted"
        reviews, unresolved, dispositions, *_ = review_loop(resumed, sandbox)
        assert len(reviews) == 2 and len(requests) == 2 and resumed.n_calls == 6
        assert unresolved == 0 and len(dispositions) == 1
        assert dispositions[0]["outcome"] == "fixed"
        assert dispositions[0]["coder"]["note"] == "changed old.py"
        assert "changed old.py" in requests[1][1]["content"]
        assert json.loads((folder / "review.json").read_text())["phase"] == "complete"
    finally:
        sandbox.close()
