import time

import pytest
from repopilot.quota import ModelQuota, QuotaTimeout, QuotaUnavailable


class FakeRedis:
    """Mirrors the Lua contract: expire stale slots, then admit only below the limit."""

    def __init__(self):
        self.slots = {}
        self.closed = False

    def register_script(self, script):
        assert "ZREMRANGEBYSCORE" in script and "ZCARD" in script

        def call(keys, args):
            now, ttl, limit, token = float(args[0]), float(args[1]), int(args[2]), args[3]
            for member, expiry in list(self.slots.items()):
                if expiry <= now - ttl:
                    del self.slots[member]
            if len(self.slots) < limit:
                self.slots[token] = now + ttl
                return 1
            return 0

        return call

    def zrem(self, key, token):
        self.slots.pop(token, None)

    def close(self):
        self.closed = True


def test_slots_are_bounded_and_released_for_waiters():
    quota = ModelQuota(FakeRedis(), 1, poll=0.01)
    assert quota.acquire(timeout=1) >= 0
    with pytest.raises(QuotaTimeout):
        quota.acquire(timeout=0.05)
    quota.release()
    assert quota.acquire(timeout=1) >= 0
    assert len(quota.client.slots) == 1
    quota.release()
    assert quota.client.slots == {}


def test_crashed_holder_is_reclaimed_after_the_ttl():
    client = FakeRedis()
    quota = ModelQuota(client, 1, ttl=120, poll=0.01)
    client.slots["crashed-worker-token"] = time.time() - 300  # expired long ago
    assert quota.acquire(timeout=1) >= 0
    assert "crashed-worker-token" not in client.slots


def test_unavailable_redis_is_reported_not_hidden():
    class Broken(FakeRedis):
        def register_script(self, script):
            def call(keys, args):
                raise RuntimeError("connection refused")

            return call

    with pytest.raises(QuotaUnavailable):
        ModelQuota(Broken(), 2).acquire(timeout=1)


def test_configuration_requires_url_and_positive_limit():
    assert ModelQuota.from_env({}) is None
    assert ModelQuota.from_env({"REDIS_URL": "redis://redis:6379/0"}) is None
    assert ModelQuota.from_env({"REDIS_URL": "redis://redis:6379/0", "MODEL_MAX_CONCURRENCY": "0"}) is None
    assert ModelQuota.from_env({"REDIS_URL": "redis://redis:6379/0", "MODEL_MAX_CONCURRENCY": "nope"}) is None
    quota = ModelQuota.from_env({"REDIS_URL": "redis://redis:6379/0", "MODEL_MAX_CONCURRENCY": "2"})
    assert quota.limit == 2 and quota.key == "repopilot:model-slots"
