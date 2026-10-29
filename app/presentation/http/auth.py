# app/presentation/http/auth.py
"""API credentials и cookie sessions имеют независимых owners и CSRF-политику."""

import secrets
from dataclasses import dataclass

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.application.sessions import token_hash
from app.domain.errors import ServiceError
from app.domain.models import Session

COOKIE = "spec_session"
API_KEY = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Отдельный ключ 1С/бота из API_KEYS"
)


@dataclass
class Principal:
    """Авторизованный контекст запроса без открытого API key или cookie."""

    owner: str
    session: Session | None = None


def container(request: Request):
    """Возвращает composition root, собранный при старте приложения."""
    return request.app.state.container


def check_csrf(request: Request, session: Session) -> None:
    """Защищает cookie write actions token и same-origin проверкой."""
    supplied = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(supplied, session.csrf):
        raise ServiceError("csrf_failed", "Не прошла CSRF-проверка", 403)
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin.rstrip("/") != expected:
        raise ServiceError("origin_failed", "Недопустимый источник запроса", 403)


async def principal(request: Request, api_key: str | None = Security(API_KEY)) -> Principal:
    """API-key всегда выбирает интеграцию, даже если браузер прислал cookie."""
    dependencies = container(request)
    key = api_key
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        key = authorization[7:]
    if key is not None:
        if len(key) > 512:
            raise ServiceError("invalid_api_key", "Недействительный API credential", 401)
        digest = token_hash(key)
        for name, expected in dependencies.settings.api_keys.items():
            if secrets.compare_digest(digest, token_hash(expected.get_secret_value())):
                return Principal(owner="api:" + name)
        raise ServiceError("invalid_api_key", "Недействительный API credential", 401)
    session = await dependencies.sessions.resolve(request.cookies.get(COOKIE))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_csrf(request, session)
    return Principal(owner="web:" + session.token_hash, session=session)
