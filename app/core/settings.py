# app/core/settings.py
"""Типизированная конфигурация; открытые defaults перекрываются секретами .env."""

from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения; не читаем окружение вне этой модели."""

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )
    app_bind: str = "0.0.0.0"
    app_port: int = Field(8083, ge=1024, le=65535)
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "specification"
    database_user: str = "specification"
    database_password: SecretStr = SecretStr("")
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "specification"
    rabbitmq_user: str = "specification"
    rabbitmq_password: SecretStr = SecretStr("")
    rabbitmq_queue: str = "specification.extractions"
    shared_vlm_base_url: str = "http://shared-vlm:8000/v1"
    shared_vlm_model: str = "shared-vlm"
    vlm_timeout_seconds: int = Field(240, ge=5, le=1800)
    vlm_max_tokens: int = Field(16000, ge=256, le=64000)
    vlm_attempts: int = Field(3, ge=1, le=5)
    worker_concurrency: int = Field(1, ge=1, le=4)
    lease_seconds: int = Field(90, ge=30)
    heartbeat_seconds: int = Field(10, ge=1, le=20)
    max_queued_jobs: int = Field(100, ge=1)
    data_directory: Path = Path("data")
    max_upload_mb: int = Field(50, ge=1, le=500)
    max_pdf_pages: int = Field(500, ge=1, le=5000)
    max_selected_pages: int = Field(100, ge=1, le=500)
    render_dpi: int = Field(160, ge=72, le=300)
    max_image_pixels: int = Field(6_000_000, ge=100_000, le=20_000_000)
    max_page_points: int = Field(20000, ge=1000, le=50000)
    max_stream_chars: int = Field(2_000_000, ge=10000, le=10_000_000)
    max_events: int = Field(100000, ge=1000)
    max_items: int = Field(20000, ge=100, le=100000)
    retention_hours: int = Field(24, ge=1, le=720)
    session_ttl_minutes: int = Field(60, ge=1, le=1440)
    cookie_secure: bool = False
    trusted_proxy_hosts: list[str] = Field(default_factory=list)
    ui_password: SecretStr = SecretStr("")
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    rate_limit_per_minute: int = Field(120, ge=10)
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        """Собирает DSN с экранированием пароля; запрещено записывать в журнал."""
        password = quote(self.database_password.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{quote(self.database_user, safe='')}:{password}"
            f"@{self.database_host}:{self.database_port}/{quote(self.database_name, safe='')}"
        )

    @property
    def amqp_url(self) -> str:
        """Возвращает адрес существующего app-vhost без раскрытия credentials."""
        password = quote(self.rabbitmq_password.get_secret_value(), safe="")
        return (
            f"amqp://{quote(self.rabbitmq_user, safe='')}:{password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/{quote(self.rabbitmq_vhost, safe='')}"
        )

    def require_secrets(self) -> None:
        """Останавливает production entrypoint до запуска при пустых секретах."""
        for name in ("database_password", "rabbitmq_password", "ui_password"):
            if len(getattr(self, name).get_secret_value()) < 12:
                raise ValueError(f"Секрет {name} должен содержать не менее 12 символов")
        if any(len(key.get_secret_value()) < 24 for key in self.api_keys.values()):
            raise ValueError("API_KEYS: каждый ключ должен содержать не менее 24 символов")
