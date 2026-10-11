"""Объекты ноды в панели Remnawave и проверка изоляции.

Для ноды бот создаёт config-profile (Trojan+WS, блок приватных IP), саму ноду,
сквад владельца и хост ALLOW_ONLY на этот сквад, затем убеждается, что скваду
видны ровно ноды владельца. Каждый объект создаётся отдельным шагом «найти или
создать»: если ответ панели потерялся, повтор находит уже созданное, а не
плодит дубль. Панель подменена поддельной (``tests/byo/fakes.py``).
"""

from __future__ import annotations

import pytest

from app.byo.panel import (
    IsolationError,
    NodeLayout,
    NodeNotConnectedError,
    PanelProvisioner,
    PanelStateError,
)
from tests.byo.fakes import FakePanel, PanelUnavailableError


LAYOUT = NodeLayout(
    name='o-lab-a-1',
    inbound_tag='o-lab-a-1-tws',
    inbound_port=8501,
    ws_path='/secretpath',
    fqdn='abc123.vpn.miet-tv.ru',
    address='203.0.113.10',
    host_remark='lab-a 1',
    proxy_url='socks5://xray:1080',
)
SECOND = LAYOUT._replace(
    name='o-lab-a-2',
    inbound_tag='o-lab-a-2-tws',
    fqdn='def456.vpn.miet-tv.ru',
    address='203.0.113.20',
    host_remark='lab-a 2',
)


