"""Фоновое подключение серверов в боте.

Обработчик лишь создаёт ноду и просит ``ByoRuntime`` подключить её: подключение
идёт фоновой задачей, владелец видит прогресс и итог, админы — уведомление.
После перезапуска бота незаконченные подключения продолжаются сами. Одна нода не
подключается дважды одновременно. Внешний мир подменён (``tests/byo/fakes.py``),
БД — файловая SQLite (у задач свои сессии).
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.byo.connect import ConnectDeps, ConnectSettings
from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner, ByoStep
from app.byo.owners import NodeLimits, NodeRequest, load_node, register_node
from app.byo.panel import PanelProvisioner
from app.byo.runtime import ByoRuntime
from app.byo.settings import ByoSettings
from app.database.models import Base, PromoGroup, Tariff
from tests.byo.fakes import CountingRng, FakeBot, FakeBotObjects, FakeDns, FakePanel, FakeServer, FakeSsh
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


OWNER_TG = 7001
ADMIN_TG = 1
# Таблицы бота, которые читает проверка свободного имени группы.
TABLES = [ByoOwner.__table__, ByoNode.__table__, ByoStep.__table__, PromoGroup.__table__, Tariff.__table__]
CONNECT = ConnectSettings(zone='vpn.miet-tv.ru', dns_domain='miet-tv.ru', gateway_ip='194.87.254.244', proxy_url=None)


async def _no_sleep(_seconds: float) -> None:
    return None


class World:
    def __init__(self, *, server: FakeServer | None = None, panel: FakePanel | None = None) -> None:
        self.panel = panel or FakePanel()
        self.ssh = FakeSsh(server or FakeServer())
        self.dns = FakeDns()
        self.bot_objects = FakeBotObjects()
        self.rng = CountingRng()

    @contextlib.asynccontextmanager
    async def deps(self, db):
        yield ConnectDeps(
            settings=CONNECT,
            panel=PanelProvisioner(self.panel, sleep=_no_sleep),
            dns=self.dns,
            open_ssh=self.ssh,
            bot=self.bot_objects,
            save=db.commit,
            rng=self.rng,
            sleep=_no_sleep,
        )


@contextlib.asynccontextmanager
async def _database(tmp_path, monkeypatch):
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "byo.db"}')
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=TABLES))
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


def _settings(tmp_path, **overrides) -> ByoSettings:
    values = {
        'enabled': True,
        'node_zone': 'vpn.miet-tv.ru',
        'dns_domain': 'miet-tv.ru',
        'gateway_ip': '194.87.254.244',
        'egress_proxy_url': 'socks5://xray:1080',
        'regru_username': 'api-user',
        'regru_password': 'api-pass',
        'ssh_key_path': str(tmp_path / 'keys' / 'id_ed25519'),
    }
    return ByoSettings(_env_file=None, **{**values, **overrides})


def _runtime(tmp_path, maker, world: World, **overrides) -> ByoRuntime:
    return ByoRuntime(
        _settings(tmp_path, **overrides),
        session_factory=maker,
        deps_factory=world.deps,
        admin_ids=lambda: [ADMIN_TG],
    )


async def _new_node(maker, ip: str = '203.0.114.10') -> int:
    async with maker() as db:
        node = await register_node(db, NodeRequest(OWNER_TG, ip, 'miet'), NodeLimits(3, 3))
        return node.id


async def _status(maker, node_id: int) -> str:
    async with maker() as db:
        return (await load_node(db, node_id)).status


def _texts_to(bot: FakeBot, chat_id: int) -> list[str]:
    return [m['text'] for m in bot.sent if m['chat_id'] == chat_id] + [
        m['text'] for m in bot.edited if m['chat_id'] == chat_id
    ]


@pytest.mark.asyncio
async def test_a_launched_node_connects_in_the_background(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        world, bot = World(), FakeBot()
        runtime = _runtime(tmp_path, maker, world)
        await runtime.start(bot, engine=engine)
        node_id = await _new_node(maker)

        await runtime.launch(node_id, chat_id=OWNER_TG, message_id=None)

        assert await _status(maker, node_id) == ByoNodeStatus.ONLINE.value
        assert any('LABA0000CODE' in text for text in _texts_to(bot, OWNER_TG))
        assert any('подключён' in text and '203.0.114.10' in text for text in _texts_to(bot, ADMIN_TG))


@pytest.mark.asyncio
async def test_a_failed_node_offers_a_retry_and_tells_the_admins(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        world, bot = World(server=FakeServer(country='RU')), FakeBot()
        runtime = _runtime(tmp_path, maker, world)
        await runtime.start(bot, engine=engine)
        node_id = await _new_node(maker)

        await runtime.launch(node_id, chat_id=OWNER_TG, message_id=None)

        assert await _status(maker, node_id) == ByoNodeStatus.FAILED.value
        assert any('России' in text and 'byo:retry' not in text for text in _texts_to(bot, OWNER_TG))
        assert any('Проверка сервера' in text for text in _texts_to(bot, ADMIN_TG))


@pytest.mark.asyncio
async def test_a_node_is_never_connected_twice_at_once(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        world, bot = World(), FakeBot()
        runtime = _runtime(tmp_path, maker, world)
        await runtime.start(bot, engine=engine)
        node_id = await _new_node(maker)

        first = runtime.launch(node_id, chat_id=OWNER_TG, message_id=None)
        second = runtime.launch(node_id, chat_id=OWNER_TG, message_id=None)
        assert runtime.is_running(node_id)
        await first

        assert second is None
        assert not runtime.is_running(node_id)
        assert world.ssh.opened == ['203.0.114.10']


@pytest.mark.asyncio
async def test_unfinished_connections_resume_after_a_restart(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        node_id = await _new_node(maker)  # бот упал, пока нода подключалась
        world, bot = World(), FakeBot()
        runtime = _runtime(tmp_path, maker, world)

        await runtime.start(bot, engine=engine)
        await runtime.wait_idle()

        assert await _status(maker, node_id) == ByoNodeStatus.ONLINE.value
        assert _texts_to(bot, OWNER_TG), 'владелец узнаёт, что подключение продолжилось'


@pytest.mark.asyncio
async def test_an_unexpected_error_does_not_leave_the_node_hanging(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        bot = FakeBot()

        @contextlib.asynccontextmanager
        async def broken_deps(_db):
            raise RuntimeError('панель недоступна')
            yield  # pragma: no cover

        runtime = ByoRuntime(
            _settings(tmp_path), session_factory=maker, deps_factory=broken_deps, admin_ids=lambda: [ADMIN_TG]
        )
        await runtime.start(bot, engine=engine)
        node_id = await _new_node(maker)

        await runtime.launch(node_id, chat_id=OWNER_TG, message_id=None)

        assert await _status(maker, node_id) == ByoNodeStatus.FAILED.value
        assert any('внутренняя ошибка' in text for text in _texts_to(bot, OWNER_TG))


@pytest.mark.asyncio
async def test_a_disabled_module_does_nothing_at_start(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        await _new_node(maker)
        world, bot = World(), FakeBot()
        runtime = _runtime(tmp_path, maker, world, enabled=False)

        await runtime.start(bot, engine=engine)

        assert not runtime.is_active
        assert world.ssh.opened == []
        assert not (tmp_path / 'keys').exists()


@pytest.mark.asyncio
async def test_owners_get_our_public_key_once_started(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        runtime = _runtime(tmp_path, maker, World())
        await runtime.start(FakeBot(), engine=engine)

        assert runtime.is_active
        assert runtime.public_key().startswith('ssh-ed25519 ')


@pytest.mark.asyncio
async def test_a_broken_start_leaves_the_bot_running_without_the_module(tmp_path, monkeypatch):
    # Ошибка подготовки (ключ, таблицы) не должна ронять запуск всего бота.
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        (tmp_path / 'keys').write_text('это файл, а не папка')
        runtime = _runtime(tmp_path, maker, World())

        await runtime.start(FakeBot(), engine=engine)

        assert not runtime.is_active


@pytest.mark.asyncio
async def test_a_vanished_node_is_skipped_quietly(tmp_path, monkeypatch):
    async with _database(tmp_path, monkeypatch) as (engine, maker):
        world, bot = World(), FakeBot()
        runtime = _runtime(tmp_path, maker, world)
        await runtime.start(bot, engine=engine)

        await runtime.launch(404, chat_id=OWNER_TG, message_id=None)

        assert bot.sent == []
        assert world.ssh.opened == []
