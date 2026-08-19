"""Database engine, sessions, and bootstrap helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from agenticscrum.config import Settings, load_roster_seeds, load_settings
from agenticscrum.models import Base, TeamMember


_ENGINE_CACHE: dict[str, Engine] = {}
_SESSION_FACTORY_CACHE: dict[str, sessionmaker[Session]] = {}


def _get_engine(config: Settings) -> Engine:
    url = config.database_url
    existing = _ENGINE_CACHE.get(url)
    if existing is not None:
        return existing

    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # - timeout: wait for write locks instead of failing immediately
        # - check_same_thread: allow scheduler/background tasks safely
        connect_args = {"timeout": 30, "check_same_thread": False}

    engine = create_engine(url, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA busy_timeout=30000;")
            finally:
                cursor.close()

    _ENGINE_CACHE[url] = engine
    return engine


def create_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """Create a SQLAlchemy session factory for the configured database."""

    config = settings or load_settings()
    url = config.database_url
    existing = _SESSION_FACTORY_CACHE.get(url)
    if existing is not None:
        return existing

    engine = _get_engine(config)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    _SESSION_FACTORY_CACHE[url] = factory
    return factory


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Provide a transactional session scope."""

    factory = create_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(settings: Settings | None = None) -> None:
    """Create database tables and seed roster if empty."""

    config = settings or load_settings()
    engine = _get_engine(config)
    Base.metadata.create_all(engine)
    _ensure_sqlite_columns(engine)
    factory = create_session_factory(config)
    with factory() as session:
        seed_roster(session)
        session.commit()


def _ensure_sqlite_columns(engine: Engine) -> None:
    """Add columns introduced after initial create_all for existing SQLite DBs."""

    url = str(engine.url)
    if not url.startswith("sqlite"):
        return

    alterations: list[tuple[str, str, str]] = [
        ("chat_sessions", "summary", "TEXT"),
        ("chat_sessions", "archived", "BOOLEAN DEFAULT 0 NOT NULL"),
        ("chat_messages", "message_kind", "VARCHAR(50) DEFAULT 'message' NOT NULL"),
        ("chat_messages", "message_meta", "JSON"),
        ("tool_call_logs", "chat_session_id", "INTEGER"),
        ("tool_call_logs", "chat_message_id", "INTEGER"),
    ]
    with engine.begin() as conn:
        for table, column, col_type in alterations:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            if column in existing:
                continue
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            if table == "chat_messages" and column == "message_meta":
                conn.exec_driver_sql(
                    "UPDATE chat_messages SET message_meta = '{}' WHERE message_meta IS NULL"
                )


def seed_roster(session: Session) -> None:
    """Seed team roster from YAML when the table is empty."""

    count = session.scalar(select(func.count(TeamMember.id))) or 0
    if count:
        return
    for seed in load_roster_seeds():
        session.add(
            TeamMember(
                display_name=seed.display_name,
                email=seed.email,
                ado_unique_name=seed.ado_unique_name,
            )
        )
