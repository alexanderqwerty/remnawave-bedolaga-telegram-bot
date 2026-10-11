"""Создание объектов ноды в панели Remnawave и проверка изоляции.

Для одной ноды бот создаёт: config-profile с инбаундом Trojan+WS и блоком
приватных IP, ноду, сквад из одного инбаунда, хост в режиме ALLOW_ONLY на этот
сквад. Потом убеждается, что скваду доступна ровно эта нода. Панель подменена
поддельным запросчиком, поэтому проверяются тела запросов и порядок, без сети.
"""

from __future__ import annotations

import pytest

from app.byo.panel import PanelProvisioner, PanelRequester


PROFILE_UUID = 'prof-1'
INBOUND_UUID = 'inb-1'
NODE_UUID = 'node-1'
SQUAD_UUID = 'squad-1'
HOST_UUID = 'host-1'


class FakePanel(PanelRequester):
    """Отдаёт заготовленные ответы и записывает все вызовы."""

    def __init__(self, accessible_nodes: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._accessible_nodes = accessible_nodes if accessible_nodes is not None else [{'nodeName': 'o-lab-a-1'}]

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))
        if path == '/api/config-profiles':
            return {
                'uuid': PROFILE_UUID,
                'inbounds': [{'uuid': INBOUND_UUID, 'tag': body['config']['inbounds'][0]['tag']}],
            }
        if path == '/api/nodes':
            return {'uuid': NODE_UUID, 'isConnected': True}
        if path == '/api/internal-squads':
            return {'uuid': SQUAD_UUID}
        if path == '/api/hosts':
            return {'uuid': HOST_UUID}
        if path.endswith('/accessible-nodes'):
            return {'accessibleNodes': self._accessible_nodes}
        raise AssertionError(f'поддельная панель не знает путь: {path}')

    def body_of(self, path: str) -> dict:
        return next(body for method, called, body in self.calls if called == path)


SPEC = {
    'name': 'o-lab-a',
    'inbound_tag': 'o-lab-a-1-tws',
    'inbound_port': 8501,
    'ws_path': '/secretpath',
    'fqdn': 'abc123.vpn.miet-tv.ru',
    'node_name': 'lab-node-a',
    'node_address': '203.0.113.10',
    'host_remark': 'owner lab-a',
    'proxy_url': 'socks5://xray:1080',
}


async def _provision(panel: FakePanel) -> dict:
    return await PanelProvisioner(panel).create_node(SPEC)


@pytest.mark.asyncio
async def test_profile_inbound_is_trojan_over_websocket():
    panel = FakePanel()
    await _provision(panel)

    inbound = panel.body_of('/api/config-profiles')['config']['inbounds'][0]
    assert inbound['protocol'] == 'trojan'
    assert inbound['tag'] == 'o-lab-a-1-tws'
    assert inbound['streamSettings']['network'] == 'ws'
    assert inbound['streamSettings']['wsSettings']['path'] == '/secretpath'


@pytest.mark.asyncio
async def test_inbound_listens_only_on_localhost_behind_caddy():
    # Снаружи нода доступна только через Caddy на 443; сам инбаунд наружу не смотрит.
    panel = FakePanel()
    await _provision(panel)

    inbound = panel.body_of('/api/config-profiles')['config']['inbounds'][0]
    assert inbound['listen'] == '127.0.0.1'


@pytest.mark.asyncio
async def test_profile_blocks_private_addresses_before_direct():
    panel = FakePanel()
    await _provision(panel)

    rules = panel.body_of('/api/config-profiles')['config']['routing']['rules']
    block_index = next(i for i, r in enumerate(rules) if r.get('outboundTag') == 'BLOCK')
    direct_index = next(i for i, r in enumerate(rules) if r.get('outboundTag') == 'DIRECT')
    assert block_index < direct_index, 'блок приватных адресов должен стоять раньше DIRECT'
    block_rule = rules[block_index]
    assert any(net.startswith('10.') or net.startswith('192.168') or net.startswith('127.') for net in block_rule['ip'])


