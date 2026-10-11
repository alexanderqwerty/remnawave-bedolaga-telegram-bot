"""Владельцы и их серверы: имя группы, IP, лимиты, повтор и «забыть».

Пользователь сам выбирает имя группы и присылает IP сервера. Бот проверяет, что
имя годится для панели и свободно (в том числе от остатков лаборатории), что IP —
публичный IPv4 и ещё не подключён, и не даёт превратить себя в сканер чужих
адресов: не больше трёх серверов на владельца и трёх новых попыток в сутки.
SQLite в памяти.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner, ByoStep, ByoStepStatus
from app.byo.owners import (
    NodeLimits,
    NodeRequest,
    OwnerError,
    can_forget,
    find_owner,
    forget_node,
    is_slug_free,
    load_node,
    parse_group_name,
    parse_public_ipv4,
    pending_node_ids,
    register_node,
)
from app.database.models import PromoGroup, Tariff
from tests.fixtures.sqlite_memory import memory_session


TABLES = (ByoOwner.__table__, ByoNode.__table__, ByoStep.__table__, PromoGroup.__table__, Tariff.__table__)
LIMITS = NodeLimits(max_nodes=3, max_new_per_day=3)
OWNER_TG = 7001


def _request(ip: str, slug: str | None = 'miet') -> NodeRequest:
    return NodeRequest(telegram_id=OWNER_TG, ip=ip, slug=slug)


# --- имя группы и IP ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('text', 'slug'),
    [('miet', 'miet'), ('  MIET_TV ', 'miet-tv'), ('Cheer Team', 'cheer-team'), ('infocus2', 'infocus2')],
)
def test_group_name_becomes_a_panel_safe_slug(text: str, slug: str):
    assert parse_group_name(text) == slug


@pytest.mark.parametrize('text', ['', 'a', 'миэт', 'miet!', '-miet', 'miet-', 'a' * 25, 'mi et tv  x' * 3])
def test_unusable_group_names_are_rejected(text: str):
    assert parse_group_name(text) is None


@pytest.mark.parametrize('text', ['203.0.114.10', ' 8.8.8.8 ', '185.10.20.30'])
def test_public_ipv4_is_accepted(text: str):
    assert parse_public_ipv4(text) == text.strip()


@pytest.mark.parametrize(
    'text', ['10.0.0.5', '192.168.1.1', '172.28.0.200', '127.0.0.1', '100.64.0.1', '0.0.0.0', '::1', 'host', '1.2.3']
)
def test_private_and_malformed_addresses_are_rejected(text: str):
    # Иначе через бота можно было бы постучаться во внутренние сети входа или малины.
    assert parse_public_ipv4(text) is None


# --- регистрация -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_server_creates_the_owner_and_node_one(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await register_node(db, _request('203.0.114.10'), LIMITS)

        assert node.ordinal == 1
        assert node.status == ByoNodeStatus.PROVISIONING.value
        assert node.owner.slug == 'miet'
        assert (await find_owner(db, OWNER_TG)).id == node.owner_id


@pytest.mark.asyncio
async def test_next_server_gets_the_next_number(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await register_node(db, _request('203.0.114.10'), LIMITS)
        second = await register_node(db, _request('203.0.114.20', slug=None), LIMITS)

        assert second.ordinal == 2
        assert {node.ordinal for node in second.owner.nodes} == {1, 2}


@pytest.mark.asyncio
async def test_a_server_already_connected_is_refused(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await register_node(db, _request('203.0.114.10'), LIMITS)

        with pytest.raises(OwnerError):
            await register_node(db, NodeRequest(telegram_id=9002, ip='203.0.114.10', slug='other'), LIMITS)


@pytest.mark.asyncio
async def test_no_more_than_the_node_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        for last_octet in (11, 12, 13):
            await register_node(db, _request(f'203.0.114.{last_octet}'), NodeLimits(max_nodes=3, max_new_per_day=10))

        with pytest.raises(OwnerError) as error:
            await register_node(db, _request('203.0.114.14'), NodeLimits(max_nodes=3, max_new_per_day=10))

        assert '3' in str(error.value)


@pytest.mark.asyncio
async def test_no_more_than_the_daily_attempts_even_after_forgetting(monkeypatch):
    # «Забыть» неудачный сервер не обнуляет суточный счётчик — иначе бот стал бы сканером.
    async with memory_session(monkeypatch, TABLES) as db:
        limits = NodeLimits(max_nodes=3, max_new_per_day=2)
        for last_octet in (11, 12):
            node = await register_node(db, _request(f'203.0.114.{last_octet}'), limits)
            node.status = ByoNodeStatus.FAILED.value
            await forget_node(db, node)

        with pytest.raises(OwnerError):
            await register_node(db, _request('203.0.114.13'), limits)


@pytest.mark.asyncio
async def test_attempts_older_than_a_day_do_not_count(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        limits = NodeLimits(max_nodes=3, max_new_per_day=1)
        node = await register_node(db, _request('203.0.114.11'), limits)
        node.created_at = datetime.now(UTC) - timedelta(days=2)
        await db.commit()

        second = await register_node(db, _request('203.0.114.12'), limits)
        assert second.ordinal == 2


@pytest.mark.asyncio
async def test_a_blocked_owner_cannot_add_servers(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await register_node(db, _request('203.0.114.11'), LIMITS)
        node.owner.is_blocked = True
        await db.commit()

        with pytest.raises(OwnerError):
            await register_node(db, _request('203.0.114.12', slug=None), LIMITS)


# --- имя занято ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_taken_slug_is_not_free(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await register_node(db, _request('203.0.114.10'), LIMITS)
        assert not await is_slug_free(db, 'miet')
        assert await is_slug_free(db, 'cheer')


@pytest.mark.asyncio
async def test_leftovers_in_the_bot_make_a_slug_busy(monkeypatch):
    # Лаборатория оставила группу owner:lab-a и тариф «VPN lab-b» — эти имена не выдаём.
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(PromoGroup(name='owner:lab-a'))
        db.add(Tariff(name='VPN lab-b', period_prices={}))
        await db.commit()

        assert not await is_slug_free(db, 'lab-a')
        assert not await is_slug_free(db, 'lab-b')


@pytest.mark.asyncio
async def test_a_new_owner_with_a_busy_slug_is_refused(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await register_node(db, _request('203.0.114.10'), LIMITS)

        with pytest.raises(OwnerError):
            await register_node(db, NodeRequest(telegram_id=9002, ip='203.0.114.20', slug='miet'), LIMITS)


# --- повтор, «забыть», возобновление ------------------------------------------------------


async def _mark(db, node: ByoNode, *names: str) -> None:
    for name in names:
        db.add(ByoStep(node_id=node.id, name=name, status=ByoStepStatus.DONE.value, attempts=1))
    await db.commit()


@pytest.mark.asyncio
async def test_a_server_that_failed_before_dns_can_be_forgotten(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await register_node(db, _request('203.0.114.10'), LIMITS)
        await _mark(db, node, 'ssh')
        node.status = ByoNodeStatus.FAILED.value

        assert await can_forget(db, node)
        await forget_node(db, node)

        assert node.status == ByoNodeStatus.REMOVED.value
        again = await register_node(db, _request('203.0.114.10', slug=None), LIMITS)
        assert again.ordinal == 1, 'забытый сервер не занимает ни IP, ни номер'


@pytest.mark.asyncio
async def test_a_server_with_a_dns_record_cannot_be_forgotten(monkeypatch):
    # Запись DNS или объекты в панели остались бы сиротами — это работа для удаления ноды.
    async with memory_session(monkeypatch, TABLES) as db:
        node = await register_node(db, _request('203.0.114.10'), LIMITS)
        db.add(ByoStep(node_id=node.id, name='dns', status=ByoStepStatus.FAILED.value, attempts=1))
        node.status = ByoNodeStatus.FAILED.value
        await db.commit()

        assert not await can_forget(db, node)
        with pytest.raises(OwnerError):
            await forget_node(db, node)


@pytest.mark.asyncio
async def test_a_working_server_cannot_be_forgotten(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await register_node(db, _request('203.0.114.10'), LIMITS)
        assert not await can_forget(db, node)


@pytest.mark.asyncio
async def test_unfinished_connections_are_found_for_resuming(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        running = await register_node(db, _request('203.0.114.10'), LIMITS)
        failed = await register_node(db, _request('203.0.114.20', slug=None), LIMITS)
        failed.status = ByoNodeStatus.FAILED.value
        await db.commit()

        assert await pending_node_ids(db) == [running.id]


@pytest.mark.asyncio
async def test_a_loaded_node_brings_its_owner_and_siblings(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        first = await register_node(db, _request('203.0.114.10'), LIMITS)
        await register_node(db, _request('203.0.114.20', slug=None), LIMITS)
        db.expunge_all()

        node = await load_node(db, first.id)
        assert node.owner.slug == 'miet'
        assert len(node.owner.nodes) == 2
