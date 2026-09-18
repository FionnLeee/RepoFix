import json

import pytest
from repopilot import swebench
from repopilot.repository import RepositoryTask

INSTANCE = {
    "instance_id": "django__django-11099",
    "repo": "django/django",
    "base_commit": "b" * 40,
    "problem_statement": "Fix the field conversion",
    "patch": ("diff --git a/django/forms/fields.py b/django/forms/fields.py\n--- a/django/forms/fields.py\n"
              "+++ b/django/forms/fields.py\n@@ -1 +1 @@\n-x\n+y\n"
              "diff --git a/django/forms/fields.py b/django/forms/fields.py\n--- a/django/forms/fields.py\n"
              "+++ b/django/forms/fields.py\n@@ -9 +9 @@\n-x\n+y\n"),
    "test_patch": "diff --git a/tests/forms_tests/tests.py b/tests/forms_tests/tests.py\n",
    "FAIL_TO_PASS": '["tests/forms_tests/tests.py::FormsTestCase::test_a"]',
    "PASS_TO_PASS": "[]",
}


def run(run_id, status="SUCCEEDED", patch="diff --git a/x.py b/x.py\n", instance="django__django-11099"):
    return {"id": run_id, "status": status, "spec": {"instanceId": instance}, "result": {"patch": patch}}


def test_the_changed_files_of_an_instance_patch_bound_the_task():
    assert swebench.changed_files(INSTANCE["patch"]) == ["django/forms/fields.py"]
    task = swebench.task_from_instance(INSTANCE)
    assert task["mode"] == "live" and task["task"] == INSTANCE["problem_statement"]
    assert task["reviewPolicy"] == "auto"
    spec = task["spec"]
    assert spec["source"] == "https://github.com/django/django"
    assert spec["commit"] == INSTANCE["base_commit"] and spec["allowedPaths"] == ["django/forms/fields.py"]
    assert spec["verificationMode"] == "harness" and spec["instanceId"] == INSTANCE["instance_id"]
    # The translated spec is one this worker accepts: harness mode carries no local tests.
    assert RepositoryTask.model_validate(spec).verificationMode == "harness"
    assert RepositoryTask.model_validate(spec).verificationFiles == {}
    with pytest.raises(ValueError):
        swebench.task_from_instance({**INSTANCE, "patch": ""})


def test_the_development_command_uses_the_instances_own_failing_tests():
    assert swebench.default_test_command(INSTANCE) == "python -m pytest -q tests/forms_tests/tests.py"
    assert swebench.default_test_command({**INSTANCE, "FAIL_TO_PASS": "[]"}) == "python -m pytest -q"


def test_predictions_are_written_for_instance_runs_and_everything_else_is_reported():
    runs = [run("1"), run("2", status="FAILED"), run("3", patch=""),
            run("4", instance=None), run("5", patch="diff --git a/y.py b/y.py\n")]
    rows, skipped = swebench.export_predictions(runs, "repopilot")
    assert rows == [{"instance_id": "django__django-11099", "model_name_or_path": "repopilot",
                     "model_patch": "diff --git a/y.py b/y.py\n"}]  # one row per instance, last wins
    assert {entry["run_id"]: entry["reason"] for entry in skipped} == {
        "2": "status FAILED", "3": "no patch was produced"}
    assert json.dumps(rows)  # the predictions file is a plain jsonl payload


def test_the_harness_report_is_summarised_with_the_ids_behind_each_number():
    report = {
        "django__django-11099": {"resolved": True, "patch_successfully_applied": True},
        "django__django-11133": {"resolved": False, "patch_successfully_applied": True},
        "django__django-11433": {"resolved": False, "patch_successfully_applied": False},
    }
    summary = swebench.summarise_report(report)
    assert summary["instances"] == 3 and summary["resolved"] == 1 and summary["patch_applied"] == 2
    assert summary["resolved_ids"] == ["django__django-11099"]
    assert summary["unresolved_ids"] == ["django__django-11133", "django__django-11433"]


def test_the_summary_shape_the_current_harness_writes_is_understood_too():
    """swebench 5.x writes one summary per run; the ids are lists, not per-instance entries."""
    report = {"total_instances": 2, "completed_instances": 1, "resolved_instances": 1,
              "unresolved_instances": 0, "empty_patch_instances": 1, "error_instances": 0,
              "resolved_ids": ["django__django-11099"], "unresolved_ids": [],
              "empty_patch_ids": ["astropy__astropy-12907"], "error_ids": [],
              "failure_reasons": {}, "schema_version": 2}
    summary = swebench.summarise_report(report)
    assert summary["instances"] == 2 and summary["resolved"] == 1
    assert summary["resolved_ids"] == ["django__django-11099"]
    assert summary["empty_patch_ids"] == ["astropy__astropy-12907"]
    assert summary["error_ids"] == []
