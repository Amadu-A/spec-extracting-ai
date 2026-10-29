# app/main.py
"""ASGI entrypoint; команды запуска не создают shared services или таблицы."""

from app.presentation.http.app import create_app

app = create_app()
