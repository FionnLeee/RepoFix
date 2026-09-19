"""Shared model-call concurrency quota on Redis.

Redis holds only short-lived coordination state here, never run truth: a slot is a
sorted-set member that expires, so a worker crash cannot permanently leak quota. When
Redis is not configured or unreachable the worker records an event and continues without
a shared quota instead of silently pretending one exists.
"""

import os
import time
import uuid

# Clean expired slots, then admit only below the limit. Races between workers are decided
# by the script's atomicity, not by a client-side check-then-set sequence.
TRY_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[2]), ARGV[4])
  return 1
end
return 0
"""


class QuotaUnavailable(RuntimeError):
    """Redis was not reachable; the caller decides whether to fail open and record it."""


class QuotaTimeout(RuntimeError):
    pass


class ModelQuota:
    def __init__(self, client, limit, key="repopilot:model-slots", ttl=120, poll=0.25):
        self.client, self.limit, self.key, self.ttl, self.poll = client, limit, key, ttl, poll
        self.token = None
        self._script = None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        url, limit = env.get("REDIS_URL"), env.get("MODEL_MAX_CONCURRENCY")
        if not url or not limit:
            return None
        try:
            limit = int(limit)
        except ValueError:
            return None
        if limit < 1:
            return None
        import redis

        client = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        return cls(client, limit)

    def script(self):
        if self._script is None:
            self._script = self.client.register_script(TRY_ACQUIRE)
        return self._script

    def acquire(self, timeout=120):
        """Take one slot, waiting up to ``timeout`` seconds; returns the waited seconds."""
        token, started, deadline = uuid.uuid4().hex, time.monotonic(), time.monotonic() + timeout
        while True:
            try:
                granted = self.script()(keys=[self.key], args=[time.time(), self.ttl, self.limit, token])
            except Exception as error:  # redis.RedisError and friends
                raise QuotaUnavailable(str(error)) from error
            if granted:
                self.token = token
                return time.monotonic() - started
            if time.monotonic() >= deadline:
                raise QuotaTimeout(f"No model slot within {timeout}s (limit {self.limit})")
            time.sleep(self.poll)

    def release(self):
        token, self.token = self.token, None
        if not token:
            return
        try:
            self.client.zrem(self.key, token)
        except Exception:  # an unreleased slot still expires with its ttl
            pass

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
