"""Engine construction. Parameters are hidden from SQLAlchemy error messages so that a
database exception can never carry message content, fingerprints or tokens into logs."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, *, pool_size: int = 10, max_overflow: int = 10) -> AsyncEngine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("database URL must use the postgresql+psycopg driver")
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        hide_parameters=True,
        echo=False,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def rowcount(result: Result[Any]) -> int:
    """Rows affected by an UPDATE/DELETE (``Result`` is a ``CursorResult`` at runtime)."""
    return int(cast(CursorResult[Any], result).rowcount or 0)
