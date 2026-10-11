"""Создание объектов ноды в панели Remnawave и проверка изоляции.

``PanelProvisioner`` обращается к панели через ``PanelRequester`` (в бою — обёртка
над общим клиентом, в тестах — поддельный). Для одной ноды создаёт config-profile
(инбаунд Trojan+WS, блок приватных IP перед DIRECT), ноду, сквад из одного
инбаунда и хост в режиме ALLOW_ONLY на этот сквад, затем проверяет, что скваду
видна ровно эта нода. Формы запросов повторяют проверенные в лаборатории.
"""

from __future__ import annotations

from typing import Any, Protocol


NODE_PORT = 2222
# Клиенты идут на домен ноды через Caddy по TLS, не на порт инбаунда.
HTTPS_PORT = 443
# Приватные и служебные диапазоны: блокируются раньше DIRECT, чтобы через ноду
# нельзя было достучаться до внутренней сети хостера или малины.
PRIVATE_NETWORKS = [
    '0.0.0.0/8',
    '10.0.0.0/8',
    '100.64.0.0/10',
    '127.0.0.0/8',
    '169.254.0.0/16',
    '172.16.0.0/12',
    '192.168.0.0/16',
    '::1/128',
    'fc00::/7',
    'fe80::/10',
]


class PanelRequester(Protocol):
    async def request(self, method: str, path: str, body: dict | None = None) -> dict: ...


def _profile_config(spec: dict[str, Any]) -> dict:
    """Trojan+WS-инбаунд; приватные адреса в BLOCK стоят раньше DIRECT."""
    return {
        'inbounds': [
            {
                'tag': spec['inbound_tag'],
                # Наружу смотрит только Caddy; инбаунд слушает локально.
                'listen': '127.0.0.1',
                'protocol': 'trojan',
                'port': spec['inbound_port'],
                'settings': {'clients': []},
                'streamSettings': {
                    'network': 'ws',
                    'security': 'none',
                    # Host обязан совпадать с доменом, который отдаёт Caddy.
                    'wsSettings': {'path': spec['ws_path'], 'host': spec['fqdn']},
                },
            }
        ],
        'outbounds': [
            {'tag': 'DIRECT', 'protocol': 'freedom'},
            {'tag': 'BLOCK', 'protocol': 'blackhole'},
        ],
        'routing': {
            'rules': [
                {'ip': PRIVATE_NETWORKS, 'outboundTag': 'BLOCK'},
                {'inboundTag': [spec['inbound_tag']], 'outboundTag': 'DIRECT'},
            ]
        },
    }


class PanelProvisioner:
    def __init__(self, panel: PanelRequester) -> None:
        self._panel = panel

    async def create_node(self, spec: dict[str, Any]) -> dict[str, str]:
        profile = await self._panel.request(
            'POST', '/api/config-profiles', {'name': spec['name'], 'config': _profile_config(spec)}
        )
        inbound_uuid = next(i['uuid'] for i in profile['inbounds'] if i['tag'] == spec['inbound_tag'])

        node_body: dict[str, Any] = {
            'name': spec['node_name'],
            'address': spec['node_address'],
            'port': NODE_PORT,
            'countryCode': 'XX',
            'configProfile': {
                'activeConfigProfileUuid': profile['uuid'],
                'activeInbounds': [inbound_uuid],
            },
        }
        if spec.get('proxy_url'):
            # Панель ходит к ноде через egress-выход: у ноды 2222 открыт только с его IP.
            node_body['proxyUrl'] = spec['proxy_url']
        node = await self._panel.request('POST', '/api/nodes', node_body)
        squad = await self._panel.request(
            'POST', '/api/internal-squads', {'name': spec['name'], 'inbounds': [inbound_uuid]}
        )
        host = await self._panel.request(
            'POST',
            '/api/hosts',
            {
                'inbound': {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound_uuid},
                'remark': spec['host_remark'],
                'address': spec['fqdn'],
                'port': HTTPS_PORT,
                'path': spec['ws_path'],
                'host': spec['fqdn'],
                'sni': spec['fqdn'],
                'securityLayer': 'TLS',
                'internalSquads': {'mode': 'ALLOW_ONLY', 'squads': [squad['uuid']]},
            },
        )
        return {
            'config_profile_uuid': profile['uuid'],
            'inbound_uuid': inbound_uuid,
            'node_uuid': node['uuid'],
            'squad_uuid': squad['uuid'],
            'host_uuid': host['uuid'],
        }

    async def assert_isolated(self, squad_uuid: str, *, expected_node: str) -> None:
        response = await self._panel.request('GET', f'/api/internal-squads/{squad_uuid}/accessible-nodes')
        names = sorted(node['nodeName'] for node in response.get('accessibleNodes') or [])
        if names != [expected_node]:
            raise AssertionError(f'сквад {squad_uuid} должен видеть только ноду {expected_node!r}, а видит {names}')
