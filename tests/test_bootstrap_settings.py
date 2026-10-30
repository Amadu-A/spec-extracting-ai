# tests/test_bootstrap_settings.py
"""Валидация и закрытый протокол настроек автоподготовки RabbitMQ без реального брокера."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import operations
from app.core.settings import Settings

PASSWORD = "S p'ec\"!$#&\\()=end  "


def bootstrap_settings(**overrides):
    """Задаёт только искусственные тестовые секреты, не читая пользовательские dotenv."""
    values = {
        "database_password": "offline-database-password",
        "rabbitmq_password": PASSWORD,
        "ui_password": "offline-interface-password",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_password_transport_keeps_special_characters(monkeypatch, capsys):
    """Закрытый stdout сохраняет точный пароль, включая пробелы, без shell-интерпретации."""
    settings = bootstrap_settings()
    monkeypatch.setattr(operations, "Settings", lambda: settings)
    assert operations.broker_config() == 0
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        settings.rabbitmq_container,
        settings.rabbitmq_user,
        settings.rabbitmq_vhost,
        PASSWORD,
        str(settings.rabbitmq_cli_timeout_seconds),
    ]
    assert not output.err


@pytest.mark.parametrize(
    "overrides",
    [
        {"rabbitmq_user": "guest"},
        {"rabbitmq_user": "ADMIN"},
        {"rabbitmq_user": "shared_admin"},
        {"rabbitmq_user": "administrator"},
        {"rabbitmq_user": "--admin"},
        {"rabbitmq_user": "$(touch invalid)"},
        {"rabbitmq_user": "a" * 65},
        {"rabbitmq_vhost": "/"},
        {"rabbitmq_vhost": "other vhost"},
        {"rabbitmq_container": "--privileged"},
        {"rabbitmq_container": "broker\nextra-line"},
        {"rabbitmq_password": "private-secret\nnext-line"},
        {"rabbitmq_password": "private-secret\rnext-line"},
        {"rabbitmq_password": "private-secret\0next-line"},
        {"rabbitmq_password": "я" * 513},
        {"rabbitmq_password": "short"},
        {"ui_password": ""},
        {"database_password": ""},
    ],
)
def test_bootstrap_rejects_unsafe_settings_without_secret_output(overrides, monkeypatch, capsys):
    """Неподходящая учётная запись или секрет не передаются скрипту даже частично."""
    settings = bootstrap_settings(**overrides)
    monkeypatch.setattr(operations, "Settings", lambda: settings)
    assert operations.broker_config() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    for secret in (PASSWORD, "private-secret", "я" * 513):
        assert secret not in output.err


@pytest.mark.parametrize("invalid", ["API_KEYS=not-valid-json", "RABBITMQ_CLI_TIMEOUT_SECONDS=no"])
def test_settings_parse_error_does_not_expose_dotenv(invalid, tmp_path, monkeypatch, capsys):
    """Ошибки JSON/type Pydantic не выводят значения секретов из dotenv."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text(
        invalid + "\nRABBITMQ_PASSWORD=private-sensitive-password\n", encoding="utf-8"
    )
    assert operations.broker_config() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-sensitive-password" not in output.err
    assert "Некорректные настройки" in output.err


def test_documented_env_initialization_and_repeat(tmp_path, monkeypatch):
    """Исполняет команды из документации: шесть независимых секретов и запрет перезаписи .env."""
    root = Path(__file__).resolve().parents[1]
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if bash is None:
        pytest.skip("Нужен Bash для проверки операторских команд")
    document = (root / "docs/operations.md").read_text(encoding="utf-8")
    command = next(
        block for block in re.findall(r"```bash\n(.*?)```", document, re.S) if "umask" in block
    )
    environment = dict(os.environ, MSYS2_ARG_CONV_EXCL="*")
    if subprocess.run([bash, "-c", "command -v openssl"], capture_output=True).returncode:
        pytest.skip("Команды первоначальной настройки требуют openssl")
    first = subprocess.run(
        [bash, "-c", command], cwd=tmp_path, env=environment, capture_output=True, timeout=15
    )
    assert first.returncode == 0, first.stderr.decode("utf-8", errors="replace")
    private = tmp_path / ".env"
    original = private.read_bytes()
    names = {line.split("=", 1)[0] for line in original.decode().splitlines()}
    assert names == {"DATABASE_PASSWORD", "RABBITMQ_PASSWORD", "UI_PASSWORD", "API_KEYS"}
    for name in names:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=(root / ".env.example", private))
    settings.require_secrets()
    values = [
        settings.database_password,
        settings.rabbitmq_password,
        settings.ui_password,
        *settings.api_keys.values(),
    ]
    assert len({value.get_secret_value() for value in values}) == 6
    assert set(settings.api_keys) == {"onec", "telegram", "evaluation"}
    if os.name != "nt":
        assert private.stat().st_mode & 0o777 == 0o600
    repeated = subprocess.run(
        [bash, "-c", command], cwd=tmp_path, env=environment, capture_output=True, timeout=15
    )
    assert repeated.returncode != 0
    assert private.read_bytes() == original
