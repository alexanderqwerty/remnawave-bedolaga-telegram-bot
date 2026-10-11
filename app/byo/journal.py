"""Журнал шагов подключения поверх таблицы ``byo_steps``.

Реализует протокол ``StepJournal`` из ``provisioning``. Одна строка на пару
(нода, шаг): ``mark_running`` создаёт её на первой попытке и обновляет на
повторе, увеличивая ``attempts``; успех чистит прежнюю причину сбоя.

Каждая отметка сразу фиксируется (commit): за подключением следят из других
сессий, а перезапуск бота посреди подключения не должен стирать сделанное.
Вместе с отметкой фиксируются и прочие изменения сессии — это намеренно.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.byo.models import ByoStep, ByoStepStatus


DETAIL_MAX_LENGTH = 500


class DatabaseStepJournal:
    def __init__(self, db: AsyncSession, node_id: int) -> None:
        self._db = db
        self._node_id = node_id

    async def _row(self, name: str) -> ByoStep | None:
        result = await self._db.execute(select(ByoStep).where(ByoStep.node_id == self._node_id, ByoStep.name == name))
        return result.scalar_one_or_none()

    async def is_done(self, name: str) -> bool:
        row = await self._row(name)
        return row is not None and row.status == ByoStepStatus.DONE.value

    async def mark_running(self, name: str) -> None:
        row = await self._row(name)
        if row is None:
            row = ByoStep(node_id=self._node_id, name=name)
            self._db.add(row)
        row.status = ByoStepStatus.PENDING.value
        row.detail = None
        row.attempts = (row.attempts or 0) + 1
        await self._db.commit()

    async def mark_done(self, name: str) -> None:
        row = await self._row(name)
        if row is None:  # защита от вызова не по порядку
            row = ByoStep(node_id=self._node_id, name=name, attempts=1)
            self._db.add(row)
        row.status = ByoStepStatus.DONE.value
        row.detail = None
        await self._db.commit()

    async def mark_failed(self, name: str, detail: str) -> None:
        row = await self._row(name)
        if row is None:
            row = ByoStep(node_id=self._node_id, name=name, attempts=1)
            self._db.add(row)
        row.status = ByoStepStatus.FAILED.value
        row.detail = detail[:DETAIL_MAX_LENGTH]
        await self._db.commit()
