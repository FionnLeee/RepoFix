"""Independent review of a candidate patch.

The reviewer is a second role, not a second coder. It is given the pinned task, the allowed
paths, the candidate diff, the changed sources and a fresh run of the development test
command. It never sees the coder's message history, it has no tools, no sandbox and no way
to write anything: its findings go back to the coder as an observation, and the independent
acceptance run still decides whether the candidate is accepted.

A review that cannot be parsed is reported as a failure rather than as a clean candidate, and
findings that do not name a real position in the candidate are dropped and counted. An empty
review is a normal answer.
"""

import json
import os
import re

from repofix.model_policy import request_options

SEVERITIES = ("blocking", "major", "minor")
BLOCKING_SEVERITIES = ("blocking",)
DEFAULT_MAX_ROUNDS = 2
MAX_FINDINGS = 20
MAX_FIELD = 600
MAX_PATCH = 6000
MAX_FILE = 6000
MAX_USER = 24000
BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

SYSTEM = """You review one candidate patch for a Python repository.
You cannot run commands, change files or see the previous conversation; you only report what
the diff, the candidate sources and the development test output you are given can justify.
Report problems that can actually be triggered: a wrong result, a crash, a regression, a
requirement of the task that the patch does not meet, or a claim the tests no longer cover.
Do not restate the diff and do not invent problems to look thorough.
Answer with one fenced json block and nothing else:
```json
{"summary": "one sentence", "findings": [{"file": "money.py", "line": 3, "severity": "blocking",
 "finding": "what is wrong", "trigger": "the call or input that shows it",
 "evidence": "the line or test result that supports it", "suggestion": "the smallest fix"}]}
```
`file` must be one of the supplied files and `line` an existing line in it. For a deleted
file use `"side": "base"` and a line in its supplied base source; otherwise use candidate.
`severity` is
blocking (wrong result, crash, regression, unmet requirement), major (fragile or uncovered),
or minor (naming, style). Use `"findings": []` when nothing is worth reporting: an empty
review is a valid and common answer.
When the request lists findings from your previous round together with the coder's response,
also return `"previous": [{"id": 1, "status": "fixed", "note": "why"}]` with one entry per
listed finding: `fixed` (the candidate now handles it), `rejected_ok` (the coder's evidence
shows it was not a real problem), or `open` (still present; then also report it again in
`findings` with its current position)."""

PREVIOUS_STATUSES = ("fixed", "rejected_ok", "open")
RESPONSE = re.compile(r"REVIEW-RESPONSE\s+(\d+)\s*:\s*(fixed|rejected)\b\s*[-—:]?\s*(.*)", re.IGNORECASE)


def policy(run):
    """Review is on for a run unless the run asked for it to be off.

    The platform owns this switch (it is set when the run is created and can still be turned
    on while the run is queued), so a review happens on the platform's terms as well as the
    coder's; it is not something the coder can decide on its own.
    """
    return run.get("reviewPolicy", "auto")


def enabled(run):
    return policy(run) != "off"


def max_rounds(run):
    value = run.get("reviewRounds", DEFAULT_MAX_ROUNDS)
    return value if type(value) is int and 0 <= value <= 5 else DEFAULT_MAX_ROUNDS


def _numbered(name, text, limit=MAX_FILE, patch=""):
    lines = text.split("\n")
    selected = set()
    section = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            section = line.endswith(f" b/{name}")
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line) if section else None
        if match:
            start, count = int(match[1]), int(match[2] or 1)
            selected.update(range(max(1, start - 5), min(len(lines), start + count + 5) + 1))
    indices = sorted(selected) if selected else range(1, len(lines) + 1)
    rendered = [f"{index:>4} | {lines[index - 1]}" for index in indices]
    body = "\n".join(rendered)
    return body[:limit] + ("\n[... truncated ...]" if len(body) > limit or len(rendered) < len(lines) else "")


