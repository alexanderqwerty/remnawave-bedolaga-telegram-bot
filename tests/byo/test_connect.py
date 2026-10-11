"""Подключение сервера владельца — все шаги одной цепочкой.

``connect_node`` проводит VPS через шаги плана: SSH → проверка → секреты → DNS →
профиль → установка → сертификат → нода → подключение → сквад → хост →
изоляция → объекты в боте. Каждый шаг пишется в журнал, повтор после сбоя
продолжает с упавшего шага и не переделывает сделанное. Сервер, панель, reg.ru
и бот подменены (``tests/byo/fakes.py``), поэтому проверяется сама цепочка.
"""

from __future__ import annotations

import base64

import pytest

from app.byo.connect import STEP_NAMES, ConnectDeps, ConnectSettings, connect_node
from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner
from app.byo.panel import PanelProvisioner
from app.byo.provisioning import ProvisioningError
from tests.byo.fakes import (
    CountingRng,
    FakeBotObjects,
    FakeDns,
    FakePanel,
    FakeServer,
    FakeSsh,
    MemoryJournal,
)


SETTINGS = ConnectSettings(
    zone='vpn.miet-tv.ru',
    dns_domain='miet-tv.ru',
    gateway_ip='194.87.254.244',
    proxy_url='socks5://xray:1080',
)
NODE_IP = '203.0.113.10'


class Saves:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


class NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class World:
    """Всё окружение одного подключения: сервер, панель, reg.ru, бот, журнал."""

    def __init__(self, *, server: FakeServer | None = None, panel: FakePanel | None = None, dns: FakeDns | None = None):
        self.server = server or FakeServer()
        self.ssh = FakeSsh(self.server)
        self.panel = panel or FakePanel()
        self.dns = dns or FakeDns()
        self.bot = FakeBotObjects()
        self.journal = MemoryJournal()
        self.saves = Saves()
        self.sleeps = NoSleep()
        self.progress: list[str] = []
        # Один генератор на всё окружение: у каждой ноды свои label и путь.
        self.rng = CountingRng()
        self.owner = ByoOwner(telegram_id=7001, slug='lab-a')
        self.node = self.add_node(ordinal=1, ip=NODE_IP)

    def add_node(self, *, ordinal: int, ip: str) -> ByoNode:
        node = ByoNode(ordinal=ordinal, ip=ip, status=ByoNodeStatus.PROVISIONING.value)
        node.owner = self.owner
        return node

    def deps(self) -> ConnectDeps:
        return ConnectDeps(
            settings=SETTINGS,
            panel=PanelProvisioner(self.panel, sleep=self.sleeps),
            dns=self.dns,
            open_ssh=self.ssh,
            bot=self.bot,
            save=self.saves,
            rng=self.rng,
            sleep=self.sleeps,
        )

    async def report(self, step: str) -> None:
        self.progress.append(step)

    async def connect(self, node: ByoNode | None = None, journal: MemoryJournal | None = None) -> None:
        await connect_node(node or self.node, self.deps(), journal or self.journal, progress=self.report)


# --- путь без сбоев ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_server_goes_through_every_step_in_order():
    world = World()
    await world.connect()

    assert world.progress == list(STEP_NAMES)
    assert world.journal.done() == list(STEP_NAMES)
    assert world.node.status == ByoNodeStatus.ONLINE.value


@pytest.mark.asyncio
async def test_the_plan_order_is_kept():
    # DNS заводится раньше установки, чтобы успел разойтись, пока ставится Docker;
    # нода регистрируется в панели только когда домен уже отдаёт сертификат.
    order = list(STEP_NAMES)
    assert order.index('dns') < order.index('install') < order.index('tls') < order.index('node')
    assert order.index('online') < order.index('squad') < order.index('host') < order.index('isolation')
    assert order[-1] == 'bot'


