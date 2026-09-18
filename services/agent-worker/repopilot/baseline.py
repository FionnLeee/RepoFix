import json
import os
import shlex
from pathlib import Path


def task_definitions():
    path = Path(os.getenv("BASELINE_DEFINITIONS", "benchmarks/tasks.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def _write_script(files):
    script = "import json; from pathlib import Path; data=json.loads(" + repr(json.dumps(files)) + "); "
    script += "[(Path(k).write_text(v,encoding='utf-8')) for k,v in data.items()]"
    return "python -c " + shlex.quote(script)


def reference_commands(task_id, spec, files, revise=False, workspace_image=False):
    """The scripted demo actions for a baseline task.

    A snapshot run is checked against the catalog's own file set; an image run copies nothing
    in — the repository is already in the image — so only the task identity and the allowed
    paths have to match.
    """
    task = next((t for t in task_definitions() if t["id"] == task_id), None)
    if (not task or spec.allowedPaths != sorted(task["reference"])
            or (not workspace_image and files != task["files"])):
        raise ValueError("Deterministic repository mode requires a matching built-in baseline task")
    commands = ["find . -type f", spec.testCommand, _write_script(task["reference"]), spec.testCommand,
                "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
    if not revise:
        return commands
    # The revision round the reviewer asks for: the same patch, re-applied with an explicit
    # revision marker, so the candidate really changes and the old review becomes stale.
    revised = dict(task["reference"])
    first = sorted(revised)[0]
    revised[first] = "# 评审修订：按评审意见重新核对后提交。\n" + revised[first]
    return commands + ["find . -type f", spec.testCommand, _write_script(revised), spec.testCommand,
                       "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
