# app/worker.py
"""Отдельный bounded consumer и DB-outbox dispatcher; браузер не участвует в lifecycle."""

import asyncio
import contextlib
import logging
from time import time
from uuid import UUID

from app.bootstrap import build_container
from app.core.observability import configure_logging


async def maintenance(container) -> None:
    """Переотправляет потерянные queued/stale jobs и удаляет данные по TTL."""
    dispatched: dict[str, float] = {}
    iteration = 0
    while True:
        try:
            await asyncio.to_thread(
                (container.settings.data_directory / "worker.heartbeat").write_text,
                str(time()),
                encoding="ascii",
            )
            for job_id in await container.repository.dispatchable(
                container.settings.max_queued_jobs
            ):
                if time() - dispatched.get(job_id, 0) >= 60:
                    await container.broker.publish(job_id)
                    dispatched[job_id] = time()
            dispatched = {key: stamp for key, stamp in dispatched.items() if time() - stamp < 300}
            if iteration % 6 == 0:
                for document_id in await container.repository.cleanup():
                    await container.storage.delete(document_id)
                await container.storage.cleanup_orphans(
                    container.repository.document,
                    time() - container.settings.retention_hours * 3600,
                )
            iteration += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning("worker_maintenance_retry")
        await asyncio.sleep(10)


async def run() -> None:
    """Подключает существующий broker; ack выполняется после durable terminal/checkpoint."""
    container = build_container()
    configure_logging(container.settings.log_level)
    maintenance_task = asyncio.create_task(maintenance(container))
    slots = asyncio.Semaphore(container.settings.worker_concurrency)
    active: set[asyncio.Task] = set()

    async def consume(message) -> None:
        """Ограничивает job concurrency независимо от размера доставки RabbitMQ."""
        async with slots, message.process(requeue=True):
            try:
                job_id = str(UUID(message.body.decode("ascii")))
            except (ValueError, UnicodeError):
                logging.getLogger(__name__).warning("invalid_queue_message")
                return
            await container.processor.execute(job_id)

    try:
        while True:
            try:
                await container.broker.connect()
                async with container.broker.queue.iterator() as iterator:
                    async for message in iterator:
                        task = asyncio.create_task(consume(message))
                        active.add(task)
                        task.add_done_callback(active.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("broker_consumer_reconnect")
                await asyncio.sleep(5)
    finally:
        maintenance_task.cancel()
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance_task
        await container.broker.close()
        await container.model.close()


if __name__ == "__main__":
    asyncio.run(run())
