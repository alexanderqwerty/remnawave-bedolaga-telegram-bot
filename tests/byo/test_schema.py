"""Создание таблиц BYO при старте бота.

На свежей БД upstream делает ``create_all`` по всему ``Base``, а на
существующей только гонит свои миграции — таблиц ``byo_*`` там не будет.
``ensure_tables`` докладывает недостающие и не трогает уже созданные.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.byo.models import ByoOwner
from app.byo.schema import BYO_TABLES, ensure_tables
from app.database.models import Base, PromoGroup, Tariff
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


UPSTREAM_TABLES = (PromoGroup.__table__, Tariff.__table__)


async def _existing_upstream_database(monkeypatch):
    """База, какой её оставили миграции upstream: своих таблиц нет."""
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=list(UPSTREAM_TABLES)))
    return engine


async def _table_names(engine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))


@pytest.mark.asyncio
async def test_missing_byo_tables_are_created(monkeypatch):
    engine = await _existing_upstream_database(monkeypatch)
    try:
        await ensure_tables(engine)
        assert {table.name for table in BYO_TABLES}.issubset(await _table_names(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_rows_survive_a_second_start(monkeypatch):
    engine = await _existing_upstream_database(monkeypatch)
    try:
        await ensure_tables(engine)
        async with engine.begin() as conn:
            await conn.execute(ByoOwner.__table__.insert().values(telegram_id=1, slug='lab-a'))

        await ensure_tables(engine)

        async with engine.connect() as conn:
            rows = (await conn.execute(ByoOwner.__table__.select())).all()
        assert len(rows) == 1
    finally:
        await engine.dispose()


def test_only_byo_tables_are_touched():
    assert all(table.name.startswith('byo_') for table in BYO_TABLES)
