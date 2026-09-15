"""数据库引擎与会话管理（SQLite + SQLAlchemy 2.0 async）。"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.resolved_database_url(),
    echo=False,
    future=True,
    # SQLite 在多协程写入时需要放宽超时，避免 "database is locked"
    connect_args={"timeout": 30, "check_same_thread": False},
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """建表 + 打开 WAL，提升并发读写表现。"""
    from app import models  # noqa: F401  确保模型已注册到 metadata

    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            from sqlalchemy import text

            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个会话。"""
    async with SessionLocal() as session:
        yield session


async def dispose_db() -> None:
    await engine.dispose()
