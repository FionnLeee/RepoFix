import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from minisweagent.models.test_models import DeterministicModel, make_output

from repopilot.reporting import usage_summary
from repopilot.repository import RepositoryTask, digest, load_snapshot, validate_files
from repopilot.runtime import SYSTEM, SafeModel, Sandbox, TracedAgent

# Runs in the container with isolated Python, and refuses links at every traversed directory.
READ_TREE = """
import os, stat, json
files = {}
def walk(fd, prefix=''):
    for name in os.listdir(fd):
        if name == '__pycache__':
            continue
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        path = prefix + name
        if stat.S_ISDIR(st.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try: walk(child, path + '/')
            finally: os.close(child)
        else:
            assert stat.S_ISREG(st.st_mode) and st.st_size <= 100000, 'Special file or size limit'
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                assert stat.S_ISREG(os.fstat(child).st_mode)
                files[path] = os.read(child, 100001).decode('utf-8')
            finally: os.close(child)
            assert len(files) <= 200 and sum(len(v.encode()) for v in files.values()) <= 4194304
root = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try: walk(root)
finally: os.close(root)
print(json.dumps(files, ensure_ascii=True))
"""

VERIFY = """
import sys, unittest, json, io
sys.path.insert(0, '/workspace')
suite = unittest.defaultTestLoader.discover('/workspace/_repopilot_verify', pattern='test_*.py')
def ids(suite):
    result = []
    for item in suite:
        result.extend(ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
    return result
test_ids = sorted(ids(suite))
log = io.StringIO()
result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
print(log.getvalue()[-12000:])
print('REPOPILOT_REPORT=' + json.dumps(dict(tests=result.testsRun, ids=test_ids,
    failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
    successful=result.wasSuccessful())))
sys.exit(0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1)
"""


def read_tree(sandbox):
    result = sandbox.container.exec_run(["timeout", "10", "python", "-I", "-c", READ_TREE])
    if result.exit_code or len(result.output) > 12 * 1024 * 1024:
        raise ValueError("Candidate tree contains invalid files or exceeds limits")
    return validate_files(json.loads(result.output))


def make_patch_and_reapply(original, candidate, folder):
    """Generate patch with Git; reapply it to a separate pristine snapshot."""
    def write(root, files):
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode())

    env = os.environ.copy()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        root = Path(temporary)

        def git(*args, data=None):
            return subprocess.run(
                ["git", "-c", "core.autocrlf=false", "-c", "core.filemode=false", "-C", str(root), *args],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=env, timeout=15,
            ).stdout

        git("init", "-q")
        write(root, original)
        git("add", "-f", "--", ".")
        tree = git("write-tree").decode().strip()
        for name in original:
            (root / name).unlink()
        write(root, candidate)
        git("add", "-f", "-A", "--", ".")
        patch = git("diff", "--cached", "--no-ext-diff", "--no-textconv", tree, "--").decode()
        for name in candidate:
            (root / name).unlink()
        write(root, original)
        if patch:
            git("apply", "--check", "--whitespace=nowarn", "-", data=patch.encode())
            git("apply", "--whitespace=nowarn", "-", data=patch.encode())
        replayed = {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes().decode("utf-8")
                    for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}
        if replayed != candidate:
            raise ValueError("Patch replay does not reproduce candidate snapshot")
        return patch, replayed


def verify(files, spec, run_id, cancelled, image):
    combined = {**files, **{f"_repopilot_verify/{k}": v for k, v in spec.verificationFiles.items()}}
    sandbox = Sandbox(run_id, lambda *_: None, cancelled, files=combined, image=image)
    try:
        # The caller resolves and sets an immutable image ID for this run.
        if sandbox.container.image.id != image:
            raise ValueError("Verification image changed during the run")
        result = sandbox.execute({"command": "python -I -c " + shlex.quote(VERIFY)})
        reports = [line.removeprefix("REPOPILOT_REPORT=") for line in result["output"].splitlines()
                   if line.startswith("REPOPILOT_REPORT=")]
        report = json.loads(reports[-1]) if len(reports) == 1 else None
        return {**result, "report": report}
    finally:
        sandbox.close()


def replay_patch(original, patch, folder):
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        root = Path(temporary)
        for name, content in validate_files(original).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode())
        env = os.environ.copy()
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for args, payload in [(["init", "-q"], None),
                              (["apply", "--check", "--whitespace=nowarn", "-"], patch.encode()),
                              (["apply", "--whitespace=nowarn", "-"], patch.encode())]:
            subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(root), *args],
                           input=payload, check=True, capture_output=True, timeout=15, env=env)
        files = {str(p.relative_to(root)).replace("\\", "/"): p.read_bytes().decode()
                 for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}
        return validate_files(files)


