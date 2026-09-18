import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from minisweagent.models.test_models import DeterministicModel, make_output

from repopilot import recovery, review
from repopilot.reporting import usage_summary
from repopilot.repository import RepositoryTask, digest, load_snapshot, safe_path, validate_files
from repopilot.runtime import SYSTEM, ApprovalPaused, Cancelled, SafeModel, Sandbox, TracedAgent

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


def read_tree(sandbox, quiescent=False):
    guard = """
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
""" if quiescent else ""
    result = sandbox.container.exec_run(["timeout", "10", "python", "-I", "-c", guard + READ_TREE])
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


# A snapshot task needs a handful of steps; a real repository needs an exploration budget.
STEP_LIMIT = 20
IMAGE_STEP_LIMIT = 60
REVISION_STEPS = 5
WALL_TIME_SECONDS = 900
IMAGE_MEMORY = os.getenv("IMAGE_WORKSPACE_MEMORY", "2g")


def image_candidate(sandbox, workspace):
    """Candidate of an image workspace: the repository's own staged diff, read back as text.

    ``core.fileMode=false`` matches what the official harness does when it applies a patch, so
    a permission bit that a step happened to change is not reported as a code change.
    """
    def git(*args):
        return sandbox.container.exec_run(
            ["git", "-c", "core.fileMode=false", "-c", "core.autocrlf=false", "-c", "safe.directory=" + workspace,
             "-C", workspace, *args], user="1000:1000")

    staged = git("add", "-A", "--", ".")
    if staged.exit_code:
        raise RuntimeError("Cannot stage the repository inside the image workspace")
    patch = git("diff", "--cached", "--no-ext-diff", "--no-textconv", "HEAD").output.decode(errors="replace")
    names = git("diff", "--cached", "--name-only", "HEAD").output.decode(errors="replace").split()
    candidate = {}
    for name in names[:60]:
        safe_path(name)
        blob = git("show", f":{name}")
        if blob.exit_code:
            continue  # a deletion has no content to show
        text = blob.output.decode("utf-8", errors="replace")
        if len(text.encode()) <= 100000:
            candidate[name] = text
    return candidate, [name for name in names if name in candidate], patch


def review_before_acceptance(agent, run, spec, sandbox, snapshot, tracing, emit, instruction):
    """One review per candidate before acceptance, with a bounded number of revisions.

    ``snapshot`` returns ``(candidate, changed, patch)``: the candidate as text, the paths it
    touches, and its patch. A review that cannot be produced is reported rather than being
    treated as a clean candidate.
    """
    reviews, reviewed_sha, unresolved = [], None, 0
    while True:
        candidate, changed, patch = snapshot()
        candidate_sha = digest(candidate)
        if reviewed_sha is not None and reviewed_sha != candidate_sha:
            emit("REVIEW_INVALIDATED", {"reviewed_sha256": reviewed_sha, "candidate_sha256": candidate_sha,
                                        "reason": "修订后的候选版本已变化，原评审不再对应当前版本。"})
        if not review.enabled(run) or agent.review_rounds > review.max_rounds(run) or reviewed_sha == candidate_sha:
            return reviews, unresolved, candidate, changed, patch
        reviewed_sha = candidate_sha
        emit("REVIEW_REQUESTED", {"round": agent.review_rounds + 1, "reviewer": "read-only",
                                  "candidate_sha256": candidate_sha, "changed_files": changed,
                                  "policy": review.policy(run)})
        wall = time.time()
        try:
            evidence = sandbox.execute({"command": spec.testCommand})["output"]
            verdict = review.perform(review.reviewer_for(run, {}, candidate, changed, agent.review_rounds),
                                     run, spec, candidate, changed, patch, evidence, agent.review_rounds)
        except (Cancelled, ApprovalPaused):
            raise
        except Exception as error:
            if tracing:
                tracing.completed("review", wall, time.time(), round=agent.review_rounds + 1, error=str(error)[:200])
            emit("REVIEW_FAILED", {"round": agent.review_rounds + 1, "candidate_sha256": candidate_sha,
                                   "error": str(error)[:300]})
            reviews.append({"round": agent.review_rounds + 1, "status": "failed", "candidate_sha256": candidate_sha,
                            "error": str(error)[:300]})
            return reviews, unresolved, candidate, changed, patch
        if tracing:
            tracing.completed("review", wall, time.time(), round=agent.review_rounds + 1,
                              findings=len(verdict["findings"]), invalid=verdict["invalid_count"])
        agent.record_review(verdict)
        review_record = {"round": verdict["round"] + 1, "status": "ok", "candidate_sha256": candidate_sha,
                         "reviewer": verdict["reviewer"], "summary": verdict["summary"],
                         "findings": verdict["findings"], "invalid": verdict["invalid"], "parse_error": verdict["parse_error"],
                         "cost_usd": verdict["cost"]}
        reviews.append(review_record)
        emit("REVIEW_COMPLETED", {"round": review_record["round"], "candidate_sha256": candidate_sha,
                                  "reviewer": verdict["reviewer"], "summary": verdict["summary"],
                                  "findings": verdict["findings"], "invalid": verdict["invalid"],
                                  "invalid_count": verdict["invalid_count"], "parse_error": verdict["parse_error"]})
        blocking = review.blocking(verdict["findings"])
        unresolved = len(blocking)
        if not blocking:
            return reviews, unresolved, candidate, changed, patch
        if agent.review_rounds >= review.max_rounds(run):
            emit("REVIEW_UNRESOLVED", {"round": review_record["round"], "findings": blocking,
                                       "reason": f"已完成 {review.max_rounds(run)} 轮有限修订，评审仍有阻断项，交由独立验收判定。"})
            return reviews, unresolved, candidate, changed, patch
        agent.review_rounds += 1
        emit("REVISION_REQUESTED", {"round": agent.review_rounds, "review_round": review_record["round"],
                                    "findings": blocking})
        outcome = agent.revise(instruction, review.render_for_coder(blocking))
        if outcome.get("exit_status") != "Submitted":
            raise RuntimeError(f"Agent stopped during revision: {outcome.get('exit_status')}")


