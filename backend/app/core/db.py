"""Async SQLAlchemy engine, session factory and FastAPI dependency.

The engine is created lazily so that importing the app (and therefore running
the test-suite) never requires a reachable PostgreSQL.  ``get_session`` yields
``None`` when the database is disabled/unreachable, and every call-site treats a
``None`` session as "skip the audit write" rather than failing the trade.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_engine: Any = None
_sessionmaker: Any = None
_enabled: bool = False


async def init_db(url: Optional[str] = None) -> Any:
    """Create the async engine + sessionmaker.  Never raises."""
    global _engine, _sessionmaker, _enabled
    if _engine is not None:
        return _engine
    settings = get_settings()
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        _engine = create_async_engine(
            url or settings.database_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        _sessionmaker = async_sessionmaker(
            _engine, expire_on_commit=False, autoflush=False
        )
        _enabled = True
    except Exception:
        logger.warning("database engine unavailable; audit ledger disabled", exc_info=True)
        _engine = None
        _sessionmaker = None
        _enabled = False
    return _engine


async def close_db() -> None:
    """Dispose of the pool on shutdown."""
    global _engine, _sessionmaker, _enabled
    if _engine is not None:
        try:
            await _engine.dispose()
        except Exception:  # pragma: no cover - shutdown best effort
            logger.warning("engine dispose failed", exc_info=True)
    _engine = None
    _sessionmaker = None
    _enabled = False


def set_sessionmaker(factory: Any) -> None:
    """Inject a session factory (tests / in-memory SQLite)."""
    global _sessionmaker, _enabled
    _sessionmaker = factory
    _enabled = factory is not None


def get_engine() -> Any:
    return _engine


def get_sessionmaker() -> Any:
    return _sessionmaker


def is_enabled() -> bool:
    return bool(_enabled and _sessionmaker is not None)


@asynccontextmanager
async def session_scope() -> AsyncIterator[Any]:
    """Transactional scope.  Yields ``None`` when the ledger is disabled."""
    if not is_enabled():
        yield None
        return
    session = _sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[Any]:
    """FastAPI dependency — ``Depends(get_session)``."""
    if not is_enabled():
        yield None
        return
    session = _sessionmaker()
    try:
        yield session
    finally:
        await session.close()


async def ping() -> bool:
    """Reachability probe that never raises."""
    if not is_enabled() or _engine is None:
        return False
    try:
        from sqlalchemy import text

        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def create_all() -> bool:
    """Create the ORM schema (dev/test convenience; prod uses sql/init)."""
    from app.models.tables import Base

    if _engine is None:
        return False
    try:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return True
    except Exception:
        logger.warning("create_all failed", exc_info=True)
        return False