def request(run, spec, candidate, changed, patch, evidence, base=None, with_coverage=False, previous=None):
    """The reviewer's own message list: nothing of the coder's history is carried over.

    ``previous`` carries the blocking findings of the last round with the coder's stated
    response, so the reviewer judges each one instead of the coder grading its own revision.
    """
    parts = [f"Task: {run['task']}", f"Allowed paths: {', '.join(spec.allowedPaths)}",
             f"Development test command: {spec.testCommand}"]
    # Keep test evidence before optional source excerpts so the request budget cannot erase it.
    parts.append("Development test output on this candidate:\n```\n" + evidence[:3000] + "\n```")
    if previous:
        lines = ["Your previous round reported these findings; the coder revised and responded:"]
        for item in previous:
            response = item.get("coder") or {}
            lines.append(f"{item['id']}. [{item['severity']}] {item['file']}:{item['line']} {item['finding']}"
                         f"\n   coder response: {response.get('status', 'unstated')}"
                         + (f" - {response['note']}" if response.get("note") else ""))
        parts.append("\n".join(lines)[:MAX_PATCH])
    parts.append("Candidate diff:\n```diff\n" + patch[:MAX_PATCH] + "\n```")
    for name in sorted(changed):
        if name not in candidate:
            if name in (base or {}):
                parts.append(f"Deleted file {name}, base side:\n```python\n{_numbered(name, base[name])}\n```")
            else:
                parts.append(f"File {name}: deleted or omitted from source preview; inspect its diff.")
            continue
        parts.append(f"Candidate file {name}:\n```python\n{_numbered(name, candidate[name], patch=patch)}\n```")
    full_body = "\n\n".join(parts)
    partial = (len(patch) > MAX_PATCH or len(evidence) > 3000 or len(full_body) > MAX_USER or
               "[... truncated ...]" in full_body or any(name not in candidate and name not in (base or {})
                                                        for name in changed))
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": full_body[:MAX_USER]}]
    return (messages, partial) if with_coverage else messages


def _text(value, limit=MAX_FIELD):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _position_error(item, candidate, base=None):
    """Why a finding does not name a real position in the candidate, or None when it does."""
    name = item.get("file")
    side = item.get("side", "candidate")
    if side not in ("candidate", "base"):
        return "side must be candidate or base"
    if side == "base":
        candidate = base or {}
    if not isinstance(name, str) or name not in candidate:
        return f"file {name!r} is not a candidate file"
    line = item.get("line")
    if type(line) is not int or line < 1 or line > len(candidate[name].split("\n")):
        return f"line {line!r} is not in {name}"
    return None


def parse(text, candidate, base=None):
    """Validate a review answer; unusable findings are dropped and counted, never trusted."""
    result = {"summary": "", "findings": [], "invalid": [], "parse_error": None, "previous": []}
    match = BLOCK.search(text or "") or re.search(r"(\{.*\})", text or "", re.DOTALL)
    if not match:
        result["parse_error"] = "no json object in the answer"
        return result
    try:
        payload = json.loads(match.group(1))
    except ValueError as error:
        result["parse_error"] = f"invalid json: {error}"[:200]
        return result
    if not isinstance(payload, dict):
        result["parse_error"] = "the answer is not a json object"
        return result
    result["summary"] = _text(payload.get("summary"))
    listed = payload.get("previous") if isinstance(payload.get("previous"), list) else []
    for item in listed:
        if isinstance(item, dict) and type(item.get("id")) is int and item.get("status") in PREVIOUS_STATUSES:
            result["previous"].append({"id": item["id"], "status": item["status"], "note": _text(item.get("note"))})
    raw = payload.get("findings")
    if not isinstance(raw, list):
        result["parse_error"] = "findings is not a list"
        return result
    for item in raw:
        if not isinstance(item, dict):
            result["invalid"].append({"reason": "finding is not an object", "raw": str(item)[:120]})
            continue
        name, error = item.get("file"), _position_error(item, candidate, base)
        severity = item.get("severity")
        if error is None and severity not in SEVERITIES:
            error = f"severity {severity!r} is not one of {', '.join(SEVERITIES)}"
        if error is None and not _text(item.get("finding")):
            error = "finding text is empty"
        if error:
            result["invalid"].append({"reason": error, "raw": json.dumps(item, ensure_ascii=False)[:200]})
            continue
        if len(result["findings"]) >= MAX_FINDINGS:
            result["invalid"].append({"reason": f"more than {MAX_FINDINGS} findings", "raw": name})
            continue
        result["findings"].append({"file": name, "line": item["line"], "side": item.get("side", "candidate"),
                                   "severity": severity,
                                   "finding": _text(item.get("finding")), "trigger": _text(item.get("trigger")),
                                   "evidence": _text(item.get("evidence")), "suggestion": _text(item.get("suggestion"))})
    return result


