"""Журнал шагов поверх таблицы byo_steps — на настоящем PostgreSQL.

Без базы тест честно пропускается (нужен TEST_DATABASE_URL). Проверяем то, что
на моках не видно: первая попытка шага вставляет строку, повтор обновляет её и
увеличивает счётчик, а уникальность ``(node_id, name)`` не даёт дублей.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.byo.journal import DatabaseStepJournal
from app.byo.models import ByoNode, ByoOwner, ByoStep, ByoStepStatus
from tests.fixtures.postgres_db import postgres_session, require_postgres_dsn


pytestmark = pytest.mark.postgres

BYO_TABLES = (ByoOwner.__table__, ByoNode.__table__, ByoStep.__table__)


async def _node(db) -> ByoNode:
    owner = ByoOwner(telegram_id=7001, slug='lab-a')
    db.add(owner)
    await db.flush()
    node = ByoNode(owner_id=owner.id, ordinal=1, ip='203.0.113.10')
    db.add(node)
    await db.flush()
    return node


@pytest.mark.asyncio
async def test_first_attempt_inserts_a_pending_then_done_row():
    async with postgres_session(require_postgres_dsn(), BYO_TABLES) as db:
        node = await _node(db)
        journal = DatabaseStepJournal(db, node.id)

        assert not await journal.is_done('ssh')
        await journal.mark_running('ssh')
        await journal.mark_done('ssh')

        row = (await db.execute(select(ByoStep).where(ByoStep.node_id == node.id))).scalar_one()
        assert row.name == 'ssh'
        assert row.status == ByoStepStatus.DONE.value
        assert row.attempts == 1
        assert await journal.is_done('ssh')


@pytest.mark.asyncio
async def test_retry_updates_the_same_row_and_counts_attempts():
    async with postgres_session(require_postgres_dsn(), BYO_TABLES) as db:
        node = await _node(db)
        journal = DatabaseStepJournal(db, node.id)

        await journal.mark_running('verify')
        await journal.mark_failed('verify', 'no free port 443')
        await journal.mark_running('verify')
        await journal.mark_done('verify')

        rows = (await db.execute(select(ByoStep).where(ByoStep.node_id == node.id))).scalars().all()
        assert len(rows) == 1, 'повтор не должен плодить строки'
        assert rows[0].attempts == 2
        assert rows[0].status == ByoStepStatus.DONE.value
        assert rows[0].detail is None, 'успех очищает прежнюю причину сбоя'


@pytest.mark.asyncio
async def test_failure_detail_is_stored_and_truncated():
    async with postgres_session(require_postgres_dsn(), BYO_TABLES) as db:
        node = await _node(db)
        journal = DatabaseStepJournal(db, node.id)

        await journal.mark_running('dns')
        await journal.mark_failed('dns', 'x' * 900)

        row = (await db.execute(select(ByoStep).where(ByoStep.node_id == node.id))).scalar_one()
        assert row.status == ByoStepStatus.FAILED.value
        assert row.detail is not None
        assert len(row.detail) <= 500


@pytest.mark.asyncio
async def test_steps_of_two_nodes_do_not_collide():
    async with postgres_session(require_postgres_dsn(), BYO_TABLES) as db:
        first = await _node(db)
        second = ByoNode(owner_id=first.owner_id, ordinal=2, ip='203.0.113.11')
        db.add(second)
        await db.flush()

        first_journal = DatabaseStepJournal(db, first.id)
        await first_journal.mark_running('ssh')
        await first_journal.mark_done('ssh')
        await DatabaseStepJournal(db, second.id).mark_running('ssh')

        assert await DatabaseStepJournal(db, first.id).is_done('ssh')
        assert not await DatabaseStepJournal(db, second.id).is_done('ssh')
