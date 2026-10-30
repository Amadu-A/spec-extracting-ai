# app/application/processor.py
"""Отказоустойчивый worker use-case с lease, checkpoints и реальным JSON-stream.

Сетевые вызовы не держат DB-транзакцию. Повторная доставка RabbitMQ не запускает
вторую модель для действующего lease; старый worker не может сохранить результат.
"""

import asyncio
import contextlib
import logging
from datetime import timedelta
from time import perf_counter
from uuid import uuid4

from app.application.ports import FileStorage, JobRepository, ModelClient, PdfReader
from app.core.observability import timed
from app.core.settings import Settings
from app.domain.errors import LeaseLost, ServiceError
from app.domain.models import TERMINAL, Carryover, Job, JobState, Stage, Warning, utcnow
from app.domain.reconcile import parse_model_response, reconcile


class JobCancelled(Exception):
    """Внутренний сигнал немедленной остановки без ложного job_completed."""


class ExtractionProcessor:
    """Обрабатывает задания независимо от браузера и конкретного брокера."""

    def __init__(
        self,
        repository: JobRepository,
        storage: FileStorage,
        pdf: PdfReader,
        model: ModelClient,
        settings: Settings,
    ):
        """Все внешние зависимости заменяются тестами без изменения алгоритмов."""
        self.repository, self.storage, self.pdf, self.model = repository, storage, pdf, model
        self.settings = settings

    @timed("process_extraction")
    async def execute(self, job_id: str) -> None:
        """Захватывает lease и возобновляет работу с последнего committed листа."""
        lease = str(uuid4())

        def claim(job: Job) -> None:
            """Атомарно защищает job от конкурирующих/повторных deliveries."""
            if job.state in TERMINAL or job.expires_at <= utcnow() + timedelta(
                seconds=self.settings.retention_seconds
            ):
                raise LeaseLost
            if job.state == JobState.RUNNING and job.lease_until and job.lease_until > utcnow():
                raise LeaseLost
            job.transition(JobState.RUNNING)
            job.lease_token, job.lease_until = (
                lease,
                utcnow() + timedelta(seconds=self.settings.lease_seconds),
            )
            job.emit("job_started", recovered=job.current_pdf_page is not None)

        try:
            claimed = await self.repository.edit(job_id, claim)
        except (LeaseLost, ServiceError):
            return
        work = asyncio.create_task(self._process(job_id, lease))
        heartbeat = asyncio.create_task(self._heartbeat(job_id, lease))
        budget = (claimed.expires_at - utcnow()).total_seconds() - self.settings.retention_seconds
        deadline = asyncio.timeout(max(0, budget))
        try:
            async with deadline:
                await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
                if work.done():
                    await work
                else:
                    await heartbeat
                    raise LeaseLost
        except JobCancelled:
            await self._finish(job_id, lease, JobState.CANCELLED)
        except LeaseLost:
            pass
        except ServiceError as exc:
            await self._finish(job_id, lease, JobState.FAILED, exc.code, exc.message)
        except TimeoutError:
            await self._finish(
                job_id,
                lease,
                JobState.FAILED,
                "job_timeout" if deadline.expired() else "vlm_timeout",
                "Превышено время обработки задания"
                if deadline.expired()
                else "Превышено полное время обработки страницы",
            )
        except asyncio.CancelledError:
            # При остановке процесса lease истечёт, а AMQP delivery вернётся в очередь.
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "worker_unexpected_error", extra={"job_id": job_id}
            )
            await self._finish(
                job_id,
                lease,
                JobState.FAILED,
                "internal_error",
                "Внутренняя ошибка; обратитесь к журналу по job_id",
            )
        finally:
            for task in (work, heartbeat):
                if not task.done():
                    task.cancel()
            for task in (work, heartbeat):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def _guard(self, job: Job, lease: str, check_cancel: bool = True) -> None:
        """Fencing token запрещает изменения старым worker после восстановления."""
        if job.lease_token != lease or job.state != JobState.RUNNING:
            raise LeaseLost
        if check_cancel and job.cancel_requested:
            raise JobCancelled

    async def _edit(self, job_id: str, lease: str, change) -> Job:
        """Оборачивает чистые page/checkpoint мутации проверкой fencing token."""

        def guarded(job: Job) -> None:
            """Проверяет актуальные cancel и owner lease внутри row lock."""
            self._guard(job, lease)
            change(job)

        return await self.repository.edit(job_id, guarded)

    async def _heartbeat(self, job_id: str, lease: str) -> None:
        """Продлевает lease и замечает отмену даже при зависшем ответе VLM."""
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)

            def refresh(job: Job) -> None:
                """Не позволяет истёкшему worker перезаписать lease нового процесса."""
                self._guard(job, lease)
                job.lease_until = utcnow() + timedelta(seconds=self.settings.lease_seconds)

            await self.repository.edit(job_id, refresh)

    async def _stage(self, job_id: str, lease: str, index: int, name: str, action):
        """Фиксирует фактическое начало и duration операции в восстанавливаемом журнале."""

        def begin(job: Job) -> None:
            """Сохраняет начало до выполнения внешнего действия."""
            job.pages[index].stages.append(Stage(name=name))
            job.emit("stage_started", pdf_page=job.pages[index].pdf_page, stage=name)

        job = await self._edit(job_id, lease, begin)
        stage_index = len(job.pages[index].stages) - 1
        started, state = perf_counter(), "failed"
        try:
            value = await action()
            state = "completed"
            return value
        finally:
            duration = (perf_counter() - started) * 1000

            def end(current: Job) -> None:
                """Сохраняет результат этапа, только пока lease принадлежит этому worker."""
                stage = current.pages[index].stages[stage_index]
                stage.state, stage.duration_ms = state, round(duration, 2)
                current.emit(
                    "stage_completed",
                    pdf_page=current.pages[index].pdf_page,
                    stage=name,
                    state=state,
                    duration_ms=stage.duration_ms,
                )

            with contextlib.suppress(LeaseLost, JobCancelled):
                await self._edit(job_id, lease, end)

    async def _process(self, job_id: str, lease: str) -> None:
        """Пропускает committed страницы; незавершённый лист повторяет целиком."""
        job = await self.repository.get(job_id)
        previous: Carryover | None = None
        for index, page in enumerate(job.pages):
            if page.state in ("completed", "completed_with_warnings"):
                previous = page.carryover
                continue
            previous = await self._process_page(job_id, lease, index, previous)
        job = await self.repository.get(job_id)
        state = JobState.WARNINGS if any(p.warnings for p in job.pages) else JobState.COMPLETED
        await self._finish(job_id, lease, state)

    async def _process_page(
        self, job_id: str, lease: str, index: int, previous: Carryover | None
    ) -> Carryover:
        """????????? ???? ????, ???????? ??????? ? provenance ????? ???????????."""
        job = await self.repository.get(job_id)
        page = job.pages[index]

        def begin(current: Job) -> None:
            """Очищает незавершённую попытку, сохраняя уже подтверждённые соседние листы."""
            current.pages[index].state = "running"
            current.pages[index].raw_text = ""
            # Старый raw остаётся в durable SSE journal, snapshot содержит текущий лист.
            for completed_page in current.pages[:index]:
                completed_page.raw_text = ""
            for stage in current.pages[index].stages:
                if stage.state == "running":
                    stage.state = "interrupted"

        await self._edit(job_id, lease, begin)
        prepared = await self._stage(
            job_id,
            lease,
            index,
            "Извлечение текста и рендер",
            lambda: self.pdf.prepare(self.storage.path(job.document_id), page.pdf_page),
        )

        def native_timings(current: Job) -> None:
            """Сохраняет отдельно измеренные child-process этапы, не вычисляя фиктивные доли."""
            current.pages[index].stages.extend(prepared.stages)
            for stage in prepared.stages:
                current.emit(
                    "stage_completed",
                    pdf_page=page.pdf_page,
                    stage=stage.name,
                    state=stage.state,
                    duration_ms=stage.duration_ms,
                )

        await self._edit(job_id, lease, native_timings)
        context = {
            "pdf_page": page.pdf_page,
            "project_code": job.project_code,
            "previous_adjacent_page": previous.model_dump(mode="json")
            if previous and previous.pdf_page == page.pdf_page - 1
            else None,
            "render_diagnostics": prepared.warnings,
        }
        result = None
        for attempt in range(1, self.settings.vlm_attempts + 1):
            try:

                def started(current: Job, attempt=attempt) -> None:
                    """page_started соответствует реальному запросу VLM, а не рендеру."""
                    current.current_pdf_page = page.pdf_page
                    current.pages[index].attempts += 1
                    current.pages[index].raw_text = ""
                    current.emit("page_started", pdf_page=page.pdf_page, attempt=attempt)
                    current.emit("stream_reset", pdf_page=page.pdf_page)

                await self._edit(job_id, lease, started)
                raw = await self._stage(
                    job_id,
                    lease,
                    index,
                    "Запрос shared-vlm",
                    lambda: self._stream(job_id, lease, index, prepared, job.prompt, context),
                )

                async def validate(raw=raw):
                    """Полная схема проверяется только после завершения model stream."""
                    return parse_model_response(raw)

                result = await self._stage(job_id, lease, index, "Валидация JSON", validate)
                break
            except ServiceError as exc:
                if attempt == self.settings.vlm_attempts or exc.code == "vlm_contract_unsupported":
                    raise

                def retrying(current: Job, attempt=attempt, code=exc.code) -> None:
                    """Журнал retry не выдаёт невалидные фрагменты за подтверждённые позиции."""
                    current.emit(
                        "retry_scheduled", pdf_page=page.pdf_page, attempt=attempt, error_code=code
                    )

                await self._edit(job_id, lease, retrying)
                context["previous_error"] = exc.code
                await asyncio.sleep(min(2**attempt, 15))

        async def merge():
            """Чистые правила переносов исполняются тем же путём для всех клиентов."""
            return reconcile(page.pdf_page, job.project_code, result, previous)

        items, warnings, carry = await self._stage(
            job_id, lease, index, "Согласование строк", merge
        )
        is_last = index == len(job.pages) - 1
        next_adjacent = not is_last and job.pages[index + 1].pdf_page == page.pdf_page + 1
        if carry.pending and not next_adjacent:
            warnings.append(
                Warning(
                    pdf_page=page.pdf_page,
                    code="unclosed_carryover",
                    message="Незавершённая позиция: нет следующего выбранного соседнего листа",
                    row=carry.pending,
                )
            )
            carry.pending = None

        def commit(current: Job) -> None:
            """Строки, provenance, carryover и события фиксируются одной транзакцией."""
            target = current.pages[index]
            if sum(len(p.items) for p in current.pages) + len(items) > self.settings.max_items:
                raise ServiceError("item_limit", "Достигнут безопасный предел позиций задания")
            if current.event_seq + len(items) + len(warnings) + 1 > self.settings.max_events - 50:
                raise ServiceError("event_limit", "Достигнут безопасный предел журнала задания")
            target.items, target.warnings, target.carryover = items, warnings, carry
            target.state = "completed_with_warnings" if warnings else "completed"
            for evidence in items:
                current.emit(
                    "item_validated",
                    pdf_page=page.pdf_page,
                    item=evidence.item.model_dump(),
                    source_pages=evidence.source_pages,
                )
            for warning in warnings:
                current.emit("warning", **warning.model_dump(mode="json"))
            current.emit(
                "page_completed",
                pdf_page=page.pdf_page,
                item_count=len(items),
                warning_count=len(warnings),
            )

        job = await self._edit(job_id, lease, commit)
        return carry

    async def _stream(
        self, job_id: str, lease: str, index: int, page, prompt: str, context: dict
    ) -> str:
        """Буферизует небольшие delta-блоки, ограничивая память и рост SSE-журнала."""
        chunks, buffered, total, last_flush = [], "", 0, perf_counter()

        async def flush() -> None:
            """Сохраняет raw stream отдельно от подтверждённых экспортных объектов."""
            nonlocal buffered, last_flush
            if not buffered:
                return
            delta = buffered

            def append(job: Job) -> None:
                """Проверяет лимит журнала до записи, оставляя место для terminal event."""
                if job.event_seq >= self.settings.max_events - 500:
                    raise ServiceError("event_limit", "Достигнут безопасный предел журнала задания")
                job.pages[index].raw_text += delta
                job.emit("text_delta", pdf_page=job.pages[index].pdf_page, text=delta)

            await self._edit(job_id, lease, append)
            buffered, last_flush = "", perf_counter()

        async with asyncio.timeout(self.settings.vlm_timeout_seconds):
            async for delta in self.model.stream(page, prompt, context):
                total += len(delta)
                if total > self.settings.max_stream_chars:
                    raise ServiceError("stream_limit", "Ответ модели превысил безопасный лимит")
                chunks.append(delta)
                buffered += delta
                if len(buffered) >= 1024 or perf_counter() - last_flush >= 0.3:
                    await flush()
            await flush()
        return "".join(chunks)

    async def _finish(
        self,
        job_id: str,
        lease: str,
        state: JobState,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        """Сохраняет terminal state; отмена имеет приоритет перед завершением/ошибкой."""

        def finish(job: Job) -> None:
            """Не выдаёт cancellation за успешный экспорт и не перезаписывает чужой lease."""
            self._guard(job, lease, check_cancel=False)
            target = JobState.CANCELLED if job.cancel_requested else state
            if target == JobState.FAILED:
                running = next((page for page in job.pages if page.state == "running"), None)
                if running:
                    running.state = "failed"
                    warning = Warning(
                        pdf_page=running.pdf_page,
                        code=code or "internal_error",
                        message=message or "Ошибка обработки",
                    )
                    running.warnings.append(warning)
                    job.emit("warning", **warning.model_dump(mode="json"))
            if target == JobState.CANCELLED:
                for page in job.pages:
                    if page.state == "running":
                        page.state = "failed"
                        page.warnings.append(
                            Warning(
                                pdf_page=page.pdf_page,
                                code="page_cancelled",
                                message="Обработка страницы отменена до подтверждения",
                            )
                        )
                    for stage in page.stages:
                        if stage.state == "running":
                            stage.state = "interrupted"
            job.transition(target)
            job.expires_at = utcnow() + timedelta(seconds=self.settings.retention_seconds)
            job.error_code = code if target == JobState.FAILED else None
            job.lease_token, job.lease_until = None, None
            event = (
                "job_completed"
                if target in (JobState.COMPLETED, JobState.WARNINGS)
                else "job_" + target.value
            )
            job.emit(event, status=target.value, error_code=job.error_code)

        with contextlib.suppress(LeaseLost):
            await self.repository.edit(job_id, finish)
