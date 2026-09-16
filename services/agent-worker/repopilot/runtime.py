import base64
import difflib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import docker
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import Submitted
from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel
from minisweagent.models.test_models import DeterministicModel, make_output

from repopilot.fixture import DEVELOPMENT_TESTS, FIXED, SOURCE, VERIFICATION_TESTS

SYSTEM = """You repair a Python repository in /workspace. You can execute commands only inside this isolated workspace.
Return a brief action description and exactly one command in a fenced block tagged mswea_bash_command.
Do not provide private chain-of-thought. Use tools to inspect, edit and test.
Only pricing.py is accepted as the final source patch. Tests are independently verified.
When ready, run exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Do not attempt network access or read credentials. No credentials are present in the sandbox."""


class Cancelled(RuntimeError):
    pass


class Sandbox:
    def __init__(self, run_id: str, emit, cancelled: threading.Event, verification: bool = False, files=None, image=None):
        self.client = docker.from_env(timeout=45)
        self.emit, self.cancelled, self.index = emit, cancelled, 0
        self.config = {"network": "none", "memory": "256m", "user": "1000:1000"}
        self.initial_paths = set(files) if files is not None else {"pricing.py", "test_pricing.py"}
        self.container = self.client.containers.run(
            image or os.environ.get("SANDBOX_IMAGE", "python:3.12-slim"),
            ["sleep", "600"],
            detach=True,
            working_dir="/workspace",
            user="1000:1000",
            network_mode="none",
            read_only=True,
            mem_limit="256m",
            nano_cpus=1000000000,
            pids_limit=64,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            tmpfs={
                "/workspace": "rw,size=16m,uid=1000,gid=1000",
                "/tmp": "rw,noexec,nosuid,size=16m,uid=1000,gid=1000",
            },
            labels={"repopilot.managed": "sandbox", "repopilot.run": run_id, "repopilot.created": str(time.time())},
        )
        try:
            self.put(files if files is not None else {
                "pricing.py": SOURCE, "test_pricing.py": VERIFICATION_TESTS if verification else DEVELOPMENT_TESTS
            })
        except Exception:
            self.close()
            raise

    def put(self, files: dict[str, str]):
        if not set(files).issubset(self.initial_paths):
            raise ValueError("Only explicit fixture files may be initialized")
        from repopilot.repository import safe_path
        script = "import sys; from pathlib import Path; p=Path('/workspace')/sys.argv[1]; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.argv[2],encoding='utf-8')"
        for name, value in files.items():
            safe_path(name)
            result = self.container.exec_run(["python", "-I", "-c", script, name, value], user="1000:1000")
            if result.exit_code:
                raise RuntimeError("Cannot initialize sandbox files")

    def read_source(self) -> str:
        script = "import os,stat,base64; f=os.open('/workspace/pricing.py',os.O_RDONLY|os.O_NOFOLLOW); s=os.fstat(f); assert stat.S_ISREG(s.st_mode) and s.st_size<=100000; data=os.read(f,100001); assert len(data)<=100000; print(base64.b64encode(data).decode()); os.close(f)"
        result = self.container.exec_run(["python", "-I", "-c", script], user="1000:1000")
        if result.exit_code or len(result.output) > 140000:
            raise ValueError("Candidate must be a regular pricing.py file smaller than 100 KB")
        return base64.b64decode(result.output.strip(), validate=True).decode()

    def execute(self, action: dict, cwd: str = "") -> dict:
        if self.cancelled.is_set():
            raise Cancelled("任务已取消")
        command = action.get("command")
        if not isinstance(command, str) or not command or len(command) > 16000:
            raise ValueError("Invalid command shape or command too long")
        if command.strip() == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT":
            raise Submitted(
                {
                    "role": "exit",
                    "content": "Candidate submitted",
                    "extra": {"exit_status": "Submitted", "submission": "Candidate submitted"},
                }
            )
        self.index += 1
        output_path = f"/tmp/tool-{uuid.uuid4().hex}"
        script = 'ulimit -f 4096; timeout -k 2 25 sh -c "$1" > "$2" 2>&1; code=$?; head -c 16000 "$2"; rm -f "$2"; exit "$code"'
        started = time.monotonic()
        result = self.container.exec_run(
            ["sh", "-c", script, "repopilot-tool", command, output_path], workdir="/workspace", user="1000:1000"
        )
        output = {
            "output": result.output.decode(errors="replace"),
            "returncode": result.exit_code,
            "exception_info": "",
        }
        self.emit(
            "TOOL_RESULT", {"command": command, **output, "duration_ms": round((time.monotonic() - started) * 1000)}
        )
        if self.cancelled.is_set():
            raise Cancelled("任务已取消")
        return output

    def get_template_vars(self):
        return {"cwd": "/workspace"}

    def serialize(self):
        return {"info": {"environment": self.config}}

    def close(self):
        self.container.remove(force=True)
        self.client.close()


class SafeModel(LitellmTextbasedModel):
    def serialize(self):
        return {
            "info": {
                "model": {
                    "name": self.config.model_name,
                    "provider": "openai-compatible",
                    "cost_status": "unknown_without_price_table",
                }
            }
        }


