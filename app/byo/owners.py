"""Владельцы и их серверы в БД бота: имя группы, IP, лимиты, «забыть».

Пользователь сам выбирает имя группы и присылает IP сервера. Здесь проверяется,
что имя годится для панели и свободно (в том числе от остатков в боте), что IP —
публичный IPv4 и ещё не подключён, и что бот не превращается в сканер чужих
адресов: не больше ``max_nodes`` серверов на владельца и ``max_new_per_day``
новых попыток в сутки. «Забыть» можно только сервер, по которому ещё ничего не
создано снаружи (до шага DNS); строка остаётся и считается в суточном лимите.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.byo.bot_objects import group_name, tariff_name
from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner, ByoStep, ByoStepStatus
from app.byo.node_secrets import SLUG_PATTERN
from app.database.models import PromoGroup, Tariff


SEPARATORS = re.compile(r'[\s_-]+')
ATTEMPT_WINDOW = timedelta(days=1)
# Первый шаг, который создаёт что-то снаружи (запись DNS); после него «забыть» нельзя.
FIRST_EXTERNAL_STEP = 'dns'


class OwnerError(Exception):
    """Понятная пользователю причина, по которой действие выполнить нельзя."""


class NodeLimits(NamedTuple):
    max_nodes: int
    max_new_per_day: int


class NodeRequest(NamedTuple):
    telegram_id: int
    ip: str
    slug: str | None  # только для нового владельца


def parse_group_name(text: str) -> str | None:
    slug = SEPARATORS.sub('-', text.strip().lower())
    return slug if SLUG_PATTERN.fullmatch(slug) else None


def parse_public_ipv4(text: str) -> str | None:
    try:
        address = ipaddress.IPv4Address(text.strip())
    except ValueError:
        return None
    # Приватные, служебные и документационные адреса: через бота не должно быть
    # видно внутренних сетей входа и малины.
    return str(address) if address.is_global else None


async def find_owner(db: AsyncSession, telegram_id: int) -> ByoOwner | None:
    result = await db.execute(
        select(ByoOwner).options(selectinload(ByoOwner.nodes)).where(ByoOwner.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def is_slug_free(db: AsyncSession, slug: str) -> bool:
    checks = (
        select(ByoOwner.id).where(ByoOwner.slug == slug),
        select(PromoGroup.id).where(PromoGroup.name == group_name(slug)),
        select(Tariff.id).where(Tariff.name == tariff_name(slug)),
    )
    for query in checks:
        if (await db.execute(query.limit(1))).first() is not None:
            return False
    return True


async def load_node(db: AsyncSession, node_id: int) -> ByoNode | None:
    result = await db.execute(
        select(ByoNode).options(selectinload(ByoNode.owner).selectinload(ByoOwner.nodes)).where(ByoNode.id == node_id)
    )
    return result.scalar_one_or_none()


async def pending_node_ids(db: AsyncSession) -> list[int]:
    result = await db.execute(
        select(ByoNode.id).where(ByoNode.status == ByoNodeStatus.PROVISIONING.value).order_by(ByoNode.id)
    )
    return list(result.scalars())


async def _ip_in_use(db: AsyncSession, ip: str) -> bool:
    query = select(ByoNode.id).where(ByoNode.ip == ip, ByoNode.status != ByoNodeStatus.REMOVED.value)
    return (await db.execute(query.limit(1))).first() is not None


async def _new_owner(db: AsyncSession, request: NodeRequest) -> ByoOwner:
    if request.slug is None or not await is_slug_free(db, request.slug):
        raise OwnerError('Это имя группы уже занято — выберите другое.')
    # Пустой список сразу: иначе обращение к owner.nodes после flush полезет в БД лениво.
    owner = ByoOwner(telegram_id=request.telegram_id, slug=request.slug, nodes=[])
    db.add(owner)
    await db.flush()
    return owner


async def check_limits(db: AsyncSession, owner: ByoOwner, limits: NodeLimits) -> None:
    if owner.is_blocked:
        raise OwnerError('Подключение серверов для вас отключено. Напишите в поддержку.')
    active = [node for node in owner.nodes if node.status != ByoNodeStatus.REMOVED.value]
    if len(active) >= limits.max_nodes:
        raise OwnerError(f'Можно подключить не больше {limits.max_nodes} серверов.')
    since = datetime.now(UTC) - ATTEMPT_WINDOW
    recent = await db.execute(
        select(func.count(ByoNode.id)).where(ByoNode.owner_id == owner.id, ByoNode.created_at >= since)
    )
    if recent.scalar_one() >= limits.max_new_per_day:
        raise OwnerError(f'За сутки можно добавить не больше {limits.max_new_per_day} серверов. Попробуйте завтра.')


async def register_node(db: AsyncSession, request: NodeRequest, limits: NodeLimits) -> ByoNode:
    if await _ip_in_use(db, request.ip):
        raise OwnerError('Этот сервер уже подключён.')
    owner = await find_owner(db, request.telegram_id) or await _new_owner(db, request)
    await check_limits(db, owner, limits)
    active = [node.ordinal for node in owner.nodes if node.status != ByoNodeStatus.REMOVED.value]
    node = ByoNode(ordinal=max(active, default=0) + 1, ip=request.ip, status=ByoNodeStatus.PROVISIONING.value)
    # Через связь, а не owner_id: тогда и node.owner, и owner.nodes сразу верны в памяти.
    owner.nodes.append(node)
    await db.commit()
    return node


async def failure_reasons(db: AsyncSession, node_ids: list[int]) -> dict[int, tuple[str, str]]:
    """Упавший шаг и его причина для каждой ноды из списка, у которой он есть."""
    if not node_ids:
        return {}
    result = await db.execute(
        select(ByoStep.node_id, ByoStep.name, ByoStep.detail).where(
            ByoStep.node_id.in_(node_ids), ByoStep.status == ByoStepStatus.FAILED.value
        )
    )
    return {node_id: (name, detail or '') for node_id, name, detail in result.all()}


async def can_forget(db: AsyncSession, node: ByoNode) -> bool:
    if node.status != ByoNodeStatus.FAILED.value:
        return False
    attempted = await db.execute(select(ByoStep.name).where(ByoStep.node_id == node.id))
    return FIRST_EXTERNAL_STEP not in set(attempted.scalars()) and node.config_profile_uuid is None


async def forget_node(db: AsyncSession, node: ByoNode) -> None:
    if not await can_forget(db, node):
        raise OwnerError('Этот сервер уже частично настроен — убрать его может только администратор.')
    node.status = ByoNodeStatus.REMOVED.value
    await db.commit()