@pytest.mark.asyncio
async def test_node_uses_the_new_profile_and_its_inbound():
    panel = FakePanel()
    await _provision(panel)

    node = panel.body_of('/api/nodes')
    assert node['address'] == '203.0.113.10'
    assert node['configProfile']['activeConfigProfileUuid'] == PROFILE_UUID
    assert node['configProfile']['activeInbounds'] == [INBOUND_UUID]


@pytest.mark.asyncio
async def test_squad_contains_only_this_inbound():
    panel = FakePanel()
    await _provision(panel)
    assert panel.body_of('/api/internal-squads')['inbounds'] == [INBOUND_UUID]


@pytest.mark.asyncio
async def test_host_allows_only_the_owner_squad():
    panel = FakePanel()
    await _provision(panel)

    host = panel.body_of('/api/hosts')
    assert host['internalSquads'] == {'mode': 'ALLOW_ONLY', 'squads': [SQUAD_UUID]}


@pytest.mark.asyncio
async def test_clients_reach_the_node_by_domain_over_tls_on_443():
    panel = FakePanel()
    await _provision(panel)

    host = panel.body_of('/api/hosts')
    assert host['address'] == 'abc123.vpn.miet-tv.ru'
    assert host['port'] == 443
    assert host['securityLayer'] == 'TLS'
    assert host['sni'] == 'abc123.vpn.miet-tv.ru'
    assert host['host'] == 'abc123.vpn.miet-tv.ru'
    assert host['path'] == '/secretpath'


@pytest.mark.asyncio
async def test_websocket_host_matches_the_domain_caddy_serves():
    panel = FakePanel()
    await _provision(panel)

    ws = panel.body_of('/api/config-profiles')['config']['inbounds'][0]['streamSettings']['wsSettings']
    assert ws['host'] == 'abc123.vpn.miet-tv.ru'


@pytest.mark.asyncio
async def test_panel_reaches_the_node_through_the_egress_proxy():
    panel = FakePanel()
    await _provision(panel)
    assert panel.body_of('/api/nodes')['proxyUrl'] == 'socks5://xray:1080'


@pytest.mark.asyncio
async def test_no_proxy_means_the_field_is_omitted():
    panel = FakePanel()
    await PanelProvisioner(panel).create_node({**SPEC, 'proxy_url': None})
    assert 'proxyUrl' not in panel.body_of('/api/nodes')


@pytest.mark.asyncio
async def test_objects_are_created_in_dependency_order():
    panel = FakePanel()
    await _provision(panel)

    order = [
        path
        for _method, path, _body in panel.calls
        if path
        in (
            '/api/config-profiles',
            '/api/nodes',
            '/api/internal-squads',
            '/api/hosts',
        )
    ]
    assert order == ['/api/config-profiles', '/api/nodes', '/api/internal-squads', '/api/hosts']


@pytest.mark.asyncio
async def test_create_node_returns_every_uuid_for_the_journal():
    panel = FakePanel()
    result = await _provision(panel)

    assert result == {
        'config_profile_uuid': PROFILE_UUID,
        'inbound_uuid': INBOUND_UUID,
        'node_uuid': NODE_UUID,
        'squad_uuid': SQUAD_UUID,
        'host_uuid': HOST_UUID,
    }


@pytest.mark.asyncio
async def test_isolation_holds_when_the_squad_sees_only_its_node():
    panel = FakePanel(accessible_nodes=[{'nodeName': 'lab-node-a'}])
    await PanelProvisioner(panel).assert_isolated(SQUAD_UUID, expected_node='lab-node-a')


@pytest.mark.asyncio
async def test_isolation_fails_when_the_squad_sees_a_foreign_node():
    panel = FakePanel(accessible_nodes=[{'nodeName': 'lab-node-a'}, {'nodeName': 'lab-node-b'}])
    with pytest.raises(AssertionError):
        await PanelProvisioner(panel).assert_isolated(SQUAD_UUID, expected_node='lab-node-a')


@pytest.mark.asyncio
async def test_isolation_fails_when_the_squad_sees_no_node():
    panel = FakePanel(accessible_nodes=[])
    with pytest.raises(AssertionError):
        await PanelProvisioner(panel).assert_isolated(SQUAD_UUID, expected_node='lab-node-a')
