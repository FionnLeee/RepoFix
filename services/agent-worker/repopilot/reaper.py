"""Reclaim sandbox containers whose owning attempt is gone.

A worker that dies mid-run leaves its sandbox container behind (the container is a
sibling on the Docker host, not a child of the worker process). A sweep removes labelled
sandboxes that no active run owns, so a later recovery always restores into a fresh
container instead of reusing an unexplained one.
"""

import time

LABEL = "repopilot.managed=sandbox"


def reap(client, active_runs, *, min_age=90, now=None):
    """Remove labelled sandboxes whose run is not active and that are older than ``min_age``."""
    now = time.time() if now is None else now
    removed, kept, failed = [], [], []
    for container in client.containers.list(all=True, filters={"label": LABEL}):
        labels = container.labels or {}
        run_id = labels.get("repopilot.run", "")
        try:
            created = float(labels.get("repopilot.created", "0"))
        except ValueError:
            created = 0.0
        age = now - created
        if run_id in active_runs or age < min_age:
            kept.append({"container": container.id[:12], "run": run_id, "age": round(age, 1)})
            continue
        try:
            container.remove(force=True)
            removed.append({"container": container.id[:12], "run": run_id, "age": round(age, 1)})
        except Exception as error:  # a container removed concurrently is not a failure state
            failed.append({"container": container.id[:12], "run": run_id, "error": str(error)})
    return {"removed": removed, "kept": kept, "failed": failed}
