from types import SimpleNamespace

from repopilot import review

RUN = {"id": "run-1", "mode": "demo", "task": "修复金额与运费的口径", "reviewPolicy": "auto"}
SPEC = SimpleNamespace(allowedPaths=["money.py"], testCommand="python -m pytest -q")
SOURCE = {"money.py": "def total(p, q):\n    return p * q\n"}
CANDIDATE = {"money.py": "def total(p, q):\n    return round(p * q, 2)\n"}


def answer(summary="", findings=None):
    import json
    return "```json\n" + json.dumps({"summary": summary, "findings": findings or []}, ensure_ascii=False) + "\n```"


def test_findings_must_name_a_real_position_in_the_candidate():
    parsed = review.parse(answer("ok", [
        {"file": "money.py", "line": 2, "severity": "blocking", "finding": "口径不对"},
        {"file": "missing.py", "line": 1, "severity": "blocking", "finding": "不在候选里"},
        {"file": "money.py", "line": 99, "severity": "blocking", "finding": "行号越界"},
        {"file": "money.py", "line": 0, "severity": "blocking", "finding": "行号为零"},
        {"file": "money.py", "line": 2.5, "severity": "blocking", "finding": "行号不是整数"},
        {"file": "money.py", "line": 2, "severity": "catastrophic", "finding": "等级不在词表"},
        {"file": "money.py", "line": 2, "severity": "major", "finding": "   "},
        "not an object"]), CANDIDATE)
    assert parsed["parse_error"] is None
    assert [finding["file"] for finding in parsed["findings"]] == ["money.py"]
    assert parsed["findings"][0]["severity"] == "blocking"
    assert len(parsed["invalid"]) == 7
    assert all(entry["reason"] for entry in parsed["invalid"])


def test_an_unusable_answer_is_a_failure_not_a_clean_candidate():
    assert review.parse("no json here", CANDIDATE)["parse_error"]
    assert review.parse("```json\n{oops}\n```", CANDIDATE)["parse_error"]
    assert review.parse("```json\n[1, 2]\n```", CANDIDATE)["parse_error"]
    assert review.parse(answer("ok", None).replace('"findings": []', '"findings": "none"'),
                        CANDIDATE)["parse_error"]


def test_an_empty_review_is_a_valid_answer():
    parsed = review.parse(answer("这个补丁没有问题"), CANDIDATE)
    assert parsed == {"summary": "这个补丁没有问题", "findings": [], "invalid": [], "parse_error": None}
    assert review.blocking(parsed["findings"]) == []


def test_findings_are_capped_and_the_overflow_is_reported():
    many = [{"file": "money.py", "line": 2, "severity": "minor", "finding": f"问题 {index}"}
            for index in range(review.MAX_FINDINGS + 5)]
    parsed = review.parse(answer("多", many), CANDIDATE)
    assert len(parsed["findings"]) == review.MAX_FINDINGS
    assert len(parsed["invalid"]) == 5


def test_the_reviewer_gets_its_own_context_and_the_evidence():
    messages = review.request(RUN, SPEC, CANDIDATE, ["money.py"], "- return p * q\n+ return round(p * q, 2)",
                              "1 passed in 0.02s")
    assert [message["role"] for message in messages] == ["system", "user"]
    body = messages[1]["content"]
    assert RUN["task"] in body and "money.py" in body
    assert "1 | def total" in body and "+ return round(p * q, 2)" in body
    assert "1 passed in 0.02s" in body
    assert "CODER_INTERNAL_REASONING" not in body


def test_review_policy_and_rounds_come_from_the_run():
    assert review.policy(RUN) == "auto" and review.enabled(RUN) is True
    assert review.enabled({**RUN, "reviewPolicy": "off"}) is False
    # The platform can turn the review on for a run that was created without it.
    assert review.enabled({**RUN, "reviewPolicy": "auto"}) is True
    assert review.max_rounds(RUN) == review.DEFAULT_MAX_ROUNDS
    assert review.max_rounds({**RUN, "reviewRounds": 0}) == 0
    assert review.max_rounds({**RUN, "reviewRounds": 9}) == review.DEFAULT_MAX_ROUNDS
    assert review.max_rounds({**RUN, "reviewRounds": "2"}) == review.DEFAULT_MAX_ROUNDS


def test_the_demo_stub_is_labelled_and_reports_only_once():
    first = review.StubReviewer(SOURCE, CANDIDATE, ["money.py"], 0).complete([])
    parsed = review.parse(first["content"], CANDIDATE)
    assert parsed["parse_error"] is None and len(parsed["findings"]) == 1
    finding = parsed["findings"][0]
    assert finding["file"] == "money.py" and finding["line"] == 2 and finding["severity"] == "blocking"
    assert "演示" in finding["finding"] and first["reviewer"] == "deterministic-stub"
    later = review.parse(review.StubReviewer(SOURCE, CANDIDATE, ["money.py"], 2).complete([])["content"], CANDIDATE)
    assert later["findings"] == []


def test_feedback_keeps_position_severity_and_suggestion():
    findings = [{"file": "money.py", "line": 2, "severity": "blocking", "finding": "没有保留两位小数",
                 "trigger": "total(1, 0.005)", "evidence": "返回 0.005", "suggestion": "用 round(..., 2)"}]
    text = review.render_for_coder(findings)
    for expected in ("money.py:2", "blocking", "没有保留两位小数", "total(1, 0.005)", "用 round(..., 2)"):
        assert expected in text
