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
    "image": "swebench/sweb.eval.x86_64.django_1776_django-11099:latest",
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
    assert spec["commit"] == INSTANCE["base_commit"] and spec["allowedPaths"] == []
    assert spec["verificationMode"] == "harness" and spec["instanceId"] == INSTANCE["instance_id"]
    # The default workspace is the instance's own image: a real repository does not fit the
    # bounded snapshot, and the harness owns the verdict.
    assert spec["workspaceMode"] == "image" and spec["workspacePath"] == "/testbed"
    assert spec["sandboxImage"] == INSTANCE["image"]
    translated = RepositoryTask.model_validate(spec)
    assert translated.verificationMode == "harness" and translated.verificationFiles == {}
    assert task["contextMode"] == "full"  # no bounded snapshot to index or remember
    assert swebench.task_from_instance({**INSTANCE, "patch": ""}) == task
    with pytest.raises(ValueError):
        swebench.task_from_instance({**INSTANCE, "image": None})
    # A snapshot workspace is still available for small, self-contained repositories.
    snapshot = swebench.task_from_instance(INSTANCE, workspace_mode="snapshot", allowed_paths=["django/forms/fields.py"])
    assert snapshot["spec"]["workspaceMode"] == "snapshot" and "sandboxImage" not in snapshot["spec"]
    assert RepositoryTask.model_validate(snapshot["spec"]).workspaceMode == "snapshot"


def test_an_image_task_runs_its_tests_in_the_instances_conda_environment():
    """Without the activation the bare base env has neither the deps nor a new enough Python."""
    assert swebench.image_test_command(INSTANCE).startswith("bash -lc 'source /opt/miniconda3/bin/activate")
    assert "conda activate testbed" in swebench.image_test_command(INSTANCE)
    assert "FormsTestCase" not in swebench.image_test_command(INSTANCE)
    assert "python -m pytest -q'" in swebench.image_test_command({**INSTANCE, "FAIL_TO_PASS": []})


def test_the_development_command_uses_the_instances_own_failing_tests():
    assert swebench.default_test_command(INSTANCE) == "python -m pytest -q"
    assert swebench.default_test_command({**INSTANCE, "FAIL_TO_PASS": "[]"}) == "python -m pytest -q"
    # The dataset ships FAIL_TO_PASS as a list; older exports ship it as a JSON string.
    listed = {**INSTANCE, "FAIL_TO_PASS": ["tests/test_requests.py::TestRequests::test_x"]}
    assert swebench.default_test_command(listed) == "python -m pytest -q"
    assert swebench.failing_tests(listed) == ["tests/test_requests.py::TestRequests::test_x"]
    assert swebench.failing_tests({**INSTANCE, "FAIL_TO_PASS": "[]"}) == []


def test_predictions_cover_every_instance_run_even_without_a_patch():
    # /runs answers newest first, so run "1" is the newest attempt for this instance.
    runs = [run("1"), run("2", status="FAILED", patch="", instance="empty-2"),
            run("3", patch="", instance="empty-3"), run("4", instance=None)]
    rows, skipped = swebench.export_predictions(runs, "repopilot")
    # An attempt with nothing to show is an empty submission the harness judges, not a hole in
    # the sample; a run that is not an instance run is not part of the sample at all.
    assert [row["instance_id"] for row in rows] == ["django__django-11099", "empty-2", "empty-3"]
    assert rows[0]["model_patch"] == "diff --git a/x.py b/x.py\n"  # newest attempt that produced one
    assert {entry["run_id"]: entry["reason"] for entry in skipped} == {
        "2": "status FAILED", "3": "no patch was produced"}
    # A later run with a patch is not picked over an earlier one that already has a patch, and an
    # empty submission is only used when no attempt produced anything.
    older = [run("1"), run("5", patch="diff --git a/y.py b/y.py\n")]
    with pytest.raises(ValueError, match="Multiple runs"):
        swebench.export_predictions(older, "repopilot")
    empty_rows, empty_skipped = swebench.export_predictions([run("6", status="FAILED", patch="")], "repopilot")
    assert empty_rows[0]["model_patch"] == "" and empty_skipped[0]["reason"] == "status FAILED"
    assert json.dumps(rows)  # the predictions file is a plain jsonl payload


def test_batch_export_never_uses_history_or_paginated_listing():
    runs = {f"r{i}": run(f"r{i}", instance=f"case-{i}", status="FAILED", patch="") for i in range(45)}
    def fetch(path):
        assert path.startswith("/runs/")  # listing would expose unrelated historical success
        return runs[path.rsplit("/", 1)[1]]
    ids = {f"case-{i}": f"r{i}" for i in range(45)}
    rows, skipped, bindings = swebench.batch_predictions(ids, fetch)
    assert len(rows) == len(skipped) == len(bindings) == 45
    assert all(row["model_patch"] == "" for row in rows)
    runs["r0"]["status"] = "VERIFYING"
    with pytest.raises(ValueError, match="not terminal"):
        swebench.batch_predictions(ids, fetch)
    runs["r0"]["spec"]["instanceId"] = "another"
    with pytest.raises(ValueError, match="binding mismatch"):
        swebench.batch_predictions(ids, fetch)


def test_failed_attempt_config_hash_distinguishes_shared_and_extra_budget():
    attempt = {**run("current", status="FAILED", patch=""), "reviewBudget": "shared"}
    ids = {"django__django-11099": "current"}
    first = swebench.batch_predictions(ids, lambda _: attempt)[2][0]
    attempt["reviewBudget"] = "extra"
    second = swebench.batch_predictions(ids, lambda _: attempt)[2][0]
    assert first["patch_sha256"] == second["patch_sha256"]
    assert first["config_sha256"] != second["config_sha256"]


def test_agent_payload_does_not_depend_on_gold_or_hidden_test_metadata():
    altered = {**INSTANCE, "patch": "SECRET_GOLD", "test_patch": "SECRET_TEST",
               "FAIL_TO_PASS": ["SECRET_FAIL"], "PASS_TO_PASS": ["SECRET_PASS"]}
    assert swebench.task_from_instance(altered) == swebench.task_from_instance(INSTANCE)


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
