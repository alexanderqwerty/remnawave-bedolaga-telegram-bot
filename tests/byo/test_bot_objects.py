"""Объекты владельца в самом боте.

После того как в панели появился сквад владельца, бот заводит у себя: промо-группу
``owner:<slug>``, строку сервера для сквада (недоступна для выбора, видна только
группе владельца), бесплатный тариф на 2000 дней с одним сквадом и одной группой,
промокод, выдающий эту группу, и сразу кладёт владельца в его группу. Всё через
настоящие функции бота — так, как это делает админка, — на SQLite в памяти.
Повтор шага ничего не дублирует.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.byo.bot_objects import BotObjectsError, BotOwnerObjects
from app.byo.models import ByoOwner
from app.database.crud.promo_group import create_promo_group
from app.database.crud.server_squad import create_server_squad, get_server_squad_by_uuid
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import (
    PromoCode,
    PromoCodeType,
    PromoCodeUse,
    PromoGroup,
    ServerSquad,
    Tariff,
    User,
    UserPromoGroup,
    server_squad_promo_groups,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    PromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    ServerSquad.__table__,
    server_squad_promo_groups,
    PromoCode.__table__,
    PromoCodeUse.__table__,
    User.__table__,
    UserPromoGroup.__table__,
    ByoOwner.__table__,
)
SQUAD_UUID = '11111111-2222-4333-8444-555555555555'
OWNER_TELEGRAM_ID = 7001


async def _seed(db, *, with_default_group: bool = True, owner_is_a_bot_user: bool = True) -> ByoOwner:
    if with_default_group:
        await create_promo_group(
            db, 'Базовая', server_discount_percent=0, traffic_discount_percent=0, device_discount_percent=0
        )
    if owner_is_a_bot_user:
        db.add(User(telegram_id=OWNER_TELEGRAM_ID, first_name='Owner', language='ru', status='active'))
    owner = ByoOwner(telegram_id=OWNER_TELEGRAM_ID, slug='lab-a', squad_uuid=SQUAD_UUID)
    db.add(owner)
    await db.commit()
    return owner


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _owner_group(db) -> PromoGroup:
    return (await db.execute(select(PromoGroup).where(PromoGroup.name == 'owner:lab-a'))).scalar_one()


@pytest.mark.asyncio
async def test_owner_gets_a_group_of_their_own_that_is_not_the_default(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        await BotOwnerObjects(db).ensure_for(owner)

        group = await _owner_group(db)
        assert not group.is_default
        assert owner.promo_group_id == group.id


@pytest.mark.asyncio
async def test_owner_server_is_hidden_from_selection_and_bound_to_the_group(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        await BotOwnerObjects(db).ensure_for(owner)

        server = await get_server_squad_by_uuid(db, SQUAD_UUID)
        assert server is not None
        assert server.is_available is False
        assert [group.name for group in server.allowed_promo_groups] == ['owner:lab-a']


@pytest.mark.asyncio
async def test_tariff_is_free_long_single_squad_and_single_group(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        await BotOwnerObjects(db).ensure_for(owner)

        tariff = await get_tariff_by_id(db, owner.tariff_id)
        assert tariff.name == 'VPN lab-a'
        assert tariff.allowed_squads == [SQUAD_UUID]
        assert {int(days): price for days, price in tariff.period_prices.items()} == {2000: 0}
        assert tariff.traffic_limit_gb == 0
        assert tariff.is_trial_available is False
        assert tariff.show_in_gift is False
        assert tariff.is_active is True
        assert [group.name for group in tariff.allowed_promo_groups] == ['owner:lab-a']


@pytest.mark.asyncio
async def test_promo_code_grants_the_owner_group(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        await BotOwnerObjects(db).ensure_for(owner)

        code = (await db.execute(select(PromoCode).where(PromoCode.code == owner.promo_code))).scalar_one()
        assert code.type == PromoCodeType.PROMO_GROUP.value
        assert code.promo_group_id == owner.promo_group_id
        assert code.max_uses >= 100_000, 'код раздаётся всей группе'
        assert owner.promo_code.startswith('LABA')


@pytest.mark.asyncio
async def test_owner_is_put_into_their_group(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        await BotOwnerObjects(db).ensure_for(owner)

        user = (await db.execute(select(User).where(User.telegram_id == OWNER_TELEGRAM_ID))).scalar_one()
        memberships = (
            (await db.execute(select(UserPromoGroup).where(UserPromoGroup.user_id == user.id))).scalars().all()
        )
        assert [m.promo_group_id for m in memberships] == [owner.promo_group_id]
        assert user.promo_group_id == owner.promo_group_id


@pytest.mark.asyncio
async def test_a_second_run_creates_nothing_new(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        objects = BotOwnerObjects(db)
        await objects.ensure_for(owner)
        first = (owner.promo_group_id, owner.tariff_id, owner.promo_code)

        await objects.ensure_for(owner)

        assert (owner.promo_group_id, owner.tariff_id, owner.promo_code) == first
        assert await _count(db, PromoGroup) == 2  # базовая + владельца
        assert await _count(db, Tariff) == 1
        assert await _count(db, PromoCode) == 1
        assert await _count(db, ServerSquad) == 1


@pytest.mark.asyncio
async def test_a_tariff_lost_before_saving_its_id_is_found_by_name(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        objects = BotOwnerObjects(db)
        await objects.ensure_for(owner)
        tariff_id = owner.tariff_id
        owner.tariff_id = None  # бот упал между созданием тарифа и записью его id

        await objects.ensure_for(owner)

        assert owner.tariff_id == tariff_id
        assert await _count(db, Tariff) == 1


@pytest.mark.asyncio
async def test_a_chosen_promo_code_is_kept_on_retry(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        owner.promo_code = 'LABAFIXED1'  # код записан, но в боте ещё не создан

        await BotOwnerObjects(db).ensure_for(owner)

        assert owner.promo_code == 'LABAFIXED1'
        assert (await db.execute(select(PromoCode.code))).scalars().all() == ['LABAFIXED1']


@pytest.mark.asyncio
async def test_a_server_row_left_by_a_sync_is_locked_down(monkeypatch):
    # Синхронизация серверов в боте могла успеть завести строку: доступной и с группой по умолчанию.
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        await create_server_squad(db, squad_uuid=SQUAD_UUID, display_name='o-lab-a', is_available=True)

        await BotOwnerObjects(db).ensure_for(owner)

        server = await get_server_squad_by_uuid(db, SQUAD_UUID)
        await db.refresh(server, ['allowed_promo_groups'])
        assert server.is_available is False
        assert [group.name for group in server.allowed_promo_groups] == ['owner:lab-a']


@pytest.mark.asyncio
async def test_refuses_when_the_bot_has_no_default_group(monkeypatch):
    # Upstream делает первую созданную группу группой по умолчанию — то есть общей
    # для всех пользователей. Группа владельца такой стать не должна.
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db, with_default_group=False)

        with pytest.raises(BotObjectsError):
            await BotOwnerObjects(db).ensure_for(owner)

        assert await _count(db, PromoGroup) == 0


@pytest.mark.asyncio
async def test_requires_the_owner_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db)
        owner.squad_uuid = None

        with pytest.raises(BotObjectsError):
            await BotOwnerObjects(db).ensure_for(owner)


@pytest.mark.asyncio
async def test_an_owner_unknown_to_the_bot_still_gets_the_objects(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        owner = await _seed(db, owner_is_a_bot_user=False)

        await BotOwnerObjects(db).ensure_for(owner)

        assert owner.promo_code is not None
        assert await _count(db, UserPromoGroup) == 0
