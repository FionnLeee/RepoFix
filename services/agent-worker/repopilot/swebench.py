"""Translate between SWE-bench instances and RepoPilot runs, without judging either side.

RepoPilot decides whether a candidate passes *its* pinned acceptance tests; the official
harness applies an instance's own test patch in the official image and decides FAIL_TO_PASS
and PASS_TO_PASS. The two answers are not interchangeable, so this module only translates:
an instance becomes a repository task, a finished run becomes a prediction the harness can
read, and the harness report is summarised for the run record. Runs translated here carry
``verificationMode: harness``: the worker still proves the patch replays onto the pinned
commit, and the harness owns the verdict.
"""

import json
import re

HARNESS = "swebench"
DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def changed_files(patch):
    """Paths a unified diff touches."""
    return sorted({name for _, name in DIFF_HEADER.findall(patch or "")})


def failing_tests(instance):
    """FAIL_TO_PASS as a list: the dataset ships lists, older exports ship JSON strings."""
    value = instance.get("FAIL_TO_PASS") or []
    if isinstance(value, str):
        value = json.loads(value)
    return [str(test) for test in value]


def default_test_command(instance):
    """A development command for the coder: the instance's own failing tests, when known."""
    tests = failing_tests(instance)
    files = sorted({test.split("::")[0] for test in tests if test.split("::")[0].endswith(".py")})
    return "python -m pytest -q " + " ".join(files[:5]) if files else "python -m pytest -q"


# SWE-bench images keep the pinned interpreter in a conda env that the official evaluation
# scripts activate; without that activation `python` is the bare base env, which neither has
# the dependencies nor a Python new enough for the checkout.
CONDA_PREFIX = "bash -lc 'source /opt/miniconda3/bin/activate && conda activate testbed && {command}'"


def image_test_command(instance, limit=3):
    """The development command to use inside an instance image: its own failing tests."""
    tests = [test for test in failing_tests(instance) if " " not in test][:limit]
    command = "python -m pytest -q " + " ".join(tests) if tests else "python -m pytest -q"
    return CONDA_PREFIX.format(command=command)


def task_from_instance(instance, allowed_paths=None, subdir="", context_mode="full", review_policy="auto",
                       workspace_mode="image"):
    """A repository task for one instance, judged by the official harness.

    The default workspace mode is ``image``: an instance image already carries its checkout and
    its pinned environment, and RepoPilot's bounded snapshot (200 text files, 100 KB each) does
    not fit a real repository. ``allowedPaths`` defaults to the files the instance's own patch
    touches — a wider hint than a task statement normally gives, so runs translated this way are
    not comparable with leaderboard numbers; pass an explicit list to narrow it.
    """
    paths = allowed_paths or changed_files(instance.get("patch", ""))
    if not paths:
        raise ValueError("Instance has no patch to bound the allowed paths; pass allowed_paths")
    if not instance.get("base_commit") or not instance.get("problem_statement"):
        raise ValueError("Instance needs base_commit and problem_statement")
    if workspace_mode == "image" and not instance.get("image"):
        raise ValueError("An image workspace needs the instance's own image")
    spec = {
        "source": f"https://github.com/{instance['repo']}",
        "commit": instance["base_commit"],
        "subdir": subdir,
        "allowedPaths": paths,
        "verificationMode": "harness",
        "instanceId": instance["instance_id"],
        "testCommand": image_test_command(instance) if workspace_mode == "image" else default_test_command(instance),
        "workspaceMode": workspace_mode,
    }
    if workspace_mode == "image":
        spec["sandboxImage"] = instance["image"]
        spec["workspacePath"] = "/testbed"
    return {
        "mode": "live",
        "task": instance["problem_statement"],
        "contextMode": context_mode,
        "memoryEnabled": False,
        "reviewPolicy": review_policy,
        "spec": spec,
    }


def prediction(run, model_name):
    """One line of the official predictions file, as the harness reads it."""
    return {"instance_id": (run.get("spec") or {}).get("instanceId"),
            "model_name_or_path": model_name,
            "model_patch": (run.get("result") or {}).get("patch") or ""}


def export_predictions(runs, model_name):
    """One prediction per instance run, including the runs that produced nothing.

    An attempt that crashed, ran out of steps or never submitted has no patch: SWE-bench counts
    that as an empty submission, not as a missing entry, so the denominator stays honest and the
    harness decides. Runs that are not instance runs at all are simply not part of the sample.
    """
    rows, skipped = [], []
    for run in runs:
        instance = (run.get("spec") or {}).get("instanceId")
        if not instance:
            continue
        patch = (run.get("result") or {}).get("patch") or ""
        if not patch:
            reason = ("no patch was produced" if run.get("status") == "SUCCEEDED"
                      else f"status {run.get('status')}")
            skipped.append({"run_id": run["id"], "instance_id": instance, "reason": reason})
        rows.append(prediction(run, model_name))
    seen = {}
    for row in rows:
        # One prediction per instance: the newest attempt that produced a patch wins, and an
        # empty submission is only used when no attempt produced anything.
        current = seen.get(row["instance_id"])
        if current is None or (not current["model_patch"] and row["model_patch"]):
            seen[row["instance_id"]] = row
    return list(seen.values()), skipped


def summarise_report(report):
    """Counters from an official report, plus the ids behind them.

    The harness writes two shapes: a summary per run (``total_instances``/``resolved_ids``) and,
    in older versions, one entry per instance with a ``resolved`` flag. Both are accepted so a
    report can be imported whichever version produced it.
    """
    if "resolved_ids" in report or "total_instances" in report:
        resolved = list(report.get("resolved_ids") or [])
        unresolved = list(report.get("unresolved_ids") or [])
        return {"instances": report.get("total_instances", len(resolved) + len(unresolved)),
                "resolved": len(resolved),
                "patch_applied": report.get("completed_instances"),
                "resolved_ids": sorted(resolved), "unresolved_ids": sorted(unresolved),
                "empty_patch_ids": sorted(report.get("empty_patch_ids") or []),
                "error_ids": sorted(report.get("error_ids") or []),
                "failure_reasons": report.get("failure_reasons") or {}, "harness": HARNESS}
    resolved = sorted(key for key, entry in report.items() if (entry or {}).get("resolved"))
    applied = sorted(key for key, entry in report.items() if (entry or {}).get("patch_successfully_applied"))
    return {"instances": len(report), "resolved": len(resolved), "patch_applied": len(applied),
            "resolved_ids": resolved, "unresolved_ids": sorted(set(report) - set(resolved)),
            "harness": HARNESS}
