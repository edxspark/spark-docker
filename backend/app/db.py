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
        await _ensure_columns(conn)


# 新增列清单：create_all 不会改动已存在的表，已有库需要补列。
# SQLite 的 ALTER TABLE ADD COLUMN 是轻量操作，这里做加法式迁移。
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "task_items": {
        "cover_landscape_path": "TEXT DEFAULT ''",
    },
}


async def _ensure_columns(conn) -> None:
    from sqlalchemy import text

    for table, columns in _ADDITIVE_COLUMNS.items():
        try:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result.fetchall()}
        except Exception:  # noqa: BLE001 - 表可能还不存在
            continue
        for name, ddl in columns.items():
            if name not in existing:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个会话。"""
    async with SessionLocal() as session:
        yield session


async def dispose_db() -> None:
    await engine.dispose()
