"""Account-specific free-model gate, checked before every live request."""

FREE_MODELS = frozenset({
    "qwen3.8-27b", "qwen3.7-flash-2026-07-15", "qwen3.8-flash", "kimi-k3",
    "qwen3.8-max-0902", "glm-5.3", "deepseek-v4-pro-0813", "qwen3.8-2.4t-a95b", "qwen3.8-max",
})


def require_free_model(name):
    if name.removeprefix("openai/") not in FREE_MODELS:
        raise ValueError("Model is outside the authorized free quotas; paid fallback is disabled")
    return name
