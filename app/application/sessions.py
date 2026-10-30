# app/application/sessions.py
"""Анонимные browser capability и сессионные prompt; вход пользователя не требуется."""

import hashlib
import secrets
from datetime import timedelta

from app.application.ports import JobRepository
from app.core.observability import timed
from app.core.settings import Settings
from app.domain.errors import ServiceError
from app.domain.models import Session, utcnow


def token_hash(token: str) -> str:
    """Cookie и API credentials сравниваются/сохраняются только по SHA256."""
    return hashlib.sha256(token.encode()).hexdigest()


class SessionService:
    """Серверное хранилище анонимного владельца с фиксированным сроком cookie."""

    def __init__(self, repository: JobRepository, settings: Settings, default_prompt: str):
        """Принимает порт репозитория и стандартный версионированный ресурс."""
        self.repository, self.settings, self.default_prompt = repository, settings, default_prompt

    async def create(self) -> tuple[str, Session]:
        """Создаёт случайный capability без пароля; в БД сохраняется только его хэш."""
        token = secrets.token_urlsafe(48)
        session = Session(
            token_hash=token_hash(token),
            csrf=secrets.token_urlsafe(32),
            expires_at=utcnow() + timedelta(seconds=self.settings.session_ttl_seconds),
        )
        await self.repository.save_session(session)
        return token, session

    async def resolve(self, token: str | None) -> Session:
        """Проверяет capability без продления TTL; истёкшая сессия создаётся заново через API."""
        session = (
            await self.repository.session(token_hash(token)) if token and len(token) < 200 else None
        )
        if not session:
            raise ServiceError(
                "session_expired", "Веб-сессия истекла; загрузите документ повторно", 401
            )
        return session

    @timed("manage_session_prompt")
    async def prompt(
        self, session: Session, value: str | None = None, restore: bool = False
    ) -> dict:
        """Сохраняет/сбрасывает prompt только текущей сессии; running snapshot неизменен."""
        if restore:
            session.prompt = None
        elif value is not None:
            if not value.strip() or len(value) > 20000:
                raise ServiceError("invalid_prompt", "Промпт должен содержать 1–20000 символов")
            session.prompt = value
        if restore or value is not None:
            await self.repository.save_session(session)
        effective = session.prompt if session.prompt is not None else self.default_prompt
        return {
            "prompt": effective,
            "is_default": session.prompt is None,
            "version": token_hash(effective),
            "expires_at": session.expires_at.isoformat(),
        }
