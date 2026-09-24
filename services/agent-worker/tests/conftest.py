"""Unit tests must never reach a paid model provider, regardless of the host .env."""

import pytest


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    import litellm

    def blocked(**_):
        pytest.fail("Unit test attempted a real model provider request")

    async def blocked_async(**_):
        pytest.fail("Unit test attempted a real model provider request")

    monkeypatch.setattr(litellm, "completion", blocked)
    monkeypatch.setattr(litellm, "acompletion", blocked_async)
