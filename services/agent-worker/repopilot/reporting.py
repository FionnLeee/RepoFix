"""Account for every recorded response, including rejected model output."""

import json
import os
from pathlib import Path
from uuid import UUID


def usage_summary(messages, model_calls, mode):
    totals = {"input_tokens": 0, "output_tokens": 0}
    covered = 0
    for message in messages:
        response = message.get("extra", {}).get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            continue
        values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        if not all(type(value) is int and value >= 0 for value in values):
            continue
        covered += 1
        totals["input_tokens"] += values[0]
        totals["output_tokens"] += values[1]
    if mode == "demo":
        totals = {"input_tokens": 0, "output_tokens": 0}
        covered = model_calls
    complete = model_calls is not None and covered == model_calls
    return {
        "model_calls": model_calls,
        "usage": totals if covered or complete else None,
        "usage_calls_reported": covered,
        "usage_status": "complete" if complete else "partial" if covered else "unavailable",
    }


def trajectory_summary(run_id, mode, root=None):
    run_id = str(UUID(run_id))
    folder = Path(root or os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / run_id
    data = json.loads((folder / "trajectory.json").read_text(encoding="utf-8"))
    info = data.get("info", {})
    calls = info.get("model_stats", {}).get("api_calls")
    return {
        **usage_summary(data.get("full_messages", data.get("messages", [])), calls, mode),
        "context_compactions": data.get("context_compactions", []),
        "stop_reason": info.get("exit_status"),
        "artifact_path": f"{run_id}/",
    }


def failure_result(run, error):
    text = str(error)
    if key := os.getenv("OPENAI_API_KEY"):
        text = text.replace(key, "[REDACTED]")
    result = {"error": text[:1500], "mode": run["mode"], "cost_usd": None}
    try:
        result.update(trajectory_summary(run["id"], run["mode"]))
    except (OSError, ValueError, TypeError, AttributeError):
        result.update(usage=None, usage_status="unavailable", model_calls=None)
    folder = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / str(UUID(run["id"]))
    try:
        result["review"] = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        result["artifact_write_failed"] = True
    return result
