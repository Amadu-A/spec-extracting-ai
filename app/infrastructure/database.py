# app/infrastructure/database.py
"""SQLAlchemy-хранилище: PostgreSQL в production, SQLite только в offline-тестах.

Снимок, checkpoint страницы и SSE-события изменяются одной транзакцией.
DB I/O вынесен из event loop. Блокировка строки защищает от старого worker.
"""

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import Session as DbSession

from app.domain.errors import ServiceError
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
                        old = Job.model_validate(previous.payload)
                        if old.fingerprint != job.fingerprint:
                            raise ServiceError(
                                "idempotency_conflict",
                                "Ключ уже использован с другими данными",
                                409,
                            )
                        return old
                count = db.scalar(
                    select(func.count())
                    .select_from(JobRecord)
                    .where(JobRecord.state.in_(["queued", "running"]))
                )
                if count >= capacity:
                    raise ServiceError("queue_full", "Очередь заполнена, повторите позже", 429)
                record = JobRecord(id=job.id, owner=job.owner, idempotency_key=job.idempotency_key)
                self._store(db, record, job)
                doc = db.get(DocumentRecord, job.document_id)
                if doc:
                    document = Document.model_validate(doc.payload)
                    document.expires_at = max(document.expires_at, job.expires_at)
                    doc.payload, doc.expires_at = (
                        document.model_dump(mode="json"),
                        document.expires_at,
                    )
                return job

        return await self._run(insert)

    def _store(self, db: DbSession, record: JobRecord, job: Job) -> None:
        """Сохраняет события и агрегат вместе; не вызывается из HTTP/application."""
        job.updated_at = utcnow()
        record.state, record.lease_until, record.expires_at = (
            job.state.value,
            job.lease_until,
            job.expires_at,
        )
        record.payload = job.model_dump(mode="json")
        db.add(record)
        # Между ORM-моделями нет relationship: явно создаём parent до event insert.
        # flush не делает commit, snapshot и журнал по-прежнему атомарны.
        db.flush()
        for journal_event in job.pending_events:
            db.add(
                EventRecord(
                    job_id=job.id,
                    id=journal_event.id,
                    payload=journal_event.model_dump(mode="json"),
                )
            )
        job.pending_events.clear()

    async def get(self, job_id: str) -> Job | None:
        """Читает новый снимок, исключая ORM из вызывающего слоя."""

        def read():
            """Закрывает DB session после материализации DTO."""
            with DbSession(self.engine) as db:
                row = db.get(JobRecord, job_id)
                return Job.model_validate(row.payload) if row else None

        return await self._run(read)

    async def edit(self, job_id: str, change: Callable[[Job], None]) -> Job:
        """Держит FOR UPDATE только во время чистой мутации, никогда во время VLM."""

        def update():
            """Читает свежий агрегат, применяет правило и атомарно фиксирует журнал."""
            with DbSession(self.engine) as db, db.begin():
                row = db.scalar(select(JobRecord).where(JobRecord.id == job_id).with_for_update())
                if not row:
                    raise ServiceError("job_not_found", "Задание не найдено", 404)
                job = Job.model_validate(row.payload)
                change(job)
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

    async def dispatchable(self, limit: int) -> list[str]:
        """Job-таблица служит durable outbox; повторные AMQP deliveries безопасны."""

        def read():
            """Ищет задания, не имеющие действующего lease worker."""
            with DbSession(self.engine) as db:
                query = (
                    select(JobRecord.id)
                    .where(
                        JobRecord.expires_at > utcnow(),
                        (JobRecord.state == "queued")
                        | ((JobRecord.state == "running") & (JobRecord.lease_until < utcnow())),
                    )
                    .limit(limit)
                )
                return list(db.scalars(query))

        return await self._run(read)

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
                return Session.model_validate(row.payload) if row else None

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

    async def touch_session(self, token_hash: str, expires_at: datetime) -> Session | None:
        """Продлевает только TTL под FOR UPDATE, сохраняя актуальный prompt."""

        def touch():
            """Старый cookie не воскрешает истёкшую/отозванную сессию."""
            with DbSession(self.engine) as db, db.begin():
                row = db.scalar(
                    select(SessionRecord)
                    .where(SessionRecord.id == token_hash, SessionRecord.expires_at > utcnow())
                    .with_for_update()
                )
                if not row:
                    return None
                current = Session.model_validate(row.payload)
                current.expires_at = expires_at
                row.expires_at, row.payload = expires_at, current.model_dump(mode="json")
                return current

        return await self._run(touch)

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
                docs = list(
                    db.scalars(
                        select(DocumentRecord.id).where(DocumentRecord.expires_at <= utcnow())
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
