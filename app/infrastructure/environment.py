# app/infrastructure/environment.py
"""Операторская очистка собственного .env с Pydantic, резервной копией и атомарной заменой."""

import json
import os
import secrets
import tempfile
from io import StringIO
from pathlib import Path
from uuid import uuid4

from dotenv.parser import parse_stream
from pydantic_settings import DotEnvSettingsSource

from app.core.settings import Settings

SECRET_NAMES = ("database_password", "rabbitmq_password", "ui_password", "api_keys")


def normalize_environment(root: Path) -> tuple[list[str], list[str], Path | None]:
    """Оставляет четыре секретных поля, сохраняя существующие значения и создавая недостающие.

    Считывает только .env этого проекта через источник Pydantic. Прежде чем заменить
    файл, проверяет настройки и точное чтение сериализованных секретов обратно.
    Старое содержимое сохраняется с правами 600 в игнорируемом data/env-backups.
    """
    root = root.resolve()
    private, catalog = root / ".env", root / ".env.example"
    if not catalog.is_file() or private.is_symlink():
        raise ValueError(
            "Запустите команду в корне проекта; .env не должен быть символической ссылкой"
        )
    original = private.read_bytes() if private.exists() else None
    bindings = list(parse_stream(StringIO(original.decode("utf-8")))) if original else []
    if any(binding.error for binding in bindings):
        raise ValueError("Неверный синтаксис .env; существующие секреты сохранены")
    preserved = {
        binding.key.lower(): binding.original.string.strip("\r\n") + "\n"
        for binding in bindings
        if binding.key is not None and binding.key.lower() in SECRET_NAMES
    }
    source = DotEnvSettingsSource(Settings, env_file=private, env_file_encoding="utf-8")
    stored = source()
    selected = {name: stored[name] for name in SECRET_NAMES if stored.get(name)}
    generated = []
    for name in SECRET_NAMES:
        if name not in selected:
            selected[name] = (
                {owner: secrets.token_hex(32) for owner in ("onec", "telegram", "evaluation")}
                if name == "api_keys"
                else secrets.token_hex(32)
            )
            generated.append(name.upper())
    settings = Settings(_env_file=catalog, **selected)
    settings.require_secrets()
    serialized = {}
    for name in SECRET_NAMES:
        value = getattr(settings, name)
        serialized[name.upper()] = (
            json.dumps(
                {owner: key.get_secret_value() for owner, key in value.items()},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if name == "api_keys"
            else value.get_secret_value()
        )
    # Сохраняем исходное quoting существующих значений: Compose и Pydantic
    # по-разному обрабатывают $VARIABLE и обратные слеши в новых кавычках.
    lines = []
    for name, value in serialized.items():
        if name in generated:
            lines.append(f"{name}={json.dumps(value, ensure_ascii=False)}\n")
        elif name.lower() in preserved:
            lines.append(preserved[name.lower()])
        else:
            raise ValueError("Храните API_KEYS единым JSON-полем; исходные секреты сохранены")
    content = "".join(lines).encode("utf-8")
    removed = sorted(name.upper() for name in stored if name not in SECRET_NAMES)
    if content == original:
        private.chmod(0o600)
        return removed, generated, None
    temporary = None
    backup = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=root, prefix=".env.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        decoded = DotEnvSettingsSource(Settings, env_file=temporary, env_file_encoding="utf-8")()
        checked = Settings(_env_file=None, **decoded)
        checked.require_secrets()
        if checked.model_dump(include=set(SECRET_NAMES)) != settings.model_dump(
            include=set(SECRET_NAMES)
        ):
            raise ValueError("Сериализация изменила секреты; исходный .env сохранён")
        if private.is_symlink() or (private.read_bytes() if private.exists() else None) != original:
            raise ValueError(".env изменён другим процессом; повторите проверку")
        if original is not None:
            directory = root / "data/env-backups"
            if not directory.resolve().is_relative_to(root):
                raise ValueError("Каталог резервных копий должен находиться внутри проекта")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = directory / ("env-" + uuid4().hex + ".bak")
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(original)
            backup.chmod(0o600)
        os.replace(temporary, private)
        temporary = None
        return removed, generated, backup
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