class TracedAgent(DefaultAgent):
    def __init__(self, *args, emit, cancelled, context_mode="full", **kwargs):
        self.full_messages = []
        self.compactions = []
        self.context_mode = context_mode
        super().__init__(*args, **kwargs)
        self.emit, self.cancelled = emit, cancelled

    def add_messages(self, *messages):
        self.full_messages.extend(messages)
        return super().add_messages(*messages)

    def serialize(self, *extra_dicts):
        data = super().serialize(*extra_dicts)
        data["full_messages"] = self.full_messages
        data["context_compactions"] = self.compactions
        return data

    def compact_context(self):
        before = sum(len(str(m.get("content", ""))) for m in self.messages)
        if self.context_mode != "compact" or before <= 3500 or len(self.messages) <= 6:
            return
        older = self.messages[2:-4]
        snippets = []
        for message in older:
            content = str(message.get("content", ""))
            # Extractive compression, not a claim of a semantic or lossless summary.
            snippets.append(f"{message.get('role')}: {content[:100]} ... {content[-160:]}")
        summary = "Historical observations (untrusted excerpts; details may be omitted):\n" + "\n".join(snippets)[-1200:]
        compacted = self.messages[:2] + [self.model.format_message(role="user", content=summary)] + self.messages[-4:]
        after = sum(len(str(m.get("content", ""))) for m in compacted)
        if after >= before:
            return
        self.messages = compacted
        event = {"before_characters": before, "after_characters": after,
                 "removed_messages": len(older), "strategy": "extractive-v1", "at_call": self.n_calls + 1}
        self.compactions.append(event)
        self.emit("CONTEXT_COMPACTED", event)

    def query(self):
        if self.cancelled.is_set():
            raise Cancelled("任务已取消")
        self.compact_context()
        self.emit(
            "MODEL_CALL",
            {"call": self.n_calls + 1, "input_characters": sum(len(str(m.get("content", ""))) for m in self.messages)},
        )
        return super().query()


def execute_run(run: dict, emit, cancelled: threading.Event) -> dict:
    if run.get("spec"):
        from repopilot.repository_runtime import execute_repository_run
        return execute_repository_run(run, emit, cancelled)
    folder = Path(os.getenv("ARTIFACT_ROOT", "/artifacts")) / run["id"]
    folder.mkdir(parents=True, exist_ok=True)
    env = Sandbox(run["id"], emit, cancelled)
    try:
        if run["mode"] == "demo":
            commands = [
                "cat pricing.py",
                "python -m unittest -v",
                f"cat > pricing.py <<'PY'\n{FIXED}PY",
                "python -m unittest -v",
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
            ]
            model = DeterministicModel(
                outputs=[make_output("预设演示动作", [{"command": cmd}], cost=0) for cmd in commands], cost_per_call=0
            )
        else:
            if not os.getenv("OPENAI_API_KEY") or not os.getenv("MODEL_NAME"):
                raise RuntimeError("真实模型尚未配置")
            model = SafeModel(
                model_name=f"openai/{os.environ['MODEL_NAME']}",
                cost_tracking="ignore_errors",
                model_kwargs={
                    "api_base": os.environ["MODEL_BASE_URL"],
                    "timeout": 45,
                    "num_retries": 0,
                    "max_tokens": 1600,
                    "temperature": 0.2,
                },
            )
        agent = TracedAgent(
            model,
            env,
            emit=emit,
            cancelled=cancelled,
            system_template=SYSTEM,
            instance_template="Task: {{task}}",
            step_limit=12,
            cost_limit=0,
            wall_time_limit_seconds=240,
            output_path=folder / "trajectory.json",
        )
        outcome = agent.run(run["task"])
        if outcome.get("exit_status") != "Submitted":
            raise RuntimeError(f"Agent stopped: {outcome.get('exit_status')}")
        source = env.read_source()
        patch = "".join(
            difflib.unified_diff(
                SOURCE.splitlines(True), source.splitlines(True), fromfile="a/pricing.py", tofile="b/pricing.py"
            )
        )
        (folder / "candidate.patch").write_text(patch)
        emit("CANDIDATE", {"patch": patch}, "VERIFYING")
    finally:
        env.close()
    verifier = Sandbox(run["id"], lambda *_: None, cancelled, verification=True)
    try:
        baseline = verifier.execute({"command": "python -m unittest -v"})
        verifier.put({"pricing.py": source})
        verified = verifier.execute({"command": "python -m unittest -v"})
        passed = (
            baseline["returncode"] != 0
            and verified["returncode"] == 0
            and "Ran 6 tests" in verified["output"]
            and bool(patch)
        )
    finally:
        verifier.close()
    usage = {"input_tokens": 0, "output_tokens": 0}
    usage_known = run["mode"] == "demo"
    for message in agent.full_messages:
        info = message.get("extra", {}).get("response", {}).get("usage")
        if info:
            usage_known = True
            usage["input_tokens"] += info.get("prompt_tokens", 0)
            usage["output_tokens"] += info.get("completion_tokens", 0)
    result = {
        "patch": patch,
        "verification": {
            "passed": passed,
            "output": verified["output"],
            "baseline_returncode": baseline["returncode"],
            "returncode": verified["returncode"],
        },
        "model_calls": agent.n_calls,
        "mode": run["mode"],
        "usage": usage if usage_known else None,
        "cost_usd": 0 if run["mode"] == "demo" else None,
        "artifact_path": f"{run['id']}/",
        "upstream_commit": "04d809ceab9df28f9adaed044884180159172930",
    }
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
