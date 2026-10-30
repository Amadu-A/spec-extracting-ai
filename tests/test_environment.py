# tests/test_environment.py
"""Очистка .env: сохранность секретов, создание недостающих полей и отказ без потери данных."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic_settings import DotEnvSettingsSource

from app import operations
from app.core.settings import Settings
from app.infrastructure.environment import normalize_environment


@pytest.fixture
def environment_root(tmp_path):
    """Создаёт только тестовый каталог и открытый каталог настроек, без реальных секретов."""
    (tmp_path / ".env.example").write_text(
        "RABBITMQ_USER=specification\nRABBITMQ_VHOST=specification\n", encoding="utf-8"
    )
    return tmp_path


def secret_file(root, extra=""):
    """Имитирует показанный пользователем .env со старыми несекретными именами."""
    path = root / ".env"
    values = {
        "DATABASE_PASSWORD": "existing-database-password",
        "RABBITMQ_PASSWORD": "existing-rabbitmq-password",
        "UI_PASSWORD": "existing-interface-password",
        "API_KEYS": json.dumps({"onec": "existing-api-key-at-least-24-characters"}),
    }
    path.write_text(
        "RABBITMQ_USER=spec-extraction\nRABBITMQ_VHOST=spec-extraction\n"
        + "".join(f"{name}={json.dumps(value)}\n" for name, value in values.items())
        + extra,
        encoding="utf-8",
    )
    return path


def test_normalize_keeps_existing_values_and_backup(environment_root):
    """Убирает открытые поля и старый UI_PASSWORD, сохраняя рабочие секреты и точный backup."""
    path = secret_file(environment_root)
    before = path.read_bytes()
    stored = DotEnvSettingsSource(Settings, env_file=path)()
    old = Settings(
        _env_file=None,
        **{name: stored[name] for name in ("database_password", "rabbitmq_password", "api_keys")},
    )
    removed, generated, backup = normalize_environment(environment_root)
    new = Settings(_env_file=(environment_root / ".env.example", path))
    assert removed == ["RABBITMQ_USER", "RABBITMQ_VHOST", "UI_PASSWORD"] and not generated
    assert backup.read_bytes() == before
    for name in ("database_password", "rabbitmq_password", "api_keys"):
        assert getattr(new, name) == getattr(old, name)
    assert new.rabbitmq_user == new.rabbitmq_vhost == "specification"
    assert len(path.read_text().splitlines()) == 3
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    normalized = path.read_bytes()
    assert normalize_environment(environment_root) == ([], [], None)
    assert path.read_bytes() == normalized
    assert len(list(backup.parent.iterdir())) == 1


def test_normalize_generates_only_missing_values(environment_root):
    """Дополняет неполный файл, сохраняя имеющийся пароль; повтор ничего не генерирует."""
    path = environment_root / ".env"
    path.write_text("DATABASE_PASSWORD=existing-database-password\n", encoding="utf-8")
    _, generated, _ = normalize_environment(environment_root)
    assert generated == ["RABBITMQ_PASSWORD", "API_KEYS"]
    settings = Settings(_env_file=path)
    settings.require_secrets()
    assert settings.database_password.get_secret_value() == "existing-database-password"
    assert set(settings.api_keys) == {"onec", "telegram", "evaluation"}


def test_normalize_creates_absent_file(environment_root):
    """Создаёт корректный приватный .env, если файл ещё отсутствует."""
    _, generated, backup = normalize_environment(environment_root)
    assert len(generated) == 3 and backup is None
    Settings(_env_file=environment_root / ".env").require_secrets()


@pytest.mark.parametrize(
    "invalid",
    ["API_KEYS='invalid-json'", "DATABASE_PASSWORD=short", "DATABASE_PASSWORD='unterminated"],
)
def test_invalid_secret_is_not_rotated_or_disclosed(environment_root, invalid, monkeypatch, capsys):
    """Некорректный существующий секрет требует исправления и не подменяется случайным."""
    path = secret_file(environment_root, invalid + "\n")
    before = path.read_bytes()
    monkeypatch.chdir(environment_root)
    assert operations.normalize_env() == 2
    output = capsys.readouterr()
    assert "existing-" not in output.out + output.err
    assert path.read_bytes() == before
    assert not list(environment_root.glob(".env.*.tmp"))


def test_replacement_failure_keeps_original(environment_root, monkeypatch):
    """Отказ атомарной замены сохраняет исходник и приватную резервную копию."""
    path = secret_file(environment_root)
    before = path.read_bytes()

    def fail(*args):
        """Имитирует отказ файловой системы до изменения целевого файла."""
        raise OSError("test replacement failure")

    monkeypatch.setattr("app.infrastructure.environment.os.replace", fail)
    with pytest.raises(OSError):
        normalize_environment(environment_root)
    assert path.read_bytes() == before
    assert next((environment_root / "data/env-backups").iterdir()).read_bytes() == before
    assert not list(environment_root.glob(".env.*.tmp"))


def test_command_outputs_names_only(environment_root, monkeypatch, capsys):
    """Успешная команда показывает названия полей и путь backup без паролей и API-ключей."""
    secret_file(environment_root)
    monkeypatch.chdir(environment_root)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "app.operations", "normalize-env"],
        cwd=environment_root,
        env=dict(os.environ, PYTHONPATH=str(root), PYTHONIOENCODING="utf-8"),
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "RABBITMQ_USER" in result.stdout and "data" in result.stdout
    assert "existing-" not in result.stdout + result.stderr


def test_quotes_unicode_and_dollar_are_data(environment_root):
    """Сохраняет сложный пароль и не исполняет shell-фрагмент из постороннего поля."""
    path = secret_file(environment_root, "UNUSED=$(touch marker-created)\n")
    special = "сложный-'\\\"-$()-secret "
    literal = "DATABASE_PASSWORD='literal-$UNSET_VARIABLE-secret'"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("RABBITMQ_PASSWORD=" + json.dumps(special, ensure_ascii=False) + "\n")
        stream.write(literal + "\n")
    normalize_environment(environment_root)
    assert Settings(_env_file=path).rabbitmq_password.get_secret_value() == special
    assert literal in path.read_text(encoding="utf-8")
    assert not (environment_root / "marker-created").exists()
