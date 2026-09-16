import json
import os
import shlex
from pathlib import Path


def task_definitions():
    path = Path(os.getenv("BASELINE_DEFINITIONS", "benchmarks/tasks.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def reference_commands(task_id, spec, files):
    task = next((t for t in task_definitions() if t["id"] == task_id), None)
    if not task or files != task["files"] or spec.allowedPaths != sorted(task["reference"]):
        raise ValueError("Deterministic repository mode requires a matching built-in baseline task")
    script = "import json; from pathlib import Path; data=json.loads(" + repr(json.dumps(task["reference"])) + "); "
    script += "[(Path(k).write_text(v,encoding='utf-8')) for k,v in data.items()]"
    return ["find . -type f", spec.testCommand, "python -c " + shlex.quote(script), spec.testCommand,
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
