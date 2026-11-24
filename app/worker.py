# app/worker.py
"""Отдельный bounded consumer и DB-outbox dispatcher; браузер не участвует в lifecycle."""

import asyncio
import contextlib
import logging
from random import uniform
from time import time
from uuid import UUID

from app.bootstrap import build_container
from app.core.observability import configure_logging
from app.domain.delivery import LEGACY_DELIVERY

logger = logging.getLogger(__name__)


async def dispatch_once(container) -> int:
    """Публикует не более четырёх резервов и ждёт положительного AMQP confirm каждого."""
    sent = 0
    for _ in range(min(4, container.settings.max_queued_jobs)):
        claim = await container.repository.claim_delivery(container.settings.retention_seconds)
        if claim is None:
            break
        detail = {
            "job_id": claim.job_id,
            "revision": claim.revision,
            "attempt": claim.attempt,
            "recovering": claim.recovering,
        }
        logger.info("delivery_attempt", extra=detail)
        try:
            # Включает reconnect и AMQP confirm, заведомо короче 30-секундного резерва.
            async with asyncio.timeout(8):
                await container.broker.publish(claim.job_id, claim.revision)
        except Exception as exc:
            delay = min(55, 2 ** min(claim.attempt, 6)) + uniform(0, 5)
            await container.repository.finish_delivery(claim, delay)
            logger.warning("delivery_retry", extra={**detail, "error_type": type(exc).__name__})
            break
        logger.info("delivery_confirm", extra=detail)
        await container.repository.finish_delivery(claim, None)
        sent += 1
    return sent


async def consume_delivery(container, message) -> None:
    """ACK выполняется после durable работы или безопасного отказа в захвате."""
    async with message.process(requeue=True):
        revision = message.headers.get("revision", LEGACY_DELIVERY)
        try:
            job_id = str(UUID(message.body.decode("ascii")))
            if "revision" in message.headers and (type(revision) is not int or revision < 0):
                raise ValueError("Некорректная ревизия")
        except (ValueError, UnicodeError):
            logger.warning("invalid_queue_message")
            return
        try:
            handled = await container.jobs.execute(job_id, revision)
        except Exception:
            # При недоступности БД не гоняем одну и ту же delivery в горячем цикле.
            await asyncio.sleep(2)
            raise
    logger.info(
        "delivery_ack" if handled else "delivery_ack_noop",
        extra={
            "job_id": job_id,
            "revision": revision,
            "redelivered": message.redelivered,
        },
    )


async def maintenance(container) -> None:
    """Отправляет только ожидающие резервы и обновляет heartbeat worker."""
    while True:
        try:
            await asyncio.to_thread(
                (container.settings.data_directory / "worker.heartbeat").write_text,
                str(time()),
                encoding="ascii",
            )
            await dispatch_once(container)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning("worker_maintenance_retry")
        await asyncio.sleep(10)


async def cleanup(container) -> None:
    """Удаляет истёкшие документы и сессии отдельным циклом, по умолчанию каждые 15 секунд."""
    while True:
        try:
            await container.extractions.purge_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning("worker_cleanup_retry")
        await asyncio.sleep(container.settings.cleanup_interval_seconds)


async def run() -> None:
    """Подключает существующий broker; ack выполняется после durable terminal/checkpoint."""
    container = build_container()
    configure_logging(container.settings.log_level)
    maintenance_task = asyncio.create_task(maintenance(container))
    cleanup_task = asyncio.create_task(cleanup(container))
    slots = asyncio.Semaphore(container.settings.worker_concurrency)
    active: set[asyncio.Task] = set()

    async def consume(message) -> None:
        """Ограничивает job concurrency независимо от размера доставки RabbitMQ."""
        async with slots:
            try:
                await consume_delivery(container, message)
            except Exception as exc:
                logger.warning(
                    "delivery_processing_retry", extra={"error_type": type(exc).__name__}
                )

    try:
        while True:
            try:
                await container.broker.connect()
                async with container.broker.queue.iterator() as iterator:
                    logger.info("broker_consumer_subscribed")
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
        cleanup_task.cancel()
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance_task
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        await container.broker.close()
        await container.model.close()


if __name__ == "__main__":
    asyncio.run(run())
