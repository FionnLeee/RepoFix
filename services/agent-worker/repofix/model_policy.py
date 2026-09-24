"""Explicit model routes, checked before every request; no automatic fallback."""

import os

FREE_MODELS = frozenset({
    "qwen3.8-27b", "qwen3.7-flash-2026-07-15", "qwen3.8-flash", "kimi-k3",
    "qwen3.8-max-0902", "glm-5.3", "deepseek-v4-pro-0813", "qwen3.8-2.4t-a95b", "qwen3.8-max",
})


def require_free_model(name):
    if name.removeprefix("openai/") not in FREE_MODELS:
        raise ValueError("Model is outside the authorized free quotas; paid fallback is disabled")
    return name


def require_authorized_model(name, api_base, policy=None):
    policy = policy if policy is not None else os.getenv("MODEL_POLICY", "free-quota")
    if policy == "official-deepseek":
        if name.removeprefix("openai/") != "deepseek-flash" or api_base not in (
                "https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1"):
            raise ValueError("Official DeepSeek route requires deepseek-flash at https://api.deepseek.com")
        return name
    if policy != "free-quota":
        raise ValueError("Unknown model policy")
    return require_free_model(name)


def request_options(name, api_base):
    require_authorized_model(name, api_base)
    if os.getenv("MODEL_POLICY", "free-quota") == "official-deepseek":
        # The existing 1600-token output budget is for the final action/review, not a reasoning trace.
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}