def execute_image_repository_run(run, spec, emit, cancelled, control=None, quota=None, tracing=None):
    """Work inside the repository a prepared image already carries.

    SWE-bench instance images ship their own checkout and their own pinned environment, so
    there is no bounded snapshot to copy in, nothing to restore, and the verdict belongs to the
    official harness. Two of this project's controls are defined on the snapshot workspace and
    therefore do not apply here: checkpoints (a lost attempt starts over) and approval pauses
    (there is no safe-boundary file tree to bind an approval to). Network stays off.
    """
    started = time.monotonic()
    folder = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / run["id"]
    folder.mkdir(parents=True, exist_ok=True)
    plan = control("resume-plan", {"generation": run["generation"]}) if control else {"mode": "fresh"}
    if plan["mode"] != "fresh":
        raise RuntimeError(f"An image workspace has no checkpoint to {plan['mode']} from")
    # A pinned conda environment plus the repository's own tooling needs more than a snapshot
    # sandbox: an instance image runs out of 256 MB while importing it.
    sandbox = Sandbox(run["id"], emit, cancelled, files={}, image=spec.sandboxImage,
                      workspace=spec.workspacePath, writable=True, memory=IMAGE_MEMORY)
    sandbox.tracing = tracing
    provenance = {
        "source": spec.source, "commit": spec.commit, "subdir": spec.subdir,
        "source_sha256": None, "verification_sha256": None,
        "image_id": sandbox.container.image.id, "image": spec.sandboxImage, "workspace_mode": "image",
        "workspace_path": spec.workspacePath, "allowed_paths": spec.allowedPaths,
        "task_sha256": hashlib.sha256(run["task"].encode()).hexdigest(),
        "context_mode": "full", "runner_version": "repository-v2-image",
        "approval_policy": "off", "review_policy": review.policy(run), "recovered_from": None,
    }
    try:
        emit("REPOSITORY_READY", provenance)
        if run.get("approvalPolicy", "auto") != "off":
            emit("APPROVAL_POLICY_IGNORED", {"reason": "镜像工作区没有可绑定的文件树快照，本次运行不做动作审批。"})
        system = SYSTEM.replace("You repair a Python repository in /workspace.",
                                f"You repair the Python repository checked out at {spec.workspacePath}.")
        # The images carry the base commit only, so history searches cost steps and find nothing.
        system += ("The checkout carries the base commit only: read the code and the report, then"
                   " change as little as possible and check it with the development test command.")
        system = SYSTEM.replace("Only pricing.py is accepted as the final source patch. Tests are independently verified.",
                                "The whole repository may be changed. The official harness judges the result with"
                                " the instance's own tests after you submit.")
        if run["mode"] == "demo":
            from repopilot.baseline import reference_commands
            commands = reference_commands(run.get("baselineId"), spec, {}, revise=review.enabled(run),
                                          workspace_image=True)
            model = DeterministicModel(outputs=[make_output("预设基线动作", [{"command": cmd}], cost=0)
                                                for cmd in commands], cost_per_call=0)
        else:
            if not os.getenv("OPENAI_API_KEY") or not os.getenv("MODEL_NAME"):
                raise ValueError("Live model configuration missing")
            model = SafeModel(model_name=f"openai/{os.environ['MODEL_NAME']}", cost_tracking="ignore_errors",
                              model_kwargs={"api_base": os.environ["MODEL_BASE_URL"], "timeout": 45,
                                            "num_retries": 0, "max_tokens": 1600, "temperature": 0.2})
        # A real repository costs far more exploration than a bounded snapshot task, so the
        # image workspace declares a larger step budget; the value is recorded in the provenance.
        step_limit = IMAGE_STEP_LIMIT + (REVISION_STEPS * review.max_rounds(run) if review.enabled(run) else 0)
        provenance["step_limit"] = step_limit
        agent = TracedAgent(model, sandbox, emit=emit, cancelled=cancelled, system_template=system,
                            context_mode="full", instance_template="Task: {{task}}",
                            step_limit=step_limit, cost_limit=0, wall_time_limit_seconds=WALL_TIME_SECONDS,
                            output_path=folder / "trajectory.json")
        agent.tracing = tracing
        instruction = run["task"] + "\nDevelopment test command: " + spec.testCommand
        outcome = agent.run(instruction)
        if outcome.get("exit_status") != "Submitted":
            raise RuntimeError(f"Agent stopped: {outcome.get('exit_status')}")
        reviews, unresolved, candidate, changed, patch = review_before_acceptance(
            agent, run, spec, sandbox, lambda: image_candidate(sandbox, spec.workspacePath), tracing, emit, instruction)
        if len(patch.encode()) > 500000:
            raise ValueError("Patch exceeds event size limit")
        (folder / "candidate.patch").write_text(patch, encoding="utf-8")
        emit("CANDIDATE", {"patch": patch, "changed_files": changed}, "VERIFYING")
    finally:
        sandbox.close()
    emit("VERIFICATION_DELEGATED", {"harness": "swebench", "patch_replayed": None,
                                    "reason": "官方 harness 用实例自带的测试补丁判定；本运行只负责产出补丁。"})
    verification = {"passed": None, "delegated": "swebench-harness", "output": "", "baseline": None,
                    "candidate": None, "patch_replayed": None}
    provenance["candidate_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
    (folder / "manifest.json").write_text(json.dumps({"spec": spec.model_dump(), "task": run["task"],
                                                      "provenance": provenance}), encoding="utf-8")
    result = {"patch": patch, "changed_files": changed, "provenance": provenance, "verification": verification,
              "review": {"policy": review.policy(run), "rounds": agent.review_rounds,
                         "max_rounds": review.max_rounds(run), "unresolved_blocking": unresolved, "reviews": reviews},
              "candidate_tree_stored": False,
              **usage_summary(agent.full_messages, agent.n_calls, run["mode"]), "mode": run["mode"],
              "context_compactions": agent.compactions, "trace_id": tracing.trace_id() if tracing else None,
              "trace_parent_span_id": tracing.parent_span_id() if tracing else None,
              "duration_seconds": round(time.monotonic() - started, 2), "cost_usd": None,
              "artifact_path": f"{run['id']}/"}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def execute_repository_run(run, emit, cancelled, control=None, quota=None, tracing=None):
    started = time.monotonic()
    spec = RepositoryTask.model_validate(run["spec"])
    if spec.workspaceMode == "image":
        return execute_image_repository_run(run, spec, emit, cancelled, control, quota, tracing)
    source = load_snapshot(spec)
    folder = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / run["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "source.json").write_text(json.dumps(source), encoding="utf-8")
    plan = control("resume-plan", {"generation": run["generation"]}) if control else {"mode": "fresh"}
    if plan["mode"] == "wait":
        checkpoint = recovery.load_resume(run, plan["checkpoint"], source)
        approval = plan.get("approval") or control("approvals", recovery.approval_request(run, checkpoint))
        emit("APPROVAL_WAIT", {"approval_id": approval["id"], "checkpoint": plan["checkpoint"]["id"],
                               "reason": "审批尚未决定；本次执行停在检查点，等待用户决定后由新执行代次继续。"},
             "WAITING_APPROVAL")
        return {"paused": {"kind": "approval", "approval_id": approval["id"]}, "verification": {"passed": False}}
    restored = recovery.load_resume(run, plan["checkpoint"], source) if plan["mode"] == "restore" else None
    files = restored.files if restored else source
    decision = recovery.resume_action(plan, restored) if restored else None
    if restored:
        # The interrupted attempt's own trajectory stays as evidence next to the resumed one.
        recovery.preserve_trajectory(folder, restored.binding["generation"])
    sandbox = Sandbox(run["id"], emit, cancelled, files=files)
    sandbox.tracing = tracing
    image_id = sandbox.container.image.id
    provenance = {
        "source": spec.source, "commit": spec.commit, "subdir": spec.subdir,
        "source_sha256": digest(source), "verification_sha256": digest(spec.verificationFiles),
        "image_id": image_id, "allowed_paths": spec.allowedPaths,
        "task_sha256": hashlib.sha256(run["task"].encode()).hexdigest(),
        "context_mode": run.get("contextMode", "full"), "runner_version": "repository-v1",
        "approval_policy": run.get("approvalPolicy", "auto"),
        "review_policy": review.policy(run),
        "recovered_from": {"generation": restored.binding["generation"], "checkpoint_id": restored.id,
                           "sequence": restored.sequence, "phase": restored.phase,
                           "approval_id": decision["approval_id"] if decision else None} if restored else None,
    }
    context_manager = None
    try:
        if restored:
            emit("RECOVERED", {"generation": run["generation"], "from_generation": restored.binding["generation"],
                               "checkpoint_id": restored.id, "sequence": restored.sequence, "phase": restored.phase,
                               "model_calls": restored.state.model_calls, "tool_calls": restored.state.tool_calls,
                               "workspace_sha256": restored.workspace_sha256,
                               "approval_id": decision["approval_id"] if decision else None,
                               "decision": decision["kind"] if decision else "continue"})
        else:
            emit("REPOSITORY_READY", provenance)
        system = SYSTEM.replace("Only pricing.py is accepted as the final source patch. Tests are independently verified.",
                                "Only these paths may change: " + ", ".join(spec.allowedPaths)
                                + ". Other files must remain unchanged. Independent tests run after submission.")
        if run["mode"] == "demo":
            # A demo reference is only loaded from the trusted baseline catalog, never from an API request.
            from repopilot.baseline import reference_commands
            commands = reference_commands(run.get("baselineId"), spec, source, revise=review.enabled(run))
            model = DeterministicModel(outputs=[make_output("预设基线动作", [{"command": cmd}], cost=0)
                                                for cmd in commands], cost_per_call=0)
        else:
            if not os.getenv("OPENAI_API_KEY") or not os.getenv("MODEL_NAME"):
                raise ValueError("Live model configuration missing")
            model = SafeModel(model_name=f"openai/{os.environ['MODEL_NAME']}", cost_tracking="ignore_errors",
                              model_kwargs={"api_base": os.environ["MODEL_BASE_URL"], "timeout": 45,
                                            "num_retries": 0, "max_tokens": 1600, "temperature": 0.2})
        # A revision round costs the coder steps as well, so the declared budget covers them
        # instead of letting the review loop run the attempt into its step limit.
        step_limit = STEP_LIMIT + (REVISION_STEPS * review.max_rounds(run) if review.enabled(run) else 0)
        agent = TracedAgent(model, sandbox, emit=emit, cancelled=cancelled, system_template=system,
                            context_mode=run.get("contextMode", "full"),
                            instance_template="Task: {{task}}",
                            step_limit=step_limit, cost_limit=0, wall_time_limit_seconds=360,
                            output_path=folder / "trajectory.json")
        agent.tracing = tracing
        if run.get("contextMode") == "managed":
            from repopilot.context import ContextManager
            context_manager = ContextManager(run, source, emit, folder)
            if run["mode"] == "live":
                model.config.model_kwargs["max_tokens"] = context_manager.config.output
            agent.context_manager = context_manager
            sandbox.archive_logs = True
        agent.enable_checkpoints(run, source)
        agent.enable_approvals(run.get("approvalPolicy", "auto"), spec.allowedPaths,
                               (lambda payload: control("approvals", payload)) if control else None)
        agent.enable_quota(quota)
        if restored:
            agent.restore_checkpoint(plan["checkpoint"], allow_previous_generation=True, resume_action=decision)
        instruction = run["task"] + "\nDevelopment test command: " + spec.testCommand
        outcome = agent.run(instruction)
        if outcome.get("exit_status") != "Submitted":
            raise RuntimeError(f"Agent stopped: {outcome.get('exit_status')}")
        def snapshot():
            """The candidate as text, the paths it touches, and its patch, replayed for acceptance."""
            candidate = read_tree(sandbox)
            changed = sorted(name for name in source.keys() | candidate.keys() if source.get(name) != candidate.get(name))
            if not set(changed).issubset(spec.allowedPaths):
                raise ValueError("Candidate changed paths outside allowedPaths")
            patch, replayed = make_patch_and_reapply(source, candidate, folder)
            if len(patch.encode()) > 500000:
                raise ValueError("Patch exceeds event size limit")
            (folder / "candidate.patch").write_text(patch, encoding="utf-8")
            replayed_tree["replayed"] = replayed
            return candidate, changed, patch

        replayed_tree = {}
        reviews, unresolved, candidate, changed, patch = review_before_acceptance(
            agent, run, spec, sandbox, snapshot, tracing, emit, instruction)
        # The patch of the last snapshot is the candidate both branches record.
        replayed = replayed_tree["replayed"]
        emit("CANDIDATE", {"patch": patch, "changed_files": changed}, "VERIFYING")
        # The candidate tree is kept next to the source snapshot so the workbench can render a
        # real base/candidate diff. An oversized tree is reported instead of being dropped.
        candidate_tree = json.dumps(candidate, ensure_ascii=False)
        tree_stored = len(candidate_tree.encode()) <= 2_000_000
        if tree_stored:
            (folder / "candidate.json").write_text(candidate_tree, encoding="utf-8")
    finally:
        if context_manager:
            context_manager.close()
        sandbox.close()
    if spec.verificationMode == "harness":
        # The official harness applies the instance's own test patch in its own image and judges
        # the result; this run proves only that the candidate patch replays onto the pinned commit.
        emit("VERIFICATION_DELEGATED", {"harness": "swebench", "patch_replayed": True,
                                        "reason": "官方 harness 用实例自带的测试补丁判定；本运行只保证候选补丁能干净地重放到固定提交。"})
        passed, verification = None, {"passed": None, "delegated": "swebench-harness", "output": "",
                                      "baseline": None, "candidate": None, "patch_replayed": True}
    else:
        wall = time.time()
        baseline = verify(source, spec, run["id"], cancelled, image_id)
        if tracing:
            tracing.completed("acceptance.baseline", wall, time.time(), tests=baseline["report"].get("tests"),
                              returncode=baseline["returncode"])
        wall = time.time()
        verified = verify(replayed, spec, run["id"], cancelled, image_id)
        if tracing:
            tracing.completed("acceptance.candidate", wall, time.time(), tests=verified["report"].get("tests"),
                              returncode=verified["returncode"])
        before, after = baseline["report"], verified["report"]
        passed = bool(patch and before and after and before["tests"] > 0
                      and before["failures"] > 0 and before["errors"] == 0 and before["skipped"] == 0
                      and baseline["returncode"] != 0 and verified["returncode"] == 0
                      and before["ids"] == after["ids"] and after["successful"] and after["skipped"] == 0)
        verification = {"passed": passed, "output": verified["output"], "baseline": baseline,
                        "candidate": verified, "patch_replayed": True}
    provenance.update(model=os.getenv("MODEL_NAME") if run["mode"] == "live" else "deterministic",
                      temperature=0.2, max_output_tokens=context_manager.config.output if context_manager else 1600, step_limit=step_limit,
                      prompt_sha256=hashlib.sha256(system.encode()).hexdigest(),
                      compression_strategy="structured-deterministic-v1" if context_manager else "extractive-v1",
                      context_config=context_manager.config.model_dump() if context_manager else None,
                      candidate_sha256=digest(replayed), patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    (folder / "manifest.json").write_text(json.dumps({"spec": spec.model_dump(), "task": run["task"],
                                                      "provenance": provenance}), encoding="utf-8")
    result = {"patch": patch, "changed_files": changed, "provenance": provenance,
              "verification": verification,
              "review": {"policy": review.policy(run), "rounds": agent.review_rounds,
                         "max_rounds": review.max_rounds(run), "unresolved_blocking": unresolved,
                         "reviews": reviews},
              "candidate_tree_stored": tree_stored,
              "trace_id": tracing.trace_id() if tracing else None,
              "trace_parent_span_id": tracing.parent_span_id() if tracing else None,
              **usage_summary(agent.full_messages, agent.n_calls, run["mode"]), "mode": run["mode"],
              "context_compactions": agent.compactions,
              "duration_seconds": round(time.monotonic() - started, 2), "cost_usd": None,
              "artifact_path": f"{run['id']}/"}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
