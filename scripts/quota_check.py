"""Verify the shared model quota against the Redis the workers actually use.

Runs inside a worker container (the only place with REDIS_URL and the redis client):
    docker compose exec -T worker sh -c 'python scripts/quota_check.py'
No model calls are made.
"""

import concurrent.futures
import json
import os
import time
from pathlib import Path

from repopilot.quota import ModelQuota

BURST, HOLD = 6, 1.0
quota = ModelQuota.from_env()
assert quota, "REDIS_URL and MODEL_MAX_CONCURRENCY must both be configured for this check"
assert quota.limit == 2, f"expected the default limit of 2, got {quota.limit}"

# A second, wider quota instance is used only as a plain client for the instrumentation keys.
redis = quota.client
redis.delete("probe:cur", "probe:peak")


def holder(index):
    slot = ModelQuota.from_env()
    waited = slot.acquire(timeout=60)
    try:
        current = redis.incr("probe:cur")
        peak = int(redis.get("probe:peak") or 0)
        if current > peak:
            redis.set("probe:peak", current)
        time.sleep(HOLD)
        redis.decr("probe:cur")
    finally:
        slot.release()
    return {"index": index, "waited_seconds": round(waited, 2)}


started = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=BURST) as pool:
    results = list(pool.map(holder, range(BURST)))
elapsed = time.monotonic() - started
peak = int(redis.get("probe:peak") or 0)
assert peak == quota.limit, f"observed peak concurrency {peak}, limit {quota.limit}"
assert elapsed >= HOLD * (BURST / quota.limit), round(elapsed, 2)
# The first wave is admitted immediately; every other holder has to wait for a released slot.
assert len([r for r in results if r["waited_seconds"] >= 0.5]) == BURST - quota.limit
assert int(redis.zcard(quota.key)) == 0, "all slots should be released after the burst"

# A holder that never releases (crashed worker) must not block the quota forever.
lonely = ModelQuota(quota.client, 1, ttl=1)
lonely.acquire(timeout=5)
assert int(redis.zcard(lonely.key)) == 1
time.sleep(1.5)
reclaimed = ModelQuota(quota.client, 1, ttl=1)
reclaimed.acquire(timeout=5)
reclaimed.release()
assert int(redis.zcard(quota.key)) == 0

assert ModelQuota.from_env({"REDIS_URL": "", "MODEL_MAX_CONCURRENCY": "2"}) is None
report = {"checks": ["shared_limit_enforced_across_independent_clients", "waiters_serialize_instead_of_exceeding_the_limit",
                     "slots_released_after_use", "crashed_holder_slot_reclaimed_by_ttl",
                     "missing_configuration_disables_the_quota"],
          "redis_key": quota.key, "limit": quota.limit, "burst": BURST, "peak_concurrency": peak,
          "elapsed_seconds": round(elapsed, 2), "waiters": BURST - quota.limit, "generative_model_calls": 0}
target = Path(os.environ.get("REPORT_PATH", "/artifacts/validation/quota.json"))
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
