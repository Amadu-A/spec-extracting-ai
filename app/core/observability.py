# app/core/observability.py
"""Единый JSON-журнал и измерение публичных операций без записи документов."""

import inspect
import json
import logging
import traceback
from functools import wraps
from time import perf_counter


class JsonFormatter(logging.Formatter):
    """Выводит разрешённые технические поля, исключая произвольные payload."""

    def format(self, record: logging.LogRecord) -> str:
        """Формирует одну строку для Docker logging с ограниченной ротацией."""
        data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "event": record.getMessage()
            if record.name.startswith("app.") or record.name.startswith("uvicorn.")
            else "dependency_log",
            "logger": record.name,
            "service": "spec-extracting-ai",
        }
        for name in ("operation", "duration_ms", "status", "job_id", "request_id"):
            if hasattr(record, name):
                data[name] = getattr(record, name)
        if record.exc_info:
            data["exception_type"] = record.exc_info[0].__name__
            data["traceback"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Настраивает stdout; Docker отвечает за срок и размер хранения."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def timed(operation: str):
    """Измеряет sync/async use-case; исключение и отмена остаются ошибкой."""

    def decorate(function):
        """Сохраняет сигнатуру и имя функции для DI и тестов."""

        def report(started: float, status: str) -> None:
            """Пишет единственное итоговое событие без дублирования traceback."""
            logging.getLogger(function.__module__).info(
                "operation_timing",
                extra={
                    "operation": operation,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "status": status,
                },
            )

        @wraps(function)
        async def asynchronous(*args, **kwargs):
            """Измеряет coroutine, сохраняя семантику CancelledError."""
            started, status = perf_counter(), "error"
            try:
                result = await function(*args, **kwargs)
                status = "success"
                return result
            finally:
                report(started, status)

        @wraps(function)
        def synchronous(*args, **kwargs):
            """Измеряет обычную операцию, не перехватывая исключение."""
            started, status = perf_counter(), "error"
            try:
                result = function(*args, **kwargs)
                status = "success"
                return result
            finally:
                report(started, status)

        return asynchronous if inspect.iscoroutinefunction(function) else synchronous

    return decorate
