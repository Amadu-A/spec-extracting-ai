# scripts/evaluate.py
"""Реальный headless API-прогон четырёх комплектов и матрица сравнения с baseline.

Нужен app API-key текущего owner в Settings.API_KEYS. Результаты и названия товаров
сохраняются в ignored reports/live; открытый отчёт содержит только агрегаты.
"""

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter

import httpx

from app.core.settings import Settings
from app.domain.models import Export
from app.domain.quality import compare_exports


def find_pairs(directory: Path) -> list[tuple[Path, Path]]:
    """Распознаёт оба именования spec_PDF/plain_JSON и plain_PDF/spec_JSON."""
    pairs = []
    for pdf in sorted(directory.glob("*.pdf")):
        base = pdf.stem.removeprefix("spec_")
        baseline = next(
            (
                p
                for p in (directory / (base + ".json"), directory / ("spec_" + base + ".json"))
                if p.exists()
            ),
            None,
        )
        if baseline:
            pairs.append((pdf, baseline))
    return pairs


def write_report(directory: Path, results: list[dict], mode: str) -> None:
    """Сохраняет все field/page differences и компактную проверяемую Markdown-матрицу."""
    directory.mkdir(parents=True, exist_ok=True)
    for row in results:
        if row.get("document_pages"):
            known = {page["pdf_page"]: page for page in row["pages"]}
            row["pages"] = [
                known.get(number, {"pdf_page": number, "baseline": 0, "actual": 0, "exact": 0})
                for number in range(1, row["document_pages"] + 1)
            ]
    (directory / "quality.json").write_text(
        json.dumps({"mode": mode, "datasets": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "<!-- reports/quality.md -->",
        "# Фактическое сравнение с baseline",
        "",
        f"Режим: {mode}. Baseline не является вручную проверенным golden. Exact F1 измеряет полное совпадение пяти полей, а не абсолютную полноту распознавания.",
        "",
        "| Комплект | Baseline | Получено | Точных | Exact F1 | Изменено | Пропущено | Добавлено | Время, с | Состояние |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in results:
        lines.append(
            f"| {row['name']} | {row['baseline_items']} | {row['actual_items']} | {row['exact_matches']} | {row['exact_f1']:.2%} | {len(row['changed'])} | {len(row['missing'])} | {len(row['added'])} | {row.get('duration_seconds', 0):.1f} | {row.get('status', 'не указан')} |"
        )
    lines.extend(
        [
            "",
            "Подробные различия по пяти полям и PDF-страницам: `reports/live/quality.json` (локальный файл, исключён из Git).",
            "",
            "Изменённые строки — кандидаты для ручной сверки, не автоматически исправленные данные. Различия в Caption могут быть полезными уточнениями исходного PDF. Проверка JSON-схемы не доказывает отсутствие пропусков.",
        ]
    )
    for row in results:
        lines.extend(
            [
                "",
                f"## {row['name']}: физические PDF-страницы",
                "",
                "| PDF-лист | Baseline | Получено | Точных совпадений |",
                "|---:|---:|---:|---:|",
            ]
        )
        for page in row["pages"]:
            lines.append(
                f"| {page['pdf_page']} | {page['baseline']} | {page['actual']} | {page['exact']} |"
            )
    Path("reports").mkdir(exist_ok=True)
    Path("reports/quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def evaluate(arguments) -> None:
    """Использует тот же REST API, что 1С/Telegram; браузер не нужен."""
    settings = Settings()
    if arguments.owner not in settings.api_keys:
        raise ValueError("В API_KEYS .env отсутствует указанный owner")
    pairs = find_pairs(arguments.examples)
    if len(pairs) != 4:
        raise ValueError(f"Ожидаются все четыре комплекта PDF/JSON, найдено {len(pairs)}")
    arguments.output.mkdir(parents=True, exist_ok=True)
    reports = []
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    headers = {"X-API-Key": settings.api_keys[arguments.owner].get_secret_value()}
    async with httpx.AsyncClient(
        base_url=arguments.api_url.rstrip("/"), headers=headers, timeout=120, trust_env=False
    ) as client:
        for pdf, baseline in pairs:
            expected = Export.model_validate_json(baseline.read_text(encoding="utf-8-sig"))
            dataset = pdf.stem.removeprefix("spec_")
            if dataset not in manifest:
                raise ValueError(f"В manifest отсутствует подтверждённый project_code: {dataset}")
            project = manifest[dataset]["project_code"]
            started = perf_counter()
            with pdf.open("rb") as source:
                response = await client.post(
                    "/api/v1/documents", files={"file": (pdf.name, source, "application/pdf")}
                )
            response.raise_for_status()
            document = response.json()
            # Все страницы, в том числе нешаблонные листы УУТЭ: baseline не управляет inference.
            response = await client.post(
                "/api/v1/extractions",
                data={
                    "document_id": document["id"],
                    "pages": f"1-{document['page_count']}",
                    "project_code": project,
                },
            )
            response.raise_for_status()
            status = response.json()
            deadline = asyncio.get_running_loop().time() + arguments.timeout
            while status["status"] in ("queued", "running"):
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError(f"Задание {status['job_id']} не завершилось за timeout")
                print(
                    f"{project}: {status['status']}, {status['processed_pages']}/{status['total_pages']} страниц",
                    flush=True,
                )
                await asyncio.sleep(5)
                response = await client.get(f"/api/v1/extractions/{status['job_id']}")
                response.raise_for_status()
                status = response.json()
            response = await client.get(f"/api/v1/extractions/{status['job_id']}/snapshot")
            response.raise_for_status()
            actual = Export.model_validate(response.json()["partial"])
            (arguments.output / (dataset + ".json")).write_text(
                actual.model_dump_json(indent=2), encoding="utf-8"
            )
            warnings, offset = [], 0
            while True:
                response = await client.get(
                    f"/api/v1/extractions/{status['job_id']}/warnings",
                    params={"offset": offset, "limit": 200},
                )
                response.raise_for_status()
                batch = response.json()
                warnings.extend(batch["warnings"])
                offset += len(batch["warnings"])
                if offset >= batch["total"]:
                    break
            (arguments.output / (dataset + ".diagnostics.json")).write_text(
                json.dumps({"status": status, "warnings": warnings}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            reports.append(
                {
                    "name": dataset,
                    "status": status["status"],
                    "warning_count": len(warnings),
                    "document_pages": status["document_pages"],
                    "duration_seconds": perf_counter() - started,
                    **compare_exports(expected, actual),
                }
            )
            write_report(
                arguments.output, reports, "реальный REST API → RabbitMQ → worker → shared-vlm"
            )


def main() -> None:
    """Разбирает только несекретные параметры, credentials берёт через Settings."""
    parser = argparse.ArgumentParser(description="Реальная оценка качества на четырёх PDF/JSON")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--owner", default="evaluation")
    parser.add_argument("--examples", type=Path, default=Path("examples"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("tests/fixtures/corpus-manifest.json")
    )
    parser.add_argument("--output", type=Path, default=Path("reports/live"))
    parser.add_argument("--timeout", type=int, default=7200)
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    main()