def blocking(findings):
    return [finding for finding in findings if finding["severity"] in BLOCKING_SEVERITIES]


class ModelReviewer:
    """One model call, no tools, read-only; a separate role from the coder."""

    def __init__(self, name, api_base, api_key, max_tokens=1600, temperature=0.0, timeout=45):
        self.name, self.api_base, self.api_key = name, api_base, api_key
        self.max_tokens, self.temperature, self.timeout = max_tokens, temperature, timeout

    def complete(self, messages):
        import litellm

        options = request_options(self.name, self.api_base)
        response = litellm.completion(model=f"openai/{self.name}", messages=messages, api_base=self.api_base,
                                      api_key=self.api_key, timeout=self.timeout, num_retries=0,
                                      max_tokens=self.max_tokens, temperature=self.temperature, **options)
        message = response.choices[0].message
        try:
            cost = float(litellm.completion_cost(response, model=f"openai/{self.name}") or 0.)
        except Exception:
            cost = 0.
        return {"content": message.content or "", "cost": cost, "usage": response.usage.model_dump()
                if getattr(response, "usage", None) else None, "reviewer": self.name}


class StubReviewer:
    """Deterministic stand-in used by demo runs, so the loop can be verified without credits.

    The first round reports one finding that is labelled as a stub on purpose: it drives a
    single bounded revision and must not be read as a real review conclusion. Every later
    round reports nothing. Live runs use ``ModelReviewer``.
    """

    name = "deterministic-stub"

    def __init__(self, source, candidate, changed, round_index=0):
        self.source, self.candidate, self.changed, self.round_index = source, candidate, changed, round_index

    def complete(self, messages):
        summary, findings = "演示桩：未发现问题。", []
        name = sorted(self.changed)[0] if self.changed else None
        previous = []
        if self.round_index == 0 and name:
            line = _first_changed_line(self.source.get(name, ""), self.candidate.get(name, ""))
            findings = [{"file": name, "line": line, "severity": "blocking",
                         "finding": "演示用确定性评审桩：该条只用于验证「评审→有限修订→复评」闭环，不是真实评审结论。",
                         "trigger": "该桩在首轮固定报出一条阻断项",
                         "evidence": f"{name} 第 {line} 行与固定基准版本不同",
                         "suggestion": "按反馈做一次有限修订后重新提交"}]
            summary = "演示桩：报告一条阻断项。"
        elif name and messages and "评审修订" in "\n".join(self.candidate.values()):
            # The demo coder adds this marker only in its revision. Confirm the earlier stub
            # finding explicitly, as a real reviewer must do for the disposition to close.
            body = str(messages[-1].get("content", ""))
            if "Your previous round reported these findings" in body:
                previous = [{"id": int(match), "status": "fixed", "note": "演示候选包含修订标记"}
                            for match in re.findall(r"(?m)^(\d+)\. \[", body)]
        return {"content": "```json\n" + json.dumps({"summary": summary, "findings": findings,
                                                     "previous": previous},
                                                    ensure_ascii=False) + "\n```", "cost": 0., "usage": None,
                "reviewer": self.name}


def _first_changed_line(before, after):
    old, new = before.split("\n"), after.split("\n")
    for index, (left, right) in enumerate(zip(old, new), start=1):
        if left != right:
            return index
    return min(len(old), len(new)) + 1 if old != new else 1


def reviewer_for(run, source, candidate, changed, round_index=0):
    """The reviewer a run gets: a deterministic stub for demo runs, the model otherwise."""
    if run["mode"] == "demo":
        return StubReviewer(source, candidate, changed, round_index)
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("MODEL_NAME"):
        raise ValueError("Live model configuration missing")
    return ModelReviewer(os.environ["MODEL_NAME"], os.environ.get("MODEL_BASE_URL", ""),
                         os.environ["OPENAI_API_KEY"])