class Sleeps:
    """Вместо настоящего ожидания — запись пауз."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _provisioner(panel: FakePanel, sleeps: Sleeps | None = None) -> PanelProvisioner:
    return PanelProvisioner(panel, sleep=sleeps or Sleeps())


async def _node_with_squad(provisioner: PanelProvisioner, layout: NodeLayout, squad_uuid: str | None = None):
    profile = await provisioner.ensure_profile(layout)
    node_uuid = await provisioner.ensure_node(layout, profile)
    squad_uuid = await provisioner.ensure_squad('o-lab-a', profile.inbound_uuid, squad_uuid=squad_uuid)
    return profile, node_uuid, squad_uuid


# --- профиль ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_inbound_is_trojan_over_websocket():
    panel = FakePanel()
    await _provisioner(panel).ensure_profile(LAYOUT)

    inbound = panel.body_of('POST', '/api/config-profiles')['config']['inbounds'][0]
    assert inbound['protocol'] == 'trojan'
    assert inbound['tag'] == 'o-lab-a-1-tws'
    assert inbound['port'] == 8501
    assert inbound['streamSettings']['network'] == 'ws'
    assert inbound['streamSettings']['wsSettings']['path'] == '/secretpath'


@pytest.mark.asyncio
async def test_inbound_listens_only_on_localhost_behind_caddy():
    panel = FakePanel()
    await _provisioner(panel).ensure_profile(LAYOUT)

    inbound = panel.body_of('POST', '/api/config-profiles')['config']['inbounds'][0]
    assert inbound['listen'] == '127.0.0.1'


@pytest.mark.asyncio
async def test_websocket_host_matches_the_domain_caddy_serves():
    panel = FakePanel()
    await _provisioner(panel).ensure_profile(LAYOUT)

    ws = panel.body_of('POST', '/api/config-profiles')['config']['inbounds'][0]['streamSettings']['wsSettings']
    assert ws['host'] == 'abc123.vpn.miet-tv.ru'


@pytest.mark.asyncio
async def test_profile_blocks_private_addresses_before_direct():
    panel = FakePanel()
    await _provisioner(panel).ensure_profile(LAYOUT)

    rules = panel.body_of('POST', '/api/config-profiles')['config']['routing']['rules']
    block_index = next(i for i, rule in enumerate(rules) if rule.get('outboundTag') == 'BLOCK')
    direct_index = next(i for i, rule in enumerate(rules) if rule.get('outboundTag') == 'DIRECT')
    assert block_index < direct_index, 'блок приватных адресов должен стоять раньше DIRECT'
    assert {'10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'}.issubset(rules[block_index]['ip'])


@pytest.mark.asyncio
async def test_profile_is_found_after_a_lost_response():
    panel = FakePanel()
    panel.lose_response_of.add('POST /api/config-profiles')
    provisioner = _provisioner(panel)

    with pytest.raises(PanelUnavailableError):
        await provisioner.ensure_profile(LAYOUT)
    profile = await provisioner.ensure_profile(LAYOUT)

    assert len(panel.profiles) == 1
    assert profile.uuid in panel.profiles
    assert profile.inbound_uuid == panel.profiles[profile.uuid]['inbounds'][0]['uuid']


@pytest.mark.asyncio
async def test_profile_without_the_expected_inbound_is_refused():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    await provisioner.ensure_profile(LAYOUT._replace(inbound_tag='something-else'))

    with pytest.raises(PanelStateError):
        await provisioner.ensure_profile(LAYOUT)


# --- нода ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_uses_the_profile_and_its_inbound():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile = await provisioner.ensure_profile(LAYOUT)
    await provisioner.ensure_node(LAYOUT, profile)

    node = panel.body_of('POST', '/api/nodes')
    assert node['name'] == 'o-lab-a-1'
    assert node['address'] == '203.0.113.10'
    assert node['port'] == 2222
    assert node['configProfile'] == {
        'activeConfigProfileUuid': profile.uuid,
        'activeInbounds': [profile.inbound_uuid],
    }


@pytest.mark.asyncio
async def test_panel_reaches_the_node_through_the_egress_proxy():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    await provisioner.ensure_node(LAYOUT, await provisioner.ensure_profile(LAYOUT))
    assert panel.body_of('POST', '/api/nodes')['proxyUrl'] == 'socks5://xray:1080'


@pytest.mark.asyncio
async def test_no_proxy_means_the_field_is_omitted():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    layout = LAYOUT._replace(proxy_url=None)
    await provisioner.ensure_node(layout, await provisioner.ensure_profile(layout))
    assert 'proxyUrl' not in panel.body_of('POST', '/api/nodes')


@pytest.mark.asyncio
async def test_node_is_found_after_a_lost_response():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile = await provisioner.ensure_profile(LAYOUT)
    panel.lose_response_of.add('POST /api/nodes')

    with pytest.raises(PanelUnavailableError):
        await provisioner.ensure_node(LAYOUT, profile)
    node_uuid = await provisioner.ensure_node(LAYOUT, profile)

    assert list(panel.nodes) == [node_uuid]


@pytest.mark.asyncio
async def test_a_stale_node_with_the_same_name_and_another_address_is_refused():
    # Например, старая нода с тем же номером не была удалена из панели.
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile = await provisioner.ensure_profile(LAYOUT)
    await provisioner.ensure_node(LAYOUT._replace(address='198.51.100.99'), profile)

    with pytest.raises(PanelStateError):
        await provisioner.ensure_node(LAYOUT, profile)


# --- подключение панели к ноде ----------------------------------------------------


@pytest.mark.asyncio
async def test_waits_until_the_panel_connects_to_the_node():
    panel = FakePanel(polls_before_connect=3)
    sleeps = Sleeps()
    provisioner = _provisioner(panel, sleeps)
    node_uuid = await provisioner.ensure_node(LAYOUT, await provisioner.ensure_profile(LAYOUT))

    await provisioner.wait_connected(node_uuid)

    assert len(sleeps.calls) == 3


@pytest.mark.asyncio
async def test_gives_up_with_the_panel_status_message():
    panel = FakePanel(never_connects=True)
    sleeps = Sleeps()
    provisioner = _provisioner(panel, sleeps)
    node_uuid = await provisioner.ensure_node(LAYOUT, await provisioner.ensure_profile(LAYOUT))

    with pytest.raises(NodeNotConnectedError) as error:
        await provisioner.wait_connected(node_uuid)

    assert 'ETIMEDOUT' in str(error.value)
    assert sum(sleeps.calls) >= 60, 'ждём хотя бы минуту, прежде чем сдаться'


# --- сквад владельца -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_node_creates_the_owner_squad_with_its_inbound():
    panel = FakePanel()
    profile, _node, squad_uuid = await _node_with_squad(_provisioner(panel), LAYOUT)

    assert panel.squads[squad_uuid] == {'name': 'o-lab-a', 'inbounds': [profile.inbound_uuid]}


@pytest.mark.asyncio
async def test_second_node_adds_its_inbound_to_the_same_squad():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    first, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)

    second, _node, again = await _node_with_squad(provisioner, SECOND, squad_uuid=squad_uuid)

    assert again == squad_uuid
    assert len(panel.squads) == 1
    assert panel.squads[squad_uuid]['inbounds'] == [first.inbound_uuid, second.inbound_uuid]


@pytest.mark.asyncio
async def test_an_inbound_already_in_the_squad_is_not_patched_again():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)

    await provisioner.ensure_squad('o-lab-a', profile.inbound_uuid, squad_uuid=squad_uuid)

    assert panel.calls_to('PATCH', '/api/internal-squads') == []


@pytest.mark.asyncio
async def test_squad_is_found_by_name_after_a_lost_response():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile = await provisioner.ensure_profile(LAYOUT)
    panel.lose_response_of.add('POST /api/internal-squads')

    with pytest.raises(PanelUnavailableError):
        await provisioner.ensure_squad('o-lab-a', profile.inbound_uuid)
    squad_uuid = await provisioner.ensure_squad('o-lab-a', profile.inbound_uuid)

    assert list(panel.squads) == [squad_uuid]


@pytest.mark.asyncio
async def test_a_foreign_squad_with_the_same_name_is_never_adopted():
    # Владелец сам выбирает имя группы: «lab-a» не должно подмешать его ноду в чужой сквад.
    panel = FakePanel()
    provisioner = _provisioner(panel)
    foreign = await provisioner.ensure_profile(LAYOUT._replace(name='lab-a-profile', inbound_tag='lab-a-tws'))
    foreign_squad = await provisioner.ensure_squad('o-lab-a', foreign.inbound_uuid)
    ours = await provisioner.ensure_profile(LAYOUT)

    with pytest.raises(PanelStateError):
        await provisioner.ensure_squad('o-lab-a', ours.inbound_uuid)

    assert panel.squads[foreign_squad]['inbounds'] == [foreign.inbound_uuid]
    assert panel.calls_to('PATCH', '/api/internal-squads') == []


# --- хост -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_allows_only_the_owner_squad():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)

    await provisioner.ensure_host(LAYOUT, profile, squad_uuid)

    host = panel.body_of('POST', '/api/hosts')
    assert host['internalSquads'] == {'mode': 'ALLOW_ONLY', 'squads': [squad_uuid]}
    assert host['inbound'] == {'configProfileUuid': profile.uuid, 'configProfileInboundUuid': profile.inbound_uuid}


@pytest.mark.asyncio
async def test_clients_reach_the_node_by_domain_over_tls_on_443():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)

    await provisioner.ensure_host(LAYOUT, profile, squad_uuid)

    host = panel.body_of('POST', '/api/hosts')
    assert host['address'] == 'abc123.vpn.miet-tv.ru'
    assert host['port'] == 443
    assert host['securityLayer'] == 'TLS'
    assert host['sni'] == 'abc123.vpn.miet-tv.ru'
    assert host['host'] == 'abc123.vpn.miet-tv.ru'
    assert host['path'] == '/secretpath'
    assert host['remark'] == 'lab-a 1'


@pytest.mark.asyncio
async def test_host_is_found_after_a_lost_response():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)
    panel.lose_response_of.add('POST /api/hosts')

    with pytest.raises(PanelUnavailableError):
        await provisioner.ensure_host(LAYOUT, profile, squad_uuid)
    host_uuid = await provisioner.ensure_host(LAYOUT, profile, squad_uuid)

    assert list(panel.hosts) == [host_uuid]


# --- ключ для ноды --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_key_comes_from_keygen():
    panel = FakePanel()
    assert await _provisioner(panel).generate_secret_key() == 'PANELSECRETKEY'


# --- изоляция -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolation_holds_when_the_squad_sees_exactly_the_owner_nodes():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    _profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)
    await _node_with_squad(provisioner, SECOND, squad_uuid=squad_uuid)

    await provisioner.assert_isolated(squad_uuid, expected_nodes={'o-lab-a-1', 'o-lab-a-2'})


@pytest.mark.asyncio
async def test_isolation_fails_when_a_foreign_node_is_visible():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)
    # Чужая нода с тем же инбаундом — ровно то, от чего защищает проверка.
    panel.add_foreign_node('o-lab-b-1', profile.inbound_uuid)

    with pytest.raises(IsolationError):
        await provisioner.assert_isolated(squad_uuid, expected_nodes={'o-lab-a-1'})


@pytest.mark.asyncio
async def test_isolation_fails_when_an_owner_node_is_missing():
    panel = FakePanel()
    provisioner = _provisioner(panel)
    _profile, _node, squad_uuid = await _node_with_squad(provisioner, LAYOUT)

    with pytest.raises(IsolationError):
        await provisioner.assert_isolated(squad_uuid, expected_nodes={'o-lab-a-1', 'o-lab-a-2'})
