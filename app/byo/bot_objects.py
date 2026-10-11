"""Объекты владельца в БД бота: группа, сервер, тариф, промокод.

Изоляция в боте держится на связке «промо-группа → тариф с одним сквадом →
промокод ``promo_group``»: код кладёт участника в группу владельца, а группе
виден только тариф с его сквадом. ``BotOwnerObjects.ensure_for`` заводит эту
связку через настоящие функции бота (как админка) по принципу «найти или
создать»: повтор после сбоя ничего не дублирует.

Общую синхронизацию серверов upstream (``sync_with_remnawave``) здесь нарочно не
зовём: если панель не ответит, она получит пустой список и удалит в боте все
серверы. Заводим ровно одну строку — для сквада владельца.
"""

from __future__ import annotations

import secrets

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.byo.models import ByoOwner
from app.byo.node_secrets import squad_name
from app.database.crud.promo_group import create_promo_group, get_default_promo_group
from app.database.crud.promocode import create_promocode, get_promocode_by_code
from app.database.crud.server_squad import (
    create_server_squad,
    get_server_squad_by_uuid,
    update_server_squad,
    update_server_squad_promo_groups,
)
from app.database.crud.tariff import create_tariff, get_tariff_by_id
from app.database.crud.user_promo_group import add_user_to_promo_group
from app.database.models import PromoCodeType, PromoGroup, Tariff, User


logger = structlog.get_logger(__name__)

TARIFF_PERIOD_DAYS = 2000
UNLIMITED_TRAFFIC_GB = 0
DEVICE_LIMIT = 5
# В боте нет «безлимитного» кода: он действует, пока использований меньше max_uses.
PRACTICALLY_UNLIMITED_USES = 1_000_000
# Восемь hex-символов после slug — перебором через бота не подобрать.
PROMO_CODE_RANDOM_BYTES = 4


class BotObjectsError(RuntimeError):
    """Объекты владельца в боте завести нельзя (и пытаться не стоит)."""


def group_name(slug: str) -> str:
    return f'owner:{slug}'


def tariff_name(slug: str) -> str:
    return f'VPN {slug}'


def new_promo_code(slug: str) -> str:
    return f'{slug.replace("-", "").upper()}{secrets.token_hex(PROMO_CODE_RANDOM_BYTES).upper()}'


class BotOwnerObjects:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def ensure_for(self, owner: ByoOwner) -> None:
        if owner.squad_uuid is None:
            raise BotObjectsError('сквад владельца в панели ещё не создан')
        group = await self._ensure_group(owner.slug)
        owner.promo_group_id = group.id
        await self._ensure_server(owner, group)
        owner.tariff_id = (await self._ensure_tariff(owner, group)).id
        await self._ensure_promo_code(owner, group)
        await self._ensure_membership(owner, group)
        await self._db.commit()

    async def _ensure_group(self, slug: str) -> PromoGroup:
        existing = await self._db.execute(select(PromoGroup).where(PromoGroup.name == group_name(slug)))
        group = existing.scalar_one_or_none()
        if group is not None:
            return group
        # Upstream делает первую созданную группу группой по умолчанию, то есть
        # общей для всех. Группа владельца такой стать не должна.
        if await get_default_promo_group(self._db) is None:
            raise BotObjectsError('в боте нет промо-группы по умолчанию — группа владельца стала бы общей')
        return await create_promo_group(
            self._db,
            group_name(slug),
            server_discount_percent=0,
            traffic_discount_percent=0,
            device_discount_percent=0,
        )

    async def _ensure_server(self, owner: ByoOwner, group: PromoGroup) -> None:
        server = await get_server_squad_by_uuid(self._db, owner.squad_uuid)
        if server is None:
            await create_server_squad(
                self._db,
                squad_uuid=owner.squad_uuid,
                display_name=tariff_name(owner.slug),
                original_name=squad_name(owner.slug),
                is_available=False,
                promo_group_ids=[group.id],
            )
            return
        if server.is_available:
            await update_server_squad(self._db, server.id, is_available=False)
        if {allowed.id for allowed in server.allowed_promo_groups} != {group.id}:
            await update_server_squad_promo_groups(self._db, server.id, [group.id])

    async def _ensure_tariff(self, owner: ByoOwner, group: PromoGroup) -> Tariff:
        tariff = await get_tariff_by_id(self._db, owner.tariff_id) if owner.tariff_id else None
        if tariff is None:
            by_name = await self._db.execute(select(Tariff).where(Tariff.name == tariff_name(owner.slug)))
            tariff = by_name.scalars().first()
        if tariff is not None:
            return tariff
        return await create_tariff(
            self._db,
            tariff_name(owner.slug),
            description='Серверы вашей группы',
            traffic_limit_gb=UNLIMITED_TRAFFIC_GB,
            device_limit=DEVICE_LIMIT,
            allowed_squads=[owner.squad_uuid],
            period_prices={TARIFF_PERIOD_DAYS: 0},
            is_trial_available=False,
            allow_traffic_topup=False,
            promo_group_ids=[group.id],
            show_in_gift=False,
        )

    async def _ensure_promo_code(self, owner: ByoOwner, group: PromoGroup) -> None:
        if owner.promo_code is None:
            # Код фиксируется до создания: повтор после сбоя возьмёт тот же.
            owner.promo_code = new_promo_code(owner.slug)
            await self._db.commit()
        if await get_promocode_by_code(self._db, owner.promo_code) is None:
            await create_promocode(
                self._db,
                owner.promo_code,
                PromoCodeType.PROMO_GROUP,
                max_uses=PRACTICALLY_UNLIMITED_USES,
                promo_group_id=group.id,
            )

    async def _ensure_membership(self, owner: ByoOwner, group: PromoGroup) -> None:
        found = await self._db.execute(select(User.id).where(User.telegram_id == owner.telegram_id))
        user_id = found.scalar_one_or_none()
        if user_id is None:
            logger.warning('BYO: владельца нет среди пользователей бота', telegram_id=owner.telegram_id)
            return
        await add_user_to_promo_group(self._db, user_id, group.id, assigned_by='system')
