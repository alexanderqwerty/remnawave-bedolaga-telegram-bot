"""Машина шагов подключения ноды.

Один шаг — именованное действие над общим контекстом. ``run_steps`` гоняет
список под защитой журнала: уже выполненные пропускает (но даёт им восстановить
свой вклад в контекст через ``restore``), остальные выполняет по порядку,
записывая успех и неудачу. На любом сбое останавливается и поднимает
``ProvisioningError`` с именем упавшего шага — повтор продолжит с него.

Журнал абстрактный (протокол ``StepJournal``): в бою это таблица ``byo_steps``,
в тестах — словарь в памяти.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog


logger = structlog.get_logger(__name__)

StepAction = Callable[[dict], Awaitable[None]]


class StepJournal(Protocol):
    async def is_done(self, name: str) -> bool: ...
    async def mark_running(self, name: str) -> None: ...
    async def mark_done(self, name: str) -> None: ...
    async def mark_failed(self, name: str, detail: str) -> None: ...


@dataclass(frozen=True)
class Step:
    name: str
    action: StepAction
    # Восстанавливает вклад шага в контекст, когда сам шаг пропущен на повторе
    # (например, кладёт обратно уже известные fqdn/ws_path). Не делает побочных
    # действий. По умолчанию шаг ничего в контекст не добавляет.
    restore: StepAction | None = None


class ProvisioningError(RuntimeError):
    """Шаг подключения упал. ``step`` — имя упавшего шага."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f'{step}: {detail}')
        self.step = step
        self.detail = detail


# Короткое и человекочитаемое объяснение для журнала и владельца, без трейсбека.
def _describe(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:500]


async def run_steps(steps: list[Step], journal: StepJournal, *, context: dict) -> None:
    for step in steps:
        if await journal.is_done(step.name):
            if step.restore is not None:
                await step.restore(context)
            logger.debug('BYO: шаг уже выполнен, пропускаем', step=step.name)
            continue

        await journal.mark_running(step.name)
        try:
            await step.action(context)
        except Exception as error:  # любой сбой шага идёт в журнал и наверх
            detail = _describe(error)
            await journal.mark_failed(step.name, detail)
            logger.warning('BYO: шаг не выполнен', step=step.name, detail=detail)
            raise ProvisioningError(step.name, detail) from error
        await journal.mark_done(step.name)
        logger.info('BYO: шаг выполнен', step=step.name)