def render_for_coder(findings):
    """The feedback the coder receives: the reviewer's own words, numbered, plus how to answer them.

    A finding may be rejected, but only with evidence the reviewer can check; the answer is
    recorded per finding, so "fixed" and "argued away" never look the same afterwards.
    """
    lines = ["Review of your last submitted candidate found problems that must be handled.",
             "You are still in the same workspace; handle every numbered finding, then submit again.",
             "Fix a finding that is real. If a finding is wrong, do not change correct code to satisfy it:"
             " show concrete test or code evidence instead.",
             "In the message that contains your submit command, answer each finding on its own line as"
             " `REVIEW-RESPONSE <n>: fixed - <what changed>` or `REVIEW-RESPONSE <n>: rejected - <evidence>`."]
    for index, finding in enumerate(findings, start=1):
        lines.append(f"{index}. [{finding['severity']}] {finding['file']}:{finding['line']} {finding['finding']}"
                     + (f" Trigger: {finding['trigger']}" if finding["trigger"] else "")
                     + (f" Suggestion: {finding['suggestion']}" if finding["suggestion"] else ""))
    return "\n".join(lines)


def coder_responses(messages, count):
    """The coder's stated answer per finding id, read from its own messages after the feedback."""
    answers = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for match in RESPONSE.finditer(str(message.get("content", ""))):
            index = int(match.group(1))
            if 1 <= index <= count:
                answers[index] = {"status": match.group(2).lower(), "note": _text(match.group(3), 300)}
    return {index: answers.get(index, {"status": "unstated", "note": ""}) for index in range(1, count + 1)}


def dispositions(previous, reviewer_previous=None):
    """One record per earlier finding: what the coder said, what the reviewer verified, the outcome.

    Without a reviewer verdict (rounds exhausted, review failed) the coder's claim stays
    ``unverified``; it is never promoted to ``fixed`` on the coder's word alone.
    """
    listed = reviewer_previous or []
    verdicts = {item["id"]: item for item in listed if sum(other["id"] == item["id"] for other in listed) == 1}
    records = []
    for item in previous:
        verdict = verdicts.get(item["id"])
        coder = item.get("coder") or {"status": "unstated", "note": ""}
        if verdict is None:
            outcome, reviewer = "unverified", {"status": "unverified", "note": ""}
        else:
            reviewer = {"status": verdict["status"], "note": verdict.get("note", "")}
            outcome = {"fixed": "fixed", "rejected_ok": "rejected_with_evidence", "open": "unresolved"}[verdict["status"]]
            if outcome == "rejected_with_evidence" and (
                    coder["status"] != "rejected" or not coder.get("note") or not reviewer["note"]):
                outcome = "unverified"
        records.append({"id": item["id"], "review_round": item["review_round"], "file": item["file"],
                        "line": item["line"], "side": item.get("side", "candidate"),
                        "severity": item["severity"], "finding": item["finding"],
                        "coder": coder, "reviewer": reviewer, "outcome": outcome})
    return records


def perform(reviewer, run, spec, candidate, changed, patch, evidence, round_index, base=None, previous=None):
    """Ask for one review and return it with the position validation already applied."""
    messages, partial = request(run, spec, candidate, changed, patch, evidence, base, with_coverage=True,
                                previous=previous)
    answer = reviewer.complete(messages)
    parsed = parse(answer["content"], candidate, base)
    # Coverage describes evidence supplied, not a claim that the reviewer understood every line.
    expected = {item["id"] for item in previous or []}
    supplied = [item["id"] for item in parsed["previous"]]
    parsed["partial"] = partial or bool(expected and (set(supplied) != expected or len(supplied) != len(expected)))
    parsed.update(round=round_index, reviewer=answer.get("reviewer"), cost=answer.get("cost") or 0.,
                  usage=answer.get("usage"), content=answer["content"],
                  blocking_count=len(blocking(parsed["findings"])), invalid_count=len(parsed["invalid"]))
    return parsed
