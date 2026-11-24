# app/infrastructure/database.py
"""SQLAlchemy-хранилище: PostgreSQL в production, SQLite только в offline-тестах.

Снимок, checkpoint страницы и SSE-события изменяются одной транзакцией.
DB I/O вынесен из event loop. Блокировка строки защищает от старого worker.
"""

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    and_,
    create_engine,
    delete,
    event,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import Session as DbSession

from app.domain.delivery import LEGACY_DELIVERY, DeliveryClaim
from app.domain.errors import LeaseLost, ServiceError
from app.domain.jobs import document_ids, read_job
from app.domain.models import Document, Event, Job, Session, utcnow


class Base(DeclarativeBase):
    """Метаданные принадлежат только этому приложению и его миграциям."""


class JobRecord(Base):
    """Индексируемые поля очереди плюс версионируемый JSON-снимок агрегата."""

    __tablename__ = "extraction_jobs"
    __table_args__ = (UniqueConstraint("owner", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(40), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_revision: Mapped[int | None] = mapped_column(Integer)
    delivery_state: Mapped[str | None] = mapped_column(String(16), index=True)
    delivery_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_claim: Mapped[str | None] = mapped_column(String(36))
    delivery_attempts: Mapped[int | None] = mapped_column(Integer)
    delivery_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventRecord(Base):
    """Монотонный журнал с каскадным удалением по TTL задания."""

    __tablename__ = "extraction_events"
    job_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class DocumentRecord(Base):
    """Срок PDF продлевается при создании использующего его задания."""

    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class SessionRecord(Base):
    """Cookie в открытом виде не сохраняется, prompt изолирован hash-ключом."""

    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class SqlRepository:
    """Репозиторий и транзакционная граница для агрегата и журнала событий."""

    def __init__(self, url: str):
        """Создаёт пул; production-таблицы создаются исключительно Alembic."""
        options = (
            {"connect_args": {"check_same_thread": False}}
            if url.startswith("sqlite")
            else {"pool_pre_ping": True}
        )
        self.engine = create_engine(url, **options)
        self._lock = threading.RLock()
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, record):
                """Offline SQLite проверяет те же parent/event FK, что production PostgreSQL."""
                connection.execute("PRAGMA foreign_keys=ON")

    async def _run(self, operation: Callable):
        """Выносит синхронный driver в поток; SQLite сериализуется для тестов."""

        def execute():
            """Ограничивает SQLite; PostgreSQL использует row-level locks."""
            if self.engine.dialect.name == "sqlite":
                with self._lock:
                    return operation()
            return operation()

        return await asyncio.to_thread(execute)

    async def create(self, job: Job, capacity: int) -> Job:
        """Уникальность owner/key и fingerprint защищает от повторного запуска."""

        def insert():
            """Сериализует admission PostgreSQL advisory lock без глобального mutex."""
            with DbSession(self.engine) as db, db.begin():
                if self.engine.dialect.name == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(735419023)"))
                if job.idempotency_key:
                    previous = db.scalar(
                        select(JobRecord).where(
                            JobRecord.owner == job.owner,
                            JobRecord.idempotency_key == job.idempotency_key,
                        )
                    )
                    if previous:
                        old = self._read_job(previous)
                        if old.fingerprint != job.fingerprint:
                            raise ServiceError(
                                "idempotency_conflict",
                                "Ключ уже использован с другими данными",
                                409,
                            )
                        return old
                self._check_capacity(db, capacity)
                record = JobRecord(id=job.id, owner=job.owner, idempotency_key=job.idempotency_key)
                self._store(db, record, job)
                return job

        return await self._run(insert)

    @staticmethod
    def _check_capacity(db: DbSession, capacity: int) -> None:
        """Применяет общий лимит создания и ручного повтора под admission lock."""
        count = db.scalar(
            select(func.count())
            .select_from(JobRecord)
            .where(JobRecord.state.in_(("queued", "running")))
        )
        if count >= capacity:
            raise ServiceError("queue_full", "Очередь заполнена, повторите позже", 429)

    def _store(self, db: DbSession, record: JobRecord, job: Job) -> None:
        """Сохраняет события и агрегат вместе; не вызывается из HTTP/application."""
        job.updated_at = utcnow()
        payload = job.model_dump(mode="json")
        expiry_changed = (record.payload or {}).get("expires_at") != payload["expires_at"]
        if record.payload is None or record.payload.get("revision", 0) != job.revision:
            record.delivery_revision = job.revision
            self._reset_delivery(record, job.updated_at)
            record.started_at = job.started_at = None
        if job.state.value == "running" and job.lease_token != (record.payload or {}).get(
            "lease_token"
        ):
            # При захвате работа уже дошла до consumer, даже если publisher потерял confirm.
            record.delivery_state, record.delivery_due, record.delivery_claim = (
                "received",
                None,
                None,
            )
            record.started_at = record.started_at or job.updated_at
            job.started_at = record.started_at
        if job.state.value not in ("queued", "running"):
            record.delivery_state, record.delivery_due, record.delivery_claim = "closed", None, None
        record.state, record.lease_until, record.expires_at = (
            job.state.value,
            job.lease_until,
            job.expires_at,
        )
        record.payload = payload
        db.add(record)
        # Между ORM-моделями нет relationship: явно создаём parent до event insert.
        # flush не делает commit, snapshot и журнал по-прежнему атомарны.
        db.flush()
        if expiry_changed:
            self._extend_documents(db, job)
        for journal_event in job.pending_events:
            db.add(
                EventRecord(
                    job_id=job.id,
                    id=journal_event.id,
                    payload=journal_event.model_dump(mode="json"),
                )
            )
        job.pending_events.clear()

    @staticmethod
    def _read_job(row: JobRecord) -> Job:
        """Добавляет время первого захвата ревизии без изменения сохранённого JSON."""
        job = read_job(row.payload)
        if row.started_at is not None:
            job.started_at = row.started_at.replace(tzinfo=UTC)
        return job

    @staticmethod
    def _reset_delivery(row: JobRecord, now: datetime) -> None:
        """Создаёт запрос публикации, откладывая сеть до завершения транзакции."""
        row.delivery_state, row.delivery_due, row.delivery_claim = "pending", now, None
        row.delivery_attempts, row.delivery_confirmed_at = 0, None

    def _extend_documents(self, db: DbSession, job: Job) -> None:
        """Связывает TTL всех файлов batch с живыми jobs в той же транзакции."""
        # В snapshots новых jobs несколько источников; SQL-схема остаётся совместимой.
        for document_id in sorted(document_ids(job)):
            document_row = db.scalar(
                select(DocumentRecord).where(DocumentRecord.id == document_id).with_for_update()
            )
            if document_row:
                # Читаем сроки после document lock: concurrent create уже мог продлить оригинал.
                active = db.execute(
                    select(
                        JobRecord.expires_at,
                        JobRecord.payload["document_id"].as_string(),
                        JobRecord.payload["sources"],
                    ).where(JobRecord.expires_at > utcnow())
                )
                latest = max(
                    [
                        job.expires_at,
                        *(
                            expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry
                            for expiry, first_id, sources in active
                            if document_id
                            in ([s["document_id"] for s in sources] if sources else [first_id])
                        ),
                    ]
                )
                document = Document.model_validate(document_row.payload)
                document.expires_at = latest
                document_row.expires_at, document_row.payload = (
                    latest,
                    document.model_dump(mode="json"),
                )

    async def get(self, job_id: str) -> Job | None:
        """Читает новый снимок, исключая ORM из вызывающего слоя."""

        def read():
            """Закрывает DB session после материализации DTO."""
            with DbSession(self.engine) as db:
                row = db.get(JobRecord, job_id)
                return self._read_job(row) if row else None

        return await self._run(read)

    async def edit(
        self,
        job_id: str,
        change: Callable[[Job], None],
        *,
        delivery_revision: int | None = None,
        capacity: int | None = None,
    ) -> Job:
        """Держит FOR UPDATE только во время чистой мутации, никогда во время VLM."""

        def update():
            """Читает свежий агрегат, применяет правило и атомарно фиксирует журнал."""
            with DbSession(self.engine) as db, db.begin():
                if capacity is not None and self.engine.dialect.name == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(735419023)"))
                row = db.scalar(select(JobRecord).where(JobRecord.id == job_id).with_for_update())
                if not row:
                    raise ServiceError("job_not_found", "Задание не найдено", 404)
                job = self._read_job(row)
                if delivery_revision is not None and (
                    (delivery_revision == LEGACY_DELIVERY and row.delivery_revision is not None)
                    or (delivery_revision != LEGACY_DELIVERY and delivery_revision != job.revision)
                ):
                    raise LeaseLost
                change(job)
                if (
                    capacity is not None
                    and row.state not in ("queued", "running")
                    and job.state.value == "queued"
                ):
                    self._check_capacity(db, capacity)
                self._store(db, row, job)
                return job

        return await self._run(update)

    async def events(self, job_id: str, after: int, limit: int) -> list[Event]:
        """Возвращает страницу replay, не загружая весь поток в память."""

        def read():
            """Использует составной primary key job/id для последовательного чтения."""
            with DbSession(self.engine) as db:
                rows = db.scalars(
                    select(EventRecord)
                    .where(EventRecord.job_id == job_id, EventRecord.id > after)
                    .order_by(EventRecord.id)
                    .limit(limit)
                )
                return [Event.model_validate(row.payload) for row in rows]

        return await self._run(read)

    async def claim_delivery(self, retention: int) -> DeliveryClaim | None:
        """Резервирует одну запись. Подтверждённое ожидание не зависит от возраста job."""

        def claim():
            """Блокирует только выбранную строку, исключая штатную двойную публикацию."""
            now = utcnow()
            with DbSession(self.engine) as db, db.begin():
                row = db.scalar(
                    select(JobRecord)
                    .where(
                        JobRecord.expires_at > now + timedelta(seconds=retention),
                        JobRecord.payload["cancel_requested"].as_boolean().is_not(True),
                        (
                            JobRecord.state.in_(("queued", "running"))
                            & JobRecord.delivery_state.in_(("pending", "claimed"))
                            & (JobRecord.delivery_due <= now)
                        )
                        | (
                            (JobRecord.state == "running")
                            & (JobRecord.lease_until <= now)
                            & (
                                (JobRecord.delivery_state == "received")
                                | JobRecord.delivery_state.is_(None)
                            )
                        ),
                    )
                    .order_by(
                        func.coalesce(JobRecord.delivery_due, JobRecord.lease_until), JobRecord.id
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                row.delivery_attempts = (row.delivery_attempts or 0) + 1
                row.delivery_state, row.delivery_claim = "claimed", str(uuid4())
                row.delivery_due = now + timedelta(seconds=30)
                return DeliveryClaim(
                    row.id,
                    row.payload.get("revision", 0),
                    row.delivery_claim,
                    row.delivery_attempts,
                    row.state == "running",
                )

        return await self._run(claim)

    async def finish_delivery(self, claim: DeliveryClaim, retry_delay: float | None) -> bool:
        """Сохраняет confirm или срок повтора только для действующего резерва."""

        def finish():
            """Устаревший publisher не может изменить новое поколение или отменённый job."""
            with DbSession(self.engine) as db, db.begin():
                row = db.scalar(
                    select(JobRecord).where(JobRecord.id == claim.job_id).with_for_update()
                )
                if (
                    row is None
                    or row.delivery_claim != claim.token
                    or row.payload.get("revision", 0) != claim.revision
                ):
                    return False
                now = utcnow()
                row.delivery_claim = None
                if retry_delay is None:
                    row.delivery_state, row.delivery_due = "published", None
                    row.delivery_confirmed_at = now
                else:
                    row.delivery_state = "pending"
                    row.delivery_due = now + timedelta(seconds=retry_delay)
                return True

        return await self._run(finish)

    async def delivery_inventory(self, retention: int) -> dict:
        """Возвращает счётчики и только технические координаты активных заданий."""

        def read():
            """Один снимок ограничен admission и не раскрывает payload документов."""
            now = utcnow()
            with DbSession(self.engine) as db:
                entries = db.execute(
                    select(
                        JobRecord.id,
                        JobRecord.state,
                        JobRecord.payload["job_type"].as_string(),
                        JobRecord.payload["revision"].as_integer(),
                        JobRecord.expires_at,
                        JobRecord.lease_until,
                        JobRecord.delivery_state,
                        JobRecord.delivery_attempts,
                        JobRecord.delivery_due,
                        JobRecord.delivery_confirmed_at,
                    ).where(JobRecord.state.in_(("queued", "running")), JobRecord.expires_at > now)
                )
                jobs = []
                counts = {"queued": 0, "running": 0, "stale_lease": 0}
                for (
                    identifier,
                    state,
                    profile,
                    revision,
                    expires,
                    lease,
                    delivery,
                    attempts,
                    due,
                    confirmed,
                ) in entries:
                    deadline = expires.replace(tzinfo=UTC) - timedelta(seconds=retention)
                    stale = (
                        state == "running"
                        and lease is not None
                        and lease.replace(tzinfo=UTC) <= now
                    )
                    counts[state] += 1
                    counts["stale_lease"] += int(stale)
                    delivery = delivery or "legacy_unknown"
                    counts[delivery] = counts.get(delivery, 0) + 1
                    jobs.append(
                        dict(
                            job_id=identifier,
                            profile=profile or "specification",
                            state=state,
                            revision=revision or 0,
                            expires_at=expires.isoformat(),
                            deadline=deadline.isoformat(),
                            lease_until=lease.isoformat() if lease else None,
                            delivery=delivery,
                            attempts=attempts or 0,
                            delivery_due=due.isoformat() if due else None,
                            confirmed_at=confirmed.isoformat() if confirmed else None,
                        )
                    )
                return dict(counts=counts, jobs=jobs)

        return await self._run(read)

    async def reconcile_deliveries(self, ids: list[str], retention: int) -> list[str]:
        """Вручную резервирует до десяти известных queued UUID после утраты сообщений."""
        if not 1 <= len(ids) <= 10:
            raise ValueError("Нужно указать от одного до десяти UUID")

        def reconcile():
            """Под блокировкой повторно проверяет состояние, бюджет и паузу после confirm."""
            now = utcnow()
            with DbSession(self.engine) as db, db.begin():
                rows = list(
                    db.scalars(
                        select(JobRecord)
                        .where(
                            JobRecord.id.in_(ids),
                            (JobRecord.state == "queued")
                            | ((JobRecord.state == "running") & (JobRecord.lease_until <= now)),
                            JobRecord.expires_at > now + timedelta(seconds=retention),
                            JobRecord.payload["cancel_requested"].as_boolean().is_not(True),
                            JobRecord.delivery_state.is_(None)
                            | (JobRecord.delivery_state == "closed")
                            | (
                                JobRecord.delivery_revision
                                != JobRecord.payload["revision"].as_integer()
                            )
                            | and_(
                                JobRecord.delivery_state == "published",
                                JobRecord.delivery_confirmed_at <= now - timedelta(minutes=5),
                            ),
                        )
                        .order_by(JobRecord.id)
                        .with_for_update()
                    )
                )
                for row in rows:
                    if row.delivery_revision is not None:
                        row.delivery_revision = row.payload.get("revision", 0)
                    self._reset_delivery(row, now)
                return [row.id for row in rows]

        return await self._run(reconcile)

    async def save_document(self, document: Document) -> None:
        """Сохраняет метаданные уже проверенного приватного PDF."""

        def save():
            """Фиксирует document до появления ссылок в jobs."""
            with DbSession(self.engine) as db, db.begin():
                db.merge(
                    DocumentRecord(
                        id=document.id,
                        expires_at=document.expires_at,
                        payload=document.model_dump(mode="json"),
                    )
                )

        await self._run(save)

    async def document(self, document_id: str) -> Document | None:
        """Возвращает DTO для проверки owner/TTL в application."""

        def read():
            """Не передаёт живую ORM session наружу."""
            with DbSession(self.engine) as db:
                row = db.get(DocumentRecord, document_id)
                return Document.model_validate(row.payload) if row else None

        return await self._run(read)

    async def session(self, token_hash: str) -> Session | None:
        """Возвращает только неистёкшую серверную сессию."""

        def read():
            """Проверяет TTL в SQL, избегая наивного времени SQLite."""
            with DbSession(self.engine) as db:
                row = db.scalar(
                    select(SessionRecord).where(
                        SessionRecord.id == token_hash, SessionRecord.expires_at > utcnow()
                    )
                )
                if not row:
                    return None
                # Совместимость с сессиями до удаления пароля: флаг больше не даёт прав.
                payload = {
                    key: value for key, value in row.payload.items() if key != "authenticated"
                }
                return Session.model_validate(payload)

        return await self._run(read)

    async def save_session(self, session: Session) -> None:
        """Сохраняет prompt, CSRF и TTL одной транзакцией."""

        def save():
            """Обновляет только строку текущего session hash."""
            with DbSession(self.engine) as db, db.begin():
                db.merge(
                    SessionRecord(
                        id=session.token_hash,
                        expires_at=session.expires_at,
                        payload=session.model_dump(mode="json"),
                    )
                )

        await self._run(save)

    async def edit_session(self, token_hash: str, change: Callable[[Session], None]) -> Session:
        """Не допускает lost update при независимых PUT промптов из двух вкладок."""

        def update():
            """Под блокировкой читает свежий snapshot, не продлевая фиксированный TTL."""
            with DbSession(self.engine) as db, db.begin():
                row = db.scalar(
                    select(SessionRecord)
                    .where(SessionRecord.id == token_hash, SessionRecord.expires_at > utcnow())
                    .with_for_update()
                )
                if row is None:
                    raise ServiceError("session_expired", "Веб-сессия истекла", 401)
                session = Session.model_validate(
                    {key: value for key, value in row.payload.items() if key != "authenticated"}
                )
                change(session)
                row.payload = session.model_dump(mode="json")
                return session

        return await self._run(update)

    async def overdue_jobs(self, deadline: datetime) -> list[str]:
        """Находит queued/running, чей бюджет истёк до начала периода хранения."""

        def read():
            """Возвращает идентификаторы; проверка состояния повторяется use-case под lock."""
            with DbSession(self.engine) as db:
                return list(
                    db.scalars(
                        select(JobRecord.id).where(
                            JobRecord.state.in_(["queued", "running"]),
                            (JobRecord.expires_at <= deadline)
                            | (
                                (JobRecord.state == "running")
                                & (JobRecord.lease_until <= utcnow())
                                & JobRecord.payload["cancel_requested"].as_boolean().is_(True)
                            ),
                        )
                    )
                )

        return await self._run(read)

    async def delete_session(self, token_hash: str) -> None:
        """Отзывает сессию при выходе; старый cookie больше не авторизует."""

        def remove():
            """Удаляет ровно указанный hash ключ."""
            with DbSession(self.engine) as db, db.begin():
                db.execute(delete(SessionRecord).where(SessionRecord.id == token_hash))

        await self._run(remove)

    async def cleanup(self) -> list[str]:
        """Удаляет истёкшие snapshots/events/sessions; UUID originals возвращает worker."""

        def remove():
            """Не удаляет PDF, пока жив хотя бы один использующий его job."""
            with DbSession(self.engine) as db, db.begin():
                expired = select(JobRecord.id).where(JobRecord.expires_at <= utcnow())
                db.execute(delete(EventRecord).where(EventRecord.job_id.in_(expired)))
                db.execute(delete(JobRecord).where(JobRecord.expires_at <= utcnow()))
                db.execute(delete(SessionRecord).where(SessionRecord.expires_at <= utcnow()))
                protected = {
                    document_id
                    for first_id, sources in db.execute(
                        select(
                            JobRecord.payload["document_id"].as_string(),
                            JobRecord.payload["sources"],
                        ).where(JobRecord.expires_at > utcnow())
                    )
                    for document_id in (
                        [s["document_id"] for s in sources] if sources else [first_id]
                    )
                }
                docs = list(
                    db.scalars(
                        select(DocumentRecord.id).where(
                            DocumentRecord.expires_at <= utcnow(),
                            DocumentRecord.id.not_in(protected),
                        )
                    )
                )
                db.execute(delete(DocumentRecord).where(DocumentRecord.id.in_(docs)))
                return docs

        return await self._run(remove)

    async def ping(self) -> bool:
        """Проверяет соединение и наличие мигрированной таблицы."""

        def check():
            """Технические ошибки DSN не попадают в публичный health payload."""
            with DbSession(self.engine) as db:
                db.execute(select(JobRecord.id).limit(1))
                return True

        try:
            return await self._run(check)
        except Exception:
            return False
