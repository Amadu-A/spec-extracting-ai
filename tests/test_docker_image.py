# tests/test_docker_image.py
"""Настоящая сборка с закрытыми исходниками и проверка runtime под UID 10001."""

import json
import os
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    ".env.example",
    "alembic.ini",
    "app",
    "prompts",
    "migrations",
)


def restricted_context(path: Path, dockerfile: str | None = None) -> None:
    """Упаковывает реальный код с 600/700 независимо от прав Windows build context.

    Входной пакет app остаётся доступен, а operations.py закрыт: старая сборка
    воспроизводит именно PermissionError при импорте, как в серверном логе.
    """

    def restrict(member):
        """Имитирует umask 077; исключает байткод и не меняет права файлов хоста."""
        if "__pycache__" in Path(member.name).parts or member.name.endswith(".pyc"):
            return None
        member.uid = member.gid = 0
        member.uname = member.gname = "root"
        member.mode = 0o700 if member.isdir() else 0o600
        if member.name == "app":
            member.mode = 0o755
        if member.name == "app/__init__.py":
            member.mode = 0o644
        return member

    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in CONTEXT:
            if name == "Dockerfile" and dockerfile is not None:
                from io import BytesIO

                data = dockerfile.encode("utf-8")
                info = tarfile.TarInfo("Dockerfile")
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, BytesIO(data))
            else:
                archive.add(ROOT / name, arcname=name, filter=restrict)


@pytest.mark.docker
def test_real_image_reads_restricted_sources_as_non_root(request, tmp_path):
    """Собирает Dockerfile и исполняет import/config со штатным USER и ограничениями Compose."""
    if not request.config.getoption("--docker"):
        pytest.skip("Нужен --docker для настоящей сборки; в CI включён автоматически")
    identifier = uuid4().hex[:12]
    image, container = (
        f"spec-extracting-ai:permissions-{identifier}",
        f"spec-permissions-{identifier}",
    )
    archive = tmp_path / "context.tar.gz"
    restricted_context(archive)
    environment = dict(os.environ)
    for name in ("DATABASE_PASSWORD", "RABBITMQ_PASSWORD", "UI_PASSWORD"):
        environment[name] = "offline-image-test-password-only"
    environment["API_KEYS"] = json.dumps({"image-test": "offline-image-key-at-least-24-characters"})
    try:
        with archive.open("rb") as source:
            subprocess.run(
                ["docker", "build", "--tag", image, "-"], stdin=source, check=True, timeout=600
            )
        probe = """
import json,os
from pathlib import Path
assert os.getuid()==10001
roots=[Path('/app/app'),Path('/app/prompts'),Path('/app/migrations')]
count=0
for root in roots:
    for path in [root,*root.rglob('*')]:
        assert os.access(path,os.R_OK),str(path)
        if path.is_dir(): assert os.access(path,os.X_OK),str(path)
        else: path.read_bytes()
        assert not path.stat().st_mode & 0o022,str(path)
        count+=path.suffix=='.py'
import app.operations
print(json.dumps({'uid':os.getuid(),'python_modules':count,'operations_import':'OK'}))
"""
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:mode=1777",
        ]
        for name in ("DATABASE_PASSWORD", "RABBITMQ_PASSWORD", "UI_PASSWORD", "API_KEYS"):
            command += ["--env", name]
        result = subprocess.run(
            command + [image, "python", "-c", probe],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        details = json.loads(result.stdout)
        assert details["uid"] == 10001 and details["python_modules"] > 20
        print(result.stdout.strip())
        configured = subprocess.run(
            command + [image, "python", "-m", "app.operations", "config"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert configured.returncode == 0, configured.stderr
        assert "Settings/secrets: OK" in configured.stdout
        assert "PermissionError" not in configured.stderr
        print("python -m app.operations config (UID 10001): OK")
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        subprocess.run(["docker", "image", "rm", image], capture_output=True)
