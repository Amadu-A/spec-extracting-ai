# app/domain/errors.py
"""Предметные ошибки, которые HTTP и worker преобразуют в публичные коды."""


class ServiceError(Exception):
    """Содержит безопасный текст и машинный код, не связанный с FastAPI."""

    def __init__(self, code: str, message: str, status: int = 422):
        """Фиксирует ошибку; технические credentials не передаются в message."""
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class LeaseLost(Exception):
    """Запрещает старому worker публиковать данные после перехвата lease."""
