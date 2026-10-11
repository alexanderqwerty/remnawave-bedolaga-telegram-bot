"""Страж тарифов: чужой тариф нельзя получить подделанной кнопкой.

Upstream фильтрует тарифы по промо-группе только при показе списка. Обработчики
за кнопками верят номеру тарифа из callback и проверяют лишь `is_active`, поэтому
участник одной группы мог оформить бесплатный тариф другой. Страж стоит перед
всеми такими обработчиками сразу, не трогая их код.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserPromoGroup, tariff_promo_groups
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

GroupsLoader = Callable[[Any, Any], Awaitable[set[int]]]

# Кнопка → номер поля (через двоеточие), в котором лежит id тарифа.
TARIFF_ID_FIELD: dict[str, int] = {
    'tariff_select': 1,
    'tariff_period': 1,
    'tariff_period_traffic': 1,
    'tariff_confirm': 1,
    'tariff_sbp': 1,
    'tariff_lava': 1,
    'tariff_cashera': 1,
    'daily_tariff_confirm': 1,
    'custom_days': 1,
    'custom_traffic': 1,
    'custom_confirm': 1,
    # У продления первым идёт id подписки: tariff_extend:{sub_id}:{tariff_id}[:{period}]
    'tariff_extend': 2,
    'tariff_ext_confirm': 2,
    'tariff_sw_select': 1,
    'tariff_sw_period': 1,
    'tariff_sw_confirm': 1,
    'daily_tariff_switch_confirm': 1,
    'instant_sw_preview': 1,
    'instant_sw_confirm': 1,
    'gift_tariff': 1,
    'gift_period': 1,
}


def tariff_id_from_callback(data: str | None) -> int | None:
    """Возвращает id тарифа из данных кнопки или None, если прочитать его нельзя."""
    if not data:
        return None
    parts = data.split(':')
    field = TARIFF_ID_FIELD.get(parts[0])
    if field is None or len(parts) <= field or not parts[field].isdecimal():
        return None
    return int(parts[field])


def is_tariff_allowed(tariff_group_ids: set[int], user_group_ids: set[int]) -> bool:
    """Тариф без ограничений доступен всем, с ограничениями — только участникам его групп."""
    return not tariff_group_ids or bool(tariff_group_ids & user_group_ids)


async def load_tariff_group_ids(db: AsyncSession, tariff_id: int) -> set[int]:
    """Группы, которым разрешён тариф. Пусто — ограничений нет (или тарифа не существует)."""
    result = await db.execute(
        select(tariff_promo_groups.c.promo_group_id).where(tariff_promo_groups.c.tariff_id == tariff_id)
    )
    return set(result.scalars().all())


async def load_user_group_ids(db: AsyncSession, db_user: User | None) -> set[int]:
    """Все группы пользователя: основная из `users.promo_group_id` и членства.

    Список тарифов бот строит по основной группе, кабинет — по членствам; берём
    объединение, чтобы страж не отказывал в том, что пользователю уже показали.
    """
    if db_user is None:
        return set()
    result = await db.execute(select(UserPromoGroup.promo_group_id).where(UserPromoGroup.user_id == db_user.id))
    group_ids = set(result.scalars().all())
    if db_user.promo_group_id is not None:
        group_ids.add(db_user.promo_group_id)
    return group_ids


class TariffAccessMiddleware(BaseMiddleware):
    """Не пускает к обработчику кнопку с тарифом, недоступным группам пользователя."""

    def __init__(
        self,
        tariff_groups_loader: GroupsLoader = load_tariff_group_ids,
        user_groups_loader: GroupsLoader = load_user_group_ids,
    ) -> None:
        self._load_tariff_group_ids = tariff_groups_loader
        self._load_user_group_ids = user_groups_loader

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tariff_id = tariff_id_from_callback(getattr(event, 'data', None))
        if tariff_id is None or await self._is_allowed(tariff_id, data):
            return await handler(event, data)

        db_user = data.get('db_user')
        logger.warning(
            'Отклонена кнопка с тарифом, недоступным группам пользователя',
            user_id=getattr(db_user, 'id', None),
            tariff_id=tariff_id,
        )
        language = getattr(db_user, 'language', None)
        texts = get_texts(language) if language else get_texts()
        await event.answer(texts.t('TARIFF_PURCHASE_UNAVAILABLE', 'Тариф недоступен'), show_alert=True)
        return None

    async def _is_allowed(self, tariff_id: int, data: dict[str, Any]) -> bool:
        db = data.get('db')
        if db is None:
            # Проверить доступ нечем — безопаснее отказать, чем пропустить.
            return False
        tariff_group_ids = await self._load_tariff_group_ids(db, tariff_id)
        if not tariff_group_ids:
            return True
        user_group_ids = await self._load_user_group_ids(db, data.get('db_user'))
        return is_tariff_allowed(tariff_group_ids, user_group_ids)
