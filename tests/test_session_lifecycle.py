# tests/test_session_lifecycle.py
"""Анонимные capability и реальные SQLite/PDF: изоляция, фиксированная сессия и удаление по TTL."""

import asyncio
import contextlib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.orm import Session as DbSession

from app.domain.errors import ServiceError
from app.domain.models import JobState, utcnow
from app.infrastructure.database import SessionRecord
from app.presentation.http.app import create_app
from app.worker import cleanup
from tests.conftest import KEY_A
from tests.test_recovery import uploaded


def freeze(monkeypatch, instant):
    """Заменяет только UTC-часы, не SQLite/файлы/обработку PDF; не требует ожидания часов."""
    for module in (
        "app.application.service",
        "app.application.processor",
        "app.application.sessions",
        "app.infrastructure.database",
    ):
        monkeypatch.setattr(module + ".utcnow", lambda: instant)


async def test_page_creates_private_cookie_without_login(client, root):
    """Открытие страницы сразу даёт capability; парольная форма и endpoint отсутствуют."""
    page = await client.get("/")
    assert page.status_code == 200
    assert "data-login" not in page.text and 'type="password"' not in page.text
    cookie = page.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=10800" in cookie
    token = client.cookies["spec_session"]
    session = await root.sessions.resolve(token)
    assert session.token_hash != token
    info = (await client.get("/api/v1/session")).json()
    assert set(info) == {"csrf", "expires_at"}
    assert (await client.post("/api/v1/session/login", json={})).status_code == 404
    schema = (await client.get("/openapi.json")).json()
    assert "/api/v1/session/login" not in schema["paths"]
    assert "LoginInput" not in schema["components"]["schemas"]


async def test_anonymous_browsers_cannot_read_each_other(root, client, pdf_bytes):
    """Отсутствие пароля сохраняет owner-boundary для PDF, SSE, snapshots и экспортов."""
    info = (await client.get("/api/v1/session")).json()
    response = await client.post(
        "/api/v1/extractions",
        headers={"X-CSRF-Token": info["csrf"]},
        files={"file": ("a.pdf", pdf_bytes, "application/pdf")},
        data={"pages": "1", "project_code": "P"},
    )
    assert response.status_code == 202
    job_id, doc_id = response.json()["job_id"], response.json()["document_id"]
    await root.processor.execute(job_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(root)), base_url="http://test"
    ) as other:
        await other.get("/api/v1/session")
        assert client.cookies["spec_session"] != other.cookies["spec_session"]
        paths = [
            f"/extractions/{job_id}{suffix}"
            for suffix in (
                "",
                "/snapshot",
                "/events",
                "/result.json",
                "/result.xlsx",
                "/warnings",
                "/pages/1/preview",
            )
        ]
        paths += [f"/documents/{doc_id}", f"/documents/{doc_id}/pages/1/preview"]
        for path in paths:
            assert (await other.get("/api/v1" + path)).status_code == 404
    assert (await client.get(f"/api/v1/extractions/{job_id}/result.json")).status_code == 200


async def test_session_has_fixed_lifetime_and_renews_without_password(root, client, monkeypatch):
    """Активность не сдвигает expiry; истёкший capability автоматически заменяется новым."""
    await client.get("/")
    token = client.cookies["spec_session"]
    session = await root.sessions.resolve(token)
    await root.sessions.prompt(session, value="Личные инструкции")
    original = session.expires_at
    await asyncio.gather(*[root.sessions.resolve(token) for _ in range(4)])
    assert (await root.sessions.resolve(token)).expires_at == original
    freeze(monkeypatch, original + timedelta(seconds=1))
    assert (await client.get("/api/v1/session/prompt")).status_code == 401
    renewed = await client.get("/api/v1/session")
    assert renewed.status_code == 200 and client.cookies["spec_session"] != token
    assert (await client.get("/api/v1/session/prompt")).json()["is_default"]
    with pytest.raises(ServiceError):
        await root.sessions.resolve(token)


async def test_legacy_password_session_remains_readable(root):
    """Снимки старой версии читаются без миграции БД и без проверки authenticated."""
    token, session = await root.sessions.create()
    with DbSession(root.repository.engine) as db, db.begin():
        row = db.get(SessionRecord, session.token_hash)
        row.payload = {**row.payload, "authenticated": False}
    assert (await root.sessions.resolve(token)).csrf == session.csrf


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_retention_starts_at_terminal_state(root, pdf_bytes, monkeypatch, outcome):
    """Час хранения начинается после успеха/ошибки/отмены, а не после загрузки."""
    start = utcnow()
    freeze(monkeypatch, start)
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    finished = start + timedelta(minutes=15)
    freeze(monkeypatch, finished)
    if outcome == "cancelled":
        await root.extractions.cancel(job.id, "api:alpha")
    else:
        if outcome == "failed":
            root.model.responses = [
                ServiceError("vlm_unavailable", "Недоступен тестовый адаптер", 503)
            ]
        await root.processor.execute(job.id)
    current = await root.repository.get(job.id)
    expected = finished + timedelta(seconds=3600)
    assert current.state.value == outcome and current.expires_at == expected
    assert (await root.repository.document(doc.id)).expires_at == expected
    freeze(monkeypatch, expected - timedelta(microseconds=1))
    assert await root.extractions.owned(job.id, "api:alpha")
    freeze(monkeypatch, expected)
    with pytest.raises(ServiceError) as error:
        await root.extractions.owned(job.id, "api:alpha")
    assert error.value.status == 410