@pytest.mark.asyncio
async def test_node_names_and_domain_are_saved_on_the_node():
    world = World()
    await world.connect()

    node = world.node
    assert node.fqdn == '00000001.vpn.miet-tv.ru'
    assert node.ws_path == '/000000000002'
    assert node.inbound_tag == 'o-lab-a-1-tws'
    assert 8500 <= node.inbound_port < 9500


@pytest.mark.asyncio
async def test_dns_points_the_node_domain_at_the_server():
    world = World()
    await world.connect()
    assert world.dns.added == [('miet-tv.ru', '00000001.vpn', NODE_IP)]


@pytest.mark.asyncio
async def test_panel_objects_are_recorded_for_later_removal():
    world = World()
    await world.connect()

    node, panel = world.node, world.panel
    assert node.config_profile_uuid in panel.profiles
    assert node.node_uuid in panel.nodes
    assert node.host_uuid in panel.hosts
    assert world.owner.squad_uuid in panel.squads
    assert panel.squads[world.owner.squad_uuid]['inbounds'] == [node.inbound_uuid]


@pytest.mark.asyncio
async def test_node_is_registered_with_the_server_address_and_the_egress_proxy():
    world = World()
    await world.connect()

    registered = world.panel.nodes[world.node.node_uuid]
    assert registered['name'] == 'o-lab-a-1'
    assert registered['address'] == NODE_IP
    assert registered['proxyUrl'] == 'socks5://xray:1080'


@pytest.mark.asyncio
async def test_host_serves_the_node_domain_only_to_the_owner_squad():
    world = World()
    await world.connect()

    host = world.panel.hosts[world.node.host_uuid]
    assert host['address'] == world.node.fqdn
    assert host['internalSquads'] == {'mode': 'ALLOW_ONLY', 'squads': [world.owner.squad_uuid]}


@pytest.mark.asyncio
async def test_bot_objects_are_made_last_for_the_owner():
    world = World()
    await world.connect()
    assert world.bot.ensured == ['lab-a']


@pytest.mark.asyncio
async def test_install_writes_the_panel_key_only_into_the_node_compose():
    world = World()
    await world.connect()

    files = world.server.written_files()
    assert 'SECRET_KEY=PANELSECRETKEY' in files['/opt/remnanode/docker-compose.yml']
    assert 'ufw allow from 194.87.254.244 to any port 2222' in [command for command, _ in world.server.checked]


@pytest.mark.asyncio
async def test_the_panel_key_is_never_stored():
    world = World()
    await world.connect()

    stored = [*vars(world.node).values(), *vars(world.owner).values()]
    assert not any('PANELSECRETKEY' in str(value) for value in stored)


@pytest.mark.asyncio
async def test_state_is_saved_as_the_steps_go():
    world = World()
    await world.connect()
    assert world.saves.count >= 6, 'каждый шаг, меняющий строки, сохраняет их сразу'


# --- проверка сервера -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_server_in_russia_is_rejected_before_anything_is_created():
    world = World(server=FakeServer(country='RU'))

    with pytest.raises(ProvisioningError) as error:
        await world.connect()

    assert error.value.step == 'check'
    assert 'России' in world.journal.detail['check']
    assert world.dns.added == []
    assert world.panel.calls == []
    assert world.node.status == ByoNodeStatus.FAILED.value


@pytest.mark.asyncio
async def test_a_login_without_root_is_rejected():
    world = World(server=FakeServer(uid='1000'))

    with pytest.raises(ProvisioningError) as error:
        await world.connect()

    assert error.value.step == 'ssh'
    assert 'root' in world.journal.detail['ssh']


# --- DNS ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_existing_record_for_this_server_is_not_added_again():
    world = World(dns=FakeDns({'00000001.vpn': [NODE_IP]}))
    await world.connect()
    assert world.dns.added == []


