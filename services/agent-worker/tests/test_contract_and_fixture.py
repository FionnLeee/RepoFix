import pytest
from pydantic import ValidationError
from repofix.fixture import FIXED, SOURCE
from repofix.worker import Message


def test_cross_language_contract_rejects_unknown_version_and_fields():
    valid = {"schema_version": 1, "message_id": "42", "run_id": "2d576e76-472e-4d78-93aa-21c7cac3c013"}
    assert str(Message.model_validate(valid).run_id) == valid["run_id"]
    for changed in ({**valid, "schema_version": 2}, {**valid, "approved": True}, {**valid, "run_id": "../../etc"}):
        with pytest.raises(ValidationError):
            Message.model_validate(changed)


def test_fixture_exposes_bug_and_corrected_source_handles_boundaries():
    broken, fixed = {}, {}
    exec(compile(SOURCE, "fixture", "exec"), broken)
    exec(compile(FIXED, "fixture", "exec"), fixed)
    assert broken["discounted_total"](100, 2, 20) == 180
    assert fixed["discounted_total"](100, 2, 20) == 160
    assert fixed["discounted_total"](19.99, 3, 15) == 50.97
    for discount in (-1, 101):
        with pytest.raises(ValueError):
            fixed["discounted_total"](10, 1, discount)
