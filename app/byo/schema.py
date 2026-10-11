"""Таблицы BYO на уже работающей БД.

Upstream создаёт схему ``create_all`` только на пустой базе, а дальше гонит свои
миграции — таблиц ``byo_*`` на существующей базе не появится. ``ensure_tables``
вызывать при старте бота, до первого обращения к BYO: она докладывает недостающие
таблицы (``checkfirst``), не трогая ни созданные, ни таблицы upstream.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.byo.models import ByoNode, ByoOwner, ByoStep
from app.database.models import Base


# Порядок важен: ноды ссылаются на владельцев, шаги — на ноды.
BYO_TABLES = (ByoOwner.__table__, ByoNode.__table__, ByoStep.__table__)


async def ensure_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=list(BYO_TABLES), checkfirst=True)
        )
