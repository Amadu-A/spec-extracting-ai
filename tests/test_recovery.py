# tests/test_recovery.py
"""Сбои, повторная доставка, checkpoint, fencing и TTL проверяются на реальном репозитории."""

import asyncio
from datetime import timedelta

import pytest

from app.domain.errors import LeaseLost, ServiceError
from app.domain.models import JobState, utcnow
from tests.conftest import uploaded


async def test_concurrent_delivery_processes_once(root, pdf_bytes):
    """Два delivery одного job не вызывают две параллельные модели."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await asyncio.gather(root.processor.execute(job.id), root.processor.execute(job.id))
    assert len(root.model.calls) == 1
    assert (await root.repository.get(job.id)).state == JobState.COMPLETED


async def test_restart_resumes_after_committed_page(root, pdf_bytes):
    """Перезапуск сохраняет готовый первый лист, повторяет незавершённый второй."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1-3", "P")
    root.model.delay = 0.1
    task = asyncio.create_task(root.processor.execute(job.id))
    for _ in range(400):
        current = await root.repository.get(job.id)
        if current.current_pdf_page == 2:
            break
        await asyncio.sleep(0.02)
    assert current.pages[0].state == "completed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    def expire(current):
        """Имитирует истечение lease остановленного процесса, не меняя checkpoint."""
        current.lease_until = utcnow() - timedelta(seconds=1)

    await root.repository.edit(job.id, expire)
    root.model.delay = 0
    await root.processor.execute(job.id)
    result = await root.repository.get(job.id)
    assert result.state == JobState.COMPLETED and len(result.export().items) == 3
    assert sum(c["context"]["pdf_page"] == 1 for c in root.model.calls) == 1


async def test_stale_worker_is_fenced(root, pdf_bytes):
    """Изменившийся lease запрещает старому процессу публиковать raw/validated данные."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")

    def claim(current):
        """Воспроизводит перехват задания другим worker."""
        current.state, current.lease_token = JobState.RUNNING, "new-worker"

    await root.repository.edit(job.id, claim)
    with pytest.raises(LeaseLost):
        await root.processor._edit(job.id, "old-worker", lambda current: current.emit("wrong"))
    events = await root.repository.events(job.id, 0, 100)
    assert all(event.type != "wrong" for event in events)


async def test_retry_invalid_json_recovers_without_items_from_bad_attempt(root, pdf_bytes):
    """Невалидная попытка не создаёт позиций; повтор сохраняет единственный набор."""
    root.settings.vlm_attempts = 2
    root.model.responses = ["{invalid"]
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await root.processor.execute(job.id)
    current = await root.repository.get(job.id)
    assert current.state == JobState.COMPLETED and len(current.export().items) == 1
    assert current.pages[0].attempts == 2
    events = await root.repository.events(job.id, 0, 100)
    assert sum(event.type == "item_validated" for event in events) == 1
    assert any(event.type == "retry_scheduled" for event in events)


async def test_capacity_and_expiry_cleanup(root, pdf_bytes):
    """Backpressure и TTL не затрагивают чужие файлы и активные документы."""
    doc = await uploaded(root, pdf_bytes)
    root.settings.max_queued_jobs = 1
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    with pytest.raises(ServiceError) as error:
        await root.extractions.start("api:alpha", doc.id, "2", "P")
    assert error.value.code == "queue_full"

    def expire(current):
        """Принудительно переводит срок хранения в прошлое для regression TTL."""
        current.expires_at = utcnow() - timedelta(seconds=1)

    await root.repository.edit(job.id, expire)
    with pytest.raises(ServiceError) as error:
        await root.extractions.owned(job.id, "api:alpha")
    assert error.value.code == "job_expired"
    doc.expires_at = utcnow() - timedelta(seconds=1)
    await root.repository.save_document(doc)
    removed = await root.repository.cleanup()
    assert removed == [doc.id] and await root.repository.get(job.id) is None
    await root.storage.delete(doc.id)
    assert not (root.settings.data_directory / doc.id).exists()


async def test_read_activity_cannot_overwrite_new_session_prompt(root):
    """Чтение сессии не меняет её фиксированный TTL и не перезаписывает свежий prompt."""
    token, session = await root.sessions.create()
    await root.repository.save_session(session)
    await root.sessions.prompt(session, value="Новые инструкции")
    await asyncio.gather(*[root.sessions.resolve(token) for _ in range(10)])
    current = await root.repository.session(session.token_hash)
    assert current.prompt == "Новые инструкции"


async def test_timeout_has_distinct_failed_code(root, pdf_bytes):
    """Полный timeout модели не теряется под общим internal_error."""
    root.model.responses = [TimeoutError()]
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await root.processor.execute(job.id)
    current = await root.repository.get(job.id)
    assert current.state == JobState.FAILED and current.error_code == "vlm_timeout"


async def test_export_rejects_revision_changed_during_generation(root, pdf_bytes):
    """Скачивание не возвращает старую ревизию, если во время генерации начался page retry."""
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingExporter:
        """Задерживает только сериализацию, не меняя проверку статуса и ревизии."""

        async def excel(self, result, warnings):
            """Позволяет тесту изменить ревизию до выдачи подготовленных bytes."""
            started.set()
            await release.wait()
            return b"workbook"

    root.exports.exporter = BlockingExporter()
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await root.processor.execute(job.id)
    task = asyncio.create_task(root.exports.excel(job.id, "api:alpha"))
    await started.wait()
    await root.extractions.retry(job.id, "api:alpha", 1)
    release.set()
    with pytest.raises(ServiceError) as error:
        await task
    assert error.value.code == "result_changed"