async def test_expired_result_is_denied_before_cleanup(root, client, pdf_bytes, monkeypatch):
    """API закрывает exports/preview/SSE точно по TTL, даже пока файл физически существует."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await root.processor.execute(job.id)
    expiry = (await root.repository.get(job.id)).expires_at
    freeze(monkeypatch, expiry)
    for suffix in ("", "/events", "/result.json", "/result.xlsx", "/snapshot", "/pages/1/preview"):
        response = await client.get(
            f"/api/v1/extractions/{job.id}{suffix}", headers={"X-API-Key": KEY_A}
        )
        assert response.status_code == 410
    assert (root.settings.data_directory / doc.id).exists()
    await root.extractions.purge_expired()
    assert not (root.settings.data_directory / doc.id).exists()
    assert await root.repository.get(job.id) is None


async def test_one_pdf_survives_until_last_job_expires(root, pdf_bytes, monkeypatch):
    """Истечение первого результата не удаляет оригинал второго задания на том же PDF."""
    start = utcnow()
    freeze(monkeypatch, start)
    doc = await uploaded(root, pdf_bytes)
    first = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await root.processor.execute(first.id)
    freeze(monkeypatch, start + timedelta(minutes=20))
    second = await root.extractions.start("api:alpha", doc.id, "2", "P")
    freeze(monkeypatch, start + timedelta(minutes=61))
    await root.extractions.purge_expired()
    assert await root.repository.get(first.id) is None
    assert (await root.repository.get(second.id)).state == JobState.QUEUED
    assert (root.settings.data_directory / doc.id).exists()
    await root.processor.execute(second.id)
    expiry = (await root.repository.get(second.id)).expires_at
    assert (await root.repository.document(doc.id)).expires_at == expiry
    freeze(monkeypatch, expiry)
    await root.extractions.purge_expired()
    assert not (root.settings.data_directory / doc.id).exists()


@pytest.mark.parametrize("state", [JobState.QUEUED, JobState.RUNNING])
async def test_overdue_jobs_fail_then_retain_for_an_hour(root, pdf_bytes, monkeypatch, state):
    """Очистка после простоя worker завершает зависшее задание, сохраняя час диагностики."""
    now = utcnow()
    freeze(monkeypatch, now)
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")

    def overdue(current):
        """Имитирует сохранённое задание, бюджет которого уже закончился."""
        current.state = state
        current.expires_at = now + timedelta(seconds=root.settings.retention_seconds - 1)

    await root.repository.edit(job.id, overdue)
    await root.extractions.purge_expired()
    current = await root.repository.get(job.id)
    assert current.state == JobState.FAILED and current.error_code == "job_timeout"
    assert current.expires_at == now + timedelta(hours=1)
    assert (root.settings.data_directory / doc.id).exists()


async def test_running_budget_is_bounded_without_cleanup(root, pdf_bytes):
    """Сам обработчик останавливается по deadline, даже когда цикл очистки не запущен."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    root.model.delay = 30

    def near_deadline(current):
        """Оставляет короткий реальный бюджет без ожидания часа в тесте."""
        current.expires_at = utcnow() + timedelta(seconds=root.settings.retention_seconds + 1)

    await root.repository.edit(job.id, near_deadline)
    await asyncio.wait_for(root.processor.execute(job.id), 4)
    current = await root.repository.get(job.id)
    assert current.state == JobState.FAILED and current.error_code == "job_timeout"


async def test_background_cleanup_deletes_without_browser(root, pdf_bytes, monkeypatch):
    """Настоящий цикл worker удаляет истёкший PDF без HTTP-запроса пользователя."""
    doc = await uploaded(root, pdf_bytes)
    freeze(monkeypatch, doc.expires_at + timedelta(seconds=1))
    done = asyncio.Event()
    purge = root.extractions.purge_expired

    async def observed():
        """Наблюдает завершение настоящей очистки без замены файлового хранилища."""
        await purge()
        done.set()

    monkeypatch.setattr(root.extractions, "purge_expired", observed)
    task = asyncio.create_task(cleanup(root))
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert not (root.settings.data_directory / doc.id).exists()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
