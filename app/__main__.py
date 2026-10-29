# app/__main__.py
"""Контейнерный HTTP entrypoint читает доверие proxy только через типизированные Settings."""

import uvicorn

from app.core.settings import Settings


def main() -> None:
    """Запускает один ASGI worker; forwarded headers принимаются только от явных IP."""
    settings = Settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8083,
        proxy_headers=bool(settings.trusted_proxy_hosts),
        forwarded_allow_ips=",".join(settings.trusted_proxy_hosts),
    )


if __name__ == "__main__":
    main()
