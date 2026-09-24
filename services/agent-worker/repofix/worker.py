import asyncio
import logging
import os
import socket
import threading
import uuid

import aio_pika
import docker
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repofix import tracing as tracing_module
from repofix.quota import ModelQuota
from repofix.reaper import reap
from repofix.reporting import failure_result
from repofix.runtime import ApprovalPaused, Cancelled, OwnershipLost, execute_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1, le=1)
    message_id: str
    run_id: uuid.UUID
    traceparent: str | None = None


def control_request(client: httpx.Client, run_id: str, kind: str, payload: dict) -> dict:
    """Authenticated internal call; a lost attempt stops instead of overwriting newer state."""
    response = client.post(f"/internal/runs/{run_id}/{kind}", json=payload)
    if response.status_code == 409:
        raise OwnershipLost(f"{kind} rejected: {response.text[:200]}")
    response.raise_for_status()
    return response.json()


async def handle(message: aio_pika.IncomingMessage, quota: ModelQuota | None):
    try:
        payload = Message.model_validate_json(message.body)
    except ValidationError:
        await message.reject(requeue=False)
        logging.error("Rejected malformed task message")
        return
    async with message.process(requeue=True):
        worker_id = socket.gethostname()
        base = os.environ.get("CONTROL_API_URL", "http://api:3101")
        headers = {"authorization": f"Bearer {os.environ['WORKER_TOKEN']}"}
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as client:
            try:
                claim = await client.post(f"/internal/runs/{payload.run_id}/claim", json={"workerId": worker_id})
                if claim.status_code == 409:
                    return
                claim.raise_for_status()
            except httpx.HTTPError:
                await asyncio.sleep(5)
                raise
            run = claim.json()
            cancelled, done = threading.Event(), asyncio.Event()
            sync = httpx.Client(base_url=base, headers=headers, timeout=15)

            def emit(kind: str, data: dict, status: str | None = None):
                body = {
                    "workerId": worker_id,
                    "generation": run["generation"],
                    "key": str(uuid.uuid4()),
                    "type": kind,
                    "data": data,
                }
                if status:
                    body["status"] = status
                for attempt in range(2):
                    try:
                        response = sync.post(f"/internal/runs/{run['id']}/step", json=body)
                        if response.status_code == 409:
                            raise OwnershipLost(response.text[:200])
                        response.raise_for_status()
                        if response.json().get("cancelRequested"):
                            cancelled.set()
                        return
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt:
                            raise

            def control(kind: str, payload: dict) -> dict:
                # Ownership of this attempt is stated once here instead of at every call site.
                return control_request(sync, run["id"], kind, {**payload, "workerId": worker_id})

            async def heartbeat():
                while not done.is_set():
                    try:
                        await asyncio.wait_for(done.wait(), timeout=8)
                    except TimeoutError:
                        try:
                            response = await client.post(
                                f"/internal/runs/{run['id']}/step",
                                json={
                                    "workerId": worker_id,
                                    "generation": run["generation"],
                                    "key": str(uuid.uuid4()),
                                    "type": "HEARTBEAT",
                                    "data": {},
                                },
                            )
                            if response.status_code == 409:
                                return  # another attempt owns this run now; nothing left to keep alive
                            response.raise_for_status()
                            if response.json().get("cancelRequested"):
                                cancelled.set()
                        except httpx.HTTPError:
                            cancelled.set()
                            return

            pulse = asyncio.create_task(heartbeat())
            tracing = tracing_module.Tracing(str(payload.run_id), payload.traceparent)
            try:
                result = await asyncio.to_thread(execute_run, run, emit, cancelled, control, quota, tracing)
                if result.get("paused"):
                    # The pause is already persisted (checkpoint + approval request); keep the
                    # control plane's WAITING_APPROVAL status instead of writing a terminal one.
                    logging.info("Run %s paused for approval %s", run["id"], result["paused"].get("approval_id"))
                    return
                verification = result["verification"]
                # A harness-verified run succeeds by producing a patch that replays cleanly; the
                # official harness decides whether that patch resolves the instance.
                accepted = bool(verification.get("passed") or verification.get("delegated"))
                status = "CANCELLED" if cancelled.is_set() else "SUCCEEDED" if accepted else "FAILED"
                await asyncio.to_thread(emit, status, result, status)
                logging.info("Run %s completed: %s", run["id"], status)
            except ApprovalPaused as error:
                logging.info("Run %s paused for approval: %s", run["id"], error)
            except OwnershipLost as error:
                logging.warning("Run %s attempt abandoned without writing state: %s", run["id"], error)
            except Exception as error:
                status = "CANCELLED" if isinstance(error, Cancelled) or cancelled.is_set() else "FAILED"
                result = await asyncio.to_thread(failure_result, run, error)
                try:
                    await asyncio.to_thread(emit, status, result, status)
                except OwnershipLost as lost:
                    logging.warning("Run %s result rejected by a newer attempt: %s", run["id"], lost)
                    return
                logging.warning("Run %s stopped: %s", run["id"], type(error).__name__)
            finally:
                done.set()
                await pulse
                # Flush this attempt's spans; tracing never writes state the run depends on.
                await asyncio.to_thread(tracing.close)
                sync.close()


async def reap_loop(client: httpx.AsyncClient):
    """Sweep sandbox containers whose run is no longer active; never reuse them silently."""
    docker_client = docker.from_env(timeout=45)
    while True:
        try:
            active = (await client.get("/internal/runs/active")).raise_for_status().json()
            result = await asyncio.to_thread(reap, docker_client, set(active["runs"]))
            if result["removed"] or result["failed"]:
                logging.info("Reaper removed %d container(s), %d failure(s)",
                             len(result["removed"]), len(result["failed"]))
        except Exception as error:
            # A control plane that cannot be reached must not lead to blind removals.
            logging.warning("Reaper sweep skipped: %s", type(error).__name__)
        await asyncio.sleep(30)


async def main():
    quota = ModelQuota.from_env()
    connection = await aio_pika.connect_robust(os.environ["AMQP_URL"])
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue("repofix.runs.v1", durable=True)
        await queue.consume(lambda message: handle(message, quota))
        reaper_headers = {"authorization": f"Bearer {os.environ['WORKER_TOKEN']}"}
        async with httpx.AsyncClient(base_url=os.environ.get("CONTROL_API_URL", "http://api:3101"),
                                     headers=reaper_headers, timeout=15) as client:
            sweeper = asyncio.create_task(reap_loop(client))
            logging.info("Worker %s waiting for tasks", socket.gethostname())
            try:
                await asyncio.Future()
            finally:
                sweeper.cancel()


if __name__ == "__main__":
    asyncio.run(main())
