# tests/test_script_artifacts.py
"""Настоящий Git и файловые записи: отчёты не меняют отслеживаемые файлы проекта.

HTTP и тяжёлые внешние команды подменяются явно. Статус Git и содержимое всех
отслеживаемых файлов проверяются в отдельной локальной копии без новых коммитов.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.core.settings import Settings
from scripts import evaluate as evaluation

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *arguments: str) -> bytes:
    """Выполняет локальный Git без изменения глобального safe.directory или настроек."""
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root.as_posix()}",
            "-c",
            f"safe.directory={(root / '.git').as_posix()}",
            "-C",
            str(root),
            *arguments,
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf8", errors="replace")
    return result.stdout


def checkout_tree(destination: Path) -> Path:
    """Создаёт приватный checkout HEAD без upload-pack, сети и новых коммитов.

    Исходные Git objects только читаются. Index, refs и config принадлежат
    временному репозиторию; пользовательский index не меняется.
    """
    destination.mkdir()
    git(destination, "init", "--quiet")
    objects = Path(git(ROOT, "rev-parse", "--git-path", "objects").decode("utf8").strip())
    if not objects.is_absolute():
        objects = ROOT / objects
    (destination / ".git/objects/info/alternates").write_text(
        objects.resolve().as_posix() + "\n", encoding="utf8", newline="\n"
    )
    revision = git(ROOT, "rev-parse", "HEAD").decode("ascii").strip()
    git(destination, "update-ref", "refs/heads/fixture", revision)
    git(destination, "symbolic-ref", "HEAD", "refs/heads/fixture")
    git(destination, "read-tree", "HEAD")
    git(destination, "checkout-index", "--all")
    return destination


def tracked_state(root: Path) -> tuple[bytes, dict[str, str]]:
    """Сверяет Git status и байты всех отслеживаемых файлов, включая уже изменённые."""
    paths = git(root, "ls-files", "-z").decode("utf8").split("\0")
    hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths if name
    }
    return git(root, "status", "--porcelain"), hashes


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """Изолирует записи утилиты от настоящего рабочего дерева и его Git index."""
    root = checkout_tree(tmp_path / "checkout")
    monkeypatch.setattr(evaluation, "PROJECT_ROOT", root)
    monkeypatch.chdir(root)
    assert git(root, "status", "--porcelain") == b""
    return root


@pytest.mark.parametrize("relative", ["reports/live", "reports/live/nested/run"])
def test_reports_leave_git_clean(checkout, relative):
    """JSON/Markdown создаются в live; статический отчёт и все отслеживаемые байты неизменны."""
    before = tracked_state(checkout)
    evaluation.write_report(Path(relative), [], "изолированная проверка записи")
    folder = checkout / relative
    assert json.loads((folder / "quality.json").read_text(encoding="utf8"))["datasets"] == []
    markdown = (folder / "quality.md").read_text(encoding="utf8")
    assert markdown.startswith(f"<!-- {relative}/quality.md -->")
    assert tracked_state(checkout) == before
    assert git(checkout, "check-ignore", str(folder / "quality.md"))


@pytest.mark.parametrize("relative", ["reports", ".", "reports/live/../../docs"])
async def test_outside_output_is_rejected_before_settings_or_api(checkout, monkeypatch, relative):
    """Ошибка --output не читает секреты, не вызывает API и не создаёт посторонние файлы."""

    def forbidden_settings():
        """Любое чтение Settings означает нарушение порядка предварительной проверки."""
        raise AssertionError("Settings вызван до проверки output")

    monkeypatch.setattr(evaluation, "Settings", forbidden_settings)
    before = tracked_state(checkout)
    with pytest.raises(ValueError, match="reports/live"):
        await evaluation.evaluate(argparse.Namespace(output=Path(relative)))
    assert tracked_state(checkout) == before


def test_directory_symlink_cannot_escape_live(checkout):
    """Ссылка из live на каталог tracked reports не позволяет перезаписать статический отчёт."""
    folder = checkout / "reports/live"
    folder.mkdir(parents=True)
    link = folder / "outside"
    try:
        link.symlink_to(checkout / "reports", target_is_directory=True)
    except OSError:
        pytest.skip("Среда Windows не разрешает создание symlink")
    before = tracked_state(checkout)
    with pytest.raises(ValueError, match="reports/live"):
        evaluation.write_report(link, [], "проверка ссылки")
    assert tracked_state(checkout) == before


def test_report_file_hardlink_does_not_change_tracked_target(checkout):
    """Атомарная замена отчёта сохраняет исходный tracked файл даже при случайной hardlink."""
    folder = checkout / "reports/live"
    folder.mkdir(parents=True)
    os.link(checkout / "reports/quality.md", folder / "quality.md")
    before = tracked_state(checkout)
    evaluation.write_report(folder, [], "проверка файловой ссылки")
    assert tracked_state(checkout) == before
    assert (folder / "quality.md").read_bytes() != (checkout / "reports/quality.md").read_bytes()


@pytest.mark.parametrize("relative", ["reports/live", "reports/live/evaluation"])
async def test_complete_evaluate_writes_only_ignored_reports(
    checkout, tmp_path, monkeypatch, relative
):
    """Четыре комплекта проходят настоящий evaluate с HTTP double и чистым Git после записи."""
    inputs = tmp_path / "inputs"
    await asyncio.to_thread(inputs.mkdir)
    manifest = {}
    for index in range(4):
        (inputs / f"dataset{index}.pdf").write_bytes(b"%PDF-evaluation-http-double")
        (inputs / f"dataset{index}.json").write_text('{"items":[]}', encoding="utf8")
        manifest[f"dataset{index}"] = {"project_code": f"P{index}"}
    manifest_path = inputs / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    settings = Settings(
        _env_file=None, api_keys={"evaluation": "test-evaluation-key-for-report-writes"}
    )
    monkeypatch.setattr(evaluation, "Settings", lambda: settings)
    calls = []
    current = -1

    def handle(request):
        """Эмулирует только API-сервер; парсинг, сравнение и запись evaluate остаются настоящими."""
        nonlocal current
        calls.append(request.url.path)
        if request.url.path.endswith("/documents"):
            current += 1
            payload = {"id": f"document-{current}", "page_count": 1}
        elif request.url.path.endswith("/snapshot"):
            payload = {"partial": {"items": []}}
        elif request.url.path.endswith("/warnings"):
            payload = {"warnings": [], "total": 0}
        else:
            payload = {"job_id": f"job-{current}", "status": "completed", "document_pages": 1}
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        evaluation.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    before = tracked_state(checkout)
    await evaluation.evaluate(
        argparse.Namespace(
            api_url="http://evaluation.invalid",
            owner="evaluation",
            examples=inputs,
            manifest=manifest_path,
            output=Path(relative),
            timeout=10,
        )
    )
    folder = checkout / relative
    report = json.loads((folder / "quality.json").read_text(encoding="utf8"))
    assert len(report["datasets"]) == 4
    assert len(list(folder.iterdir())) == 10
    assert len(calls) == 16
    assert tracked_state(checkout) == before


@pytest.mark.skipif(
    os.name == "nt", reason="Linux test.sh проверяется на Linux; Windows использует PowerShell gate"
)
@pytest.mark.parametrize("extra_js_test", [False, True], ids=["current-suite", "extended-suite"])
def test_linux_test_script_reports_leave_tracked_files_unchanged(checkout, tmp_path, extra_js_test):
    """Текущие JS-файлы и новая проверка не привязывают gate к числу тестов или старому HEAD."""
    bash_path = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(bash_path) if bash_path.exists() else shutil.which("bash")
    if bash is None:
        pytest.skip("Нужен Bash")
    for name in (
        "scripts/test.sh",
        "pyproject.toml",
        "package.json",
        "tests/frontend/state.test.js",
        "app/web/static/js/state.js",
    ):
        shutil.copyfile(ROOT / name, checkout / name)
    if extra_js_test:
        source = checkout / "tests/frontend/state.test.js"
        source.write_text(
            source.read_text(encoding="utf8")
            + "\ntest('Дополнительный JS-тест рабочей копии', () => "
            "assert.deepEqual(parsePages('1', 1), [1]));\n",
            encoding="utf8",
        )
    writer = tmp_path / "coverage_double.py"
    writer.write_text(
        "import pathlib,sys,tomllib\n"
        'cfg=tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf8"))\n'
        'paths=[cfg["tool"]["coverage"]["run"]["data_file"]]+[a.split("json:",1)[1] for a in sys.argv if a.startswith("--cov-report=json:")]\n'
        "for name in paths:\n"
        ' p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_text("{}",encoding="utf8")\n',
        encoding="utf8",
    )
    python = shlex.quote(Path(sys.executable).as_posix())
    launcher = python + " " + shlex.quote(writer.as_posix())
    hook = tmp_path / "uv_double.sh"
    hook.write_text(
        "uv() { if [[ ${1-} == run && ${3-} == python ]]; then shift 3; "
        + python
        + ' "$@"; elif [[ ${1-} == run && ${3-} == pytest ]]; then '
        + launcher
        + ' "$@"; else return 0; fi; }\n',
        encoding="utf8",
        newline="\n",
    )
    before = tracked_state(checkout)
    completed = subprocess.run(
        [bash, (checkout / "scripts/test.sh").as_posix()],
        cwd=checkout,
        env=dict(os.environ, BASH_ENV=hook.as_posix(), MSYS2_ARG_CONV_EXCL="*"),
        capture_output=True,
        encoding="utf8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    count = re.search(r"\btests ([1-9]\d*)\b", completed.stdout)
    assert count and re.search(rf"\bpass {count.group(1)}\b", completed.stdout)
    if extra_js_test:
        assert "Дополнительный JS-тест рабочей копии" in completed.stdout
    assert (checkout / "reports/live/.coverage").is_file()
    assert (checkout / "reports/live/coverage.json").is_file()
    assert tracked_state(checkout) == before
