# app/infrastructure/broker.py
"""Durable AMQP adapter: только app-очередь в существующем shared RabbitMQ."""

import asyncio

import aio_pika
from pamqp.commands import Basic

from app.core.settings import Settings


class RabbitPublisher:
    """Publisher confirms и bounded очередь исключают молчаливую потерю delivery."""

    def __init__(self, settings: Settings):
        """Не создаёт vhost/пользователей и не использует management/admin API."""
        self.settings = settings
        self.connection = None
        self.channel = None
        self.queue = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Объявляет принадлежащую приложению durable очередь с bounded backlog."""
        async with self._lock:
            if (
                self.connection
                and not self.connection.is_closed
                and self.channel
                and not self.channel.is_closed
                and self.queue is not None
            ):
                return
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
            connection = await aio_pika.connect_robust(self.settings.amqp_url, timeout=5)
            try:
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                await channel.set_qos(prefetch_count=self.settings.worker_concurrency)
                queue = await channel.declare_queue(
                    self.settings.rabbitmq_queue,
                    durable=True,
                    arguments={
                        "x-max-length": self.settings.max_queued_jobs * 2,
                        "x-overflow": "reject-publish",
                    },
                )
            except BaseException:
                # Timeout при объявлении не оставляет открытый channel без очереди.
                await connection.close()
                raise
            self.connection, self.channel, self.queue = connection, channel, queue

    async def publish(self, job_id: str, revision: int | None = None) -> None:
        """Передаёт UUID без PDF/prompt; повторная доставка защищена DB lease."""
        await self.connect()
        confirmation = await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=job_id.encode(),
                content_type="text/plain",
                message_id=job_id if revision is None else f"{job_id}:{revision}",
                headers={} if revision is None else {"revision": revision},
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=self.settings.rabbitmq_queue,
            mandatory=True,
            timeout=5,
        )
        if not isinstance(confirmation, Basic.Ack):
            raise RuntimeError("RabbitMQ не подтвердил доставку")

    async def ping(self) -> bool:
        """Проверяет broker, авторизацию и реальное наличие app-очереди."""
        try:
            await self.connect()
            await self.channel.declare_queue(self.settings.rabbitmq_queue, passive=True)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Закрывает каналы; unacked deliveries остаются у RabbitMQ."""
        if self.connection:
            await self.connection.close()
