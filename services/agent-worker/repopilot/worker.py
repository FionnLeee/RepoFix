import asyncio
import logging
import os
import socket
import threading
import uuid

import aio_pika
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repopilot.runtime import Cancelled, execute_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1, le=1)
    message_id: str
    run_id: uuid.UUID


async def handle(message: aio_pika.IncomingMessage):
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
                        response.raise_for_status()
                        if response.json().get("cancelRequested"):
                            cancelled.set()
                        return
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt:
                            raise

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
                            response.raise_for_status()
                            if response.json().get("cancelRequested"):
                                cancelled.set()
                        except httpx.HTTPError:
                            cancelled.set()
                            return

            pulse = asyncio.create_task(heartbeat())
            try:
                result = await asyncio.to_thread(execute_run, run, emit, cancelled)
                status = (
                    "CANCELLED" if cancelled.is_set() else "SUCCEEDED" if result["verification"]["passed"] else "FAILED"
                )
                await asyncio.to_thread(emit, status, result, status)
                logging.info("Run %s completed: %s", run["id"], status)
            except Exception as error:
                status = "CANCELLED" if isinstance(error, Cancelled) or cancelled.is_set() else "FAILED"
                text = str(error)
                if key := os.environ.get("OPENAI_API_KEY"):
                    text = text.replace(key, "[REDACTED]")
                text = text[:1500]
                await asyncio.to_thread(emit, status, {"error": text, "mode": run["mode"]}, status)
                logging.warning("Run %s stopped: %s", run["id"], type(error).__name__)
            finally:
                done.set()
                await pulse
                sync.close()


async def main():
    connection = await aio_pika.connect_robust(os.environ["AMQP_URL"])
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue("repopilot.runs.v1", durable=True)
        await queue.consume(handle)
        logging.info("Worker %s waiting for tasks", socket.gethostname())
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