def execute_repository_run(run, emit, cancelled):
    started = time.monotonic()
    spec = RepositoryTask.model_validate(run["spec"])
    files = load_snapshot(spec)
    folder = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / run["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "source.json").write_text(json.dumps(files), encoding="utf-8")
    sandbox = Sandbox(run["id"], emit, cancelled, files=files)
    image_id = sandbox.container.image.id
    provenance = {
        "source": spec.source, "commit": spec.commit, "subdir": spec.subdir,
        "source_sha256": digest(files), "verification_sha256": digest(spec.verificationFiles),
        "image_id": image_id, "allowed_paths": spec.allowedPaths,
        "task_sha256": hashlib.sha256(run["task"].encode()).hexdigest(),
        "context_mode": run.get("contextMode", "full"), "runner_version": "repository-v1",
    }
    try:
        emit("REPOSITORY_READY", provenance)
        system = SYSTEM.replace("Only pricing.py is accepted as the final source patch. Tests are independently verified.",
                                "Only these paths may change: " + ", ".join(spec.allowedPaths)
                                + ". Other files must remain unchanged. Independent tests run after submission.")
        if run["mode"] == "demo":
            # A demo reference is only loaded from the trusted baseline catalog, never from an API request.
            from repopilot.baseline import reference_commands
            commands = reference_commands(run.get("baselineId"), spec, files)
            model = DeterministicModel(outputs=[make_output("预设基线动作", [{"command": cmd}], cost=0)
                                                for cmd in commands], cost_per_call=0)
        else:
            if not os.getenv("OPENAI_API_KEY") or not os.getenv("MODEL_NAME"):
                raise ValueError("Live model configuration missing")
            model = SafeModel(model_name=f"openai/{os.environ['MODEL_NAME']}", cost_tracking="ignore_errors",
                              model_kwargs={"api_base": os.environ["MODEL_BASE_URL"], "timeout": 45,
                                            "num_retries": 0, "max_tokens": 1600, "temperature": 0.2})
        agent = TracedAgent(model, sandbox, emit=emit, cancelled=cancelled, system_template=system,
                            context_mode=run.get("contextMode", "full"),
                            instance_template="Task: {{task}}",
                            step_limit=20, cost_limit=0, wall_time_limit_seconds=360,
                            output_path=folder / "trajectory.json")
        outcome = agent.run(run["task"] + "\nDevelopment test command: " + spec.testCommand)
        if outcome.get("exit_status") != "Submitted":
            raise RuntimeError(f"Agent stopped: {outcome.get('exit_status')}")
        candidate = read_tree(sandbox)
        changed = sorted(name for name in files.keys() | candidate.keys() if files.get(name) != candidate.get(name))
        if not set(changed).issubset(spec.allowedPaths):
            raise ValueError("Candidate changed paths outside allowedPaths")
        patch, replayed = make_patch_and_reapply(files, candidate, folder)
        if len(patch.encode()) > 500000:
            raise ValueError("Patch exceeds event size limit")
        (folder / "candidate.patch").write_text(patch, encoding="utf-8")
        emit("CANDIDATE", {"patch": patch, "changed_files": changed}, "VERIFYING")
    finally:
        sandbox.close()
    baseline = verify(files, spec, run["id"], cancelled, image_id)
    verified = verify(replayed, spec, run["id"], cancelled, image_id)
    before, after = baseline["report"], verified["report"]
    passed = bool(patch and before and after and before["tests"] > 0
                  and before["failures"] > 0 and before["errors"] == 0 and before["skipped"] == 0
                  and baseline["returncode"] != 0 and verified["returncode"] == 0
                  and before["ids"] == after["ids"] and after["successful"] and after["skipped"] == 0)
    provenance.update(model=os.getenv("MODEL_NAME") if run["mode"] == "live" else "deterministic",
                      temperature=0.2, max_output_tokens=1600, step_limit=20,
                      prompt_sha256=hashlib.sha256(system.encode()).hexdigest(),
                      compression_strategy="extractive-v1", compression_threshold_characters=3500,
                      candidate_sha256=digest(replayed), patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    (folder / "manifest.json").write_text(json.dumps({"spec": spec.model_dump(), "task": run["task"],
                                                      "provenance": provenance}), encoding="utf-8")
    result = {"patch": patch, "changed_files": changed, "provenance": provenance,
              "verification": {"passed": passed, "output": verified["output"], "baseline": baseline,
                               "candidate": verified, "patch_replayed": True},
              **usage_summary(agent.full_messages, agent.n_calls, run["mode"]), "mode": run["mode"],
              "context_compactions": agent.compactions,
              "duration_seconds": round(time.monotonic() - started, 2), "cost_usd": None,
              "artifact_path": f"{run['id']}/"}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
