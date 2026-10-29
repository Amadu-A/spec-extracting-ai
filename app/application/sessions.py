# app/application/sessions.py
"""Сессионные prompt и аутентификация UI; внешние API-клиенты от них независимы."""

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
    """Серверное TTL-хранилище с prompt, ограниченным конкретным cookie."""

    def __init__(self, repository: JobRepository, settings: Settings, default_prompt: str):
        """Принимает порт репозитория и стандартный версионированный ресурс."""
        self.repository, self.settings, self.default_prompt = repository, settings, default_prompt

    async def create(self) -> tuple[str, Session]:
        """Создаёт неаутентифицированную сессию; cookie не имеет постоянного Max-Age."""
        token = secrets.token_urlsafe(48)
        session = Session(
            token_hash=token_hash(token),
            csrf=secrets.token_urlsafe(32),
            expires_at=utcnow() + timedelta(minutes=self.settings.session_ttl_minutes),
        )
        await self.repository.save_session(session)
        return token, session

    async def resolve(self, token: str | None, authenticated: bool = True) -> Session:
        """Продлевает inactivity TTL; неизвестная/истёкшая сессия требует входа."""
        session = (
            await self.repository.touch_session(
                token_hash(token), utcnow() + timedelta(minutes=self.settings.session_ttl_minutes)
            )
            if token and len(token) < 200
            else None
        )
        if not session or (authenticated and not session.authenticated):
            raise ServiceError(
                "session_expired", "Войдите в веб-сессию; временный промпт сброшен", 401
            )
        return session

    @timed("web_login")
    async def login(self, token: str, password: str) -> tuple[str, Session]:
        """Проверяет пароль и ротирует session id против fixation."""
        old = await self.resolve(token, authenticated=False)
        expected = self.settings.ui_password.get_secret_value()
        if not expected or not secrets.compare_digest(token_hash(password), token_hash(expected)):
            raise ServiceError("invalid_credentials", "Неверный пароль", 401)
        await self.repository.delete_session(old.token_hash)
        new_token, session = await self.create()
        session.authenticated = True
        await self.repository.save_session(session)
        return new_token, session

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