@pytest.mark.asyncio
async def test_a_name_taken_by_another_address_stops_the_connection():
    world = World(dns=FakeDns({'00000001.vpn': ['198.51.100.7']}))

    with pytest.raises(ProvisioningError) as error:
        await world.connect()

    assert error.value.step == 'dns'
    assert world.panel.calls == []


def test_the_node_zone_must_live_inside_the_dns_domain():
    with pytest.raises(ValueError):
        ConnectSettings(zone='vpn.example.org', dns_domain='miet-tv.ru', gateway_ip='194.87.254.244', proxy_url=None)


# --- установка и сертификат -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_install_command_is_named_but_not_quoted():
    world = World(server=FakeServer(fail_on='docker compose -f /opt/remnanode'))

    with pytest.raises(ProvisioningError) as error:
        await world.connect()

    detail = world.journal.detail['install']
    assert error.value.step == 'install'
    assert 'запуск ноды' in detail
    assert 'PANELSECRETKEY' not in detail
    assert base64.b64encode(b'SECRET_KEY').decode()[:8] not in detail


@pytest.mark.asyncio
async def test_the_node_waits_for_its_certificate():
    world = World(server=FakeServer(tls_failures=3))
    await world.connect()

    assert world.server.ran(f'https://{world.node.fqdn}/') == 4
    assert world.node.status == ByoNodeStatus.ONLINE.value


@pytest.mark.asyncio
async def test_a_certificate_that_never_comes_fails_the_tls_step():
    world = World(server=FakeServer(tls_failures=10_000))

    with pytest.raises(ProvisioningError) as error:
        await world.connect()

    assert error.value.step == 'tls'
    assert world.node.fqdn in world.journal.detail['tls']
    assert world.panel.calls_to('POST', '/api/nodes') == []
    assert sum(world.sleeps.calls) >= 600, 'сертификату дают хотя бы 10 минут'


# --- повтор после сбоя ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retry_continues_from_the_failed_step():
    panel = FakePanel(never_connects=True)
    world = World(panel=panel)
    with pytest.raises(ProvisioningError) as error:
        await world.connect()
    assert error.value.step == 'online'

    panel._never_connects = False  # хостер починил сеть
    world.progress.clear()
    await world.connect()

    assert world.progress == ['online', 'squad', 'host', 'isolation', 'bot']
    assert len(panel.calls_to('POST', '/api/nodes')) == 1
    assert world.server.ran('get.docker.com') == 1
    assert len(world.dns.added) == 1
    assert world.node.status == ByoNodeStatus.ONLINE.value


@pytest.mark.asyncio
async def test_a_retry_after_the_server_steps_does_not_reconnect_by_ssh():
    panel = FakePanel(never_connects=True)
    world = World(panel=panel)
    with pytest.raises(ProvisioningError):
        await world.connect()

    panel._never_connects = False
    await world.connect()

    assert world.ssh.opened == [NODE_IP], 'всё, что требует SSH, уже сделано'


# --- вторая нода владельца -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_node_joins_the_owner_squad():
    world = World()
    await world.connect()
    first = world.node

    second = world.add_node(ordinal=2, ip='203.0.113.20')
    await world.connect(second, MemoryJournal())

    squad = world.panel.squads[world.owner.squad_uuid]
    assert len(world.panel.squads) == 1
    assert squad['inbounds'] == [first.inbound_uuid, second.inbound_uuid]
    assert second.inbound_tag == 'o-lab-a-2-tws'
    assert second.status == ByoNodeStatus.ONLINE.value
    assert world.bot.ensured == ['lab-a', 'lab-a']


# --- изоляция ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_squad_seeing_a_foreign_node_stops_before_the_bot_objects():
    panel = FakePanel()
    panel.extra_visible_nodes.append('o-lab-b-1')
    world = World(panel=panel)

    with pytest.raises(ProvisioningError) as error:
        await world.connect()

    assert error.value.step == 'isolation'
    assert world.bot.ensured == []
    assert world.node.status == ByoNodeStatus.FAILED.value
