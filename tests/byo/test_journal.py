"""Журнал шагов виден сразу и из другой сессии.

Подключение идёт минутами, а за прогрессом следят снаружи: владелец в боте,
админ командой. Если бот перезапустится посреди подключения, сделанные шаги не
должны потеряться. Поэтому каждая отметка журнала фиксируется сразу, а не ждёт
чужого commit. Файловая SQLite: у двух сессий разные соединения, и вторая видит
только зафиксированное.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.byo.journal import DatabaseStepJournal
from app.byo.models import ByoNode, ByoOwner, ByoStep
from app.database.models import Base
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


BYO_TABLES = [ByoOwner.__table__, ByoNode.__table__, ByoStep.__table__]


@pytest.mark.asyncio
async def test_steps_are_visible_from_another_session_right_away(monkeypatch, tmp_path):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "journal.db"}')
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=BYO_TABLES))
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

        async with maker() as worker:
            owner = ByoOwner(telegram_id=7001, slug='lab-a')
            worker.add(owner)
            await worker.flush()
            node = ByoNode(owner_id=owner.id, ordinal=1, ip='203.0.113.10')
            worker.add(node)
            await worker.commit()

            journal = DatabaseStepJournal(worker, node.id)
            await journal.mark_running('ssh')
            await journal.mark_done('ssh')
            await journal.mark_running('check')
            await journal.mark_failed('check', 'сервер в России')

            async with maker() as observer:
                seen = DatabaseStepJournal(observer, node.id)
                assert await seen.is_done('ssh')
                assert not await seen.is_done('check')
    finally:
        await engine.dispose()
