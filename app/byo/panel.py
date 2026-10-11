"""Объекты ноды в панели Remnawave и проверка изоляции.

``PanelProvisioner`` обращается к панели через ``PanelRequester`` (в бою — обёртка
над общим клиентом бота, в тестах — поддельная панель). Каждый объект создаётся
своим методом по принципу «найти или создать»: если ответ панели потерялся,
повтор шага находит уже созданное по имени (адресу), а не плодит дубль.

Для ноды это config-profile (инбаунд Trojan+WS, блок приватных IP перед DIRECT),
сама нода, сквад владельца (один на все его ноды: вторая нода добавляет свой
инбаунд) и хост ALLOW_ONLY на этот сквад. ``assert_isolated`` проверяет, что
скваду видны ровно ноды владельца. Формы запросов сверены с живой панелью 3.4.4.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from typing import Any, NamedTuple, Protocol


NODE_PORT = 2222
# Клиенты идут на домен ноды через Caddy по TLS, не на порт инбаунда.
HTTPS_PORT = 443
# Панель подключается к свежей ноде за секунды; минуты хватает с запасом,
# три — на медленного хостера.
CONNECT_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5
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
    async def request(self, method: str, path: str, body: dict | None = None) -> Any: ...


class PanelStateError(RuntimeError):
    """В панели нашёлся объект, который не сходится с ожидаемым."""


class NodeNotConnectedError(RuntimeError):
    """Панель так и не подключилась к ноде."""


class IsolationError(RuntimeError):
    """Сквад владельца видит не ровно его ноды."""


class NodeLayout(NamedTuple):
    """Всё, что панели нужно знать об одной ноде владельца."""

    name: str  # o-<slug>-<n>: имя и профиля, и ноды
    inbound_tag: str
    inbound_port: int
    ws_path: str
    fqdn: str
    address: str  # IP сервера владельца
    host_remark: str  # так ноду видят участники в приложении
    proxy_url: str | None  # socks5-выход, через который панель ходит к ноде


class ProfileRef(NamedTuple):
    uuid: str
    inbound_uuid: str


def _profile_config(layout: NodeLayout) -> dict:
    """Trojan+WS-инбаунд; приватные адреса в BLOCK стоят раньше DIRECT."""
    return {
        'inbounds': [
            {
                'tag': layout.inbound_tag,
                # Наружу смотрит только Caddy; инбаунд слушает локально.
                'listen': '127.0.0.1',
                'protocol': 'trojan',
                'port': layout.inbound_port,
                'settings': {'clients': []},
                'streamSettings': {
                    'network': 'ws',
                    'security': 'none',
                    # Host обязан совпадать с доменом, который отдаёт Caddy.
                    'wsSettings': {'path': layout.ws_path, 'host': layout.fqdn},
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
                {'inboundTag': [layout.inbound_tag], 'outboundTag': 'DIRECT'},
            ]
        },
    }


def _node_body(layout: NodeLayout, profile: ProfileRef) -> dict:
    body: dict[str, Any] = {
        'name': layout.name,
        'address': layout.address,
        'port': NODE_PORT,
        'countryCode': 'XX',
        'configProfile': {
            'activeConfigProfileUuid': profile.uuid,
            'activeInbounds': [profile.inbound_uuid],
        },
    }
    if layout.proxy_url:
        # Панель ходит к ноде через egress-выход: у ноды 2222 открыт только с его IP.
        body['proxyUrl'] = layout.proxy_url
    return body


def _host_body(layout: NodeLayout, profile: ProfileRef, squad_uuid: str) -> dict:
    return {
        'inbound': {'configProfileUuid': profile.uuid, 'configProfileInboundUuid': profile.inbound_uuid},
        'remark': layout.host_remark,
        'address': layout.fqdn,
        'port': HTTPS_PORT,
        'path': layout.ws_path,
        'host': layout.fqdn,
        'sni': layout.fqdn,
        'securityLayer': 'TLS',
        'internalSquads': {'mode': 'ALLOW_ONLY', 'squads': [squad_uuid]},
    }


class PanelProvisioner:
    def __init__(
        self,
        panel: PanelRequester,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._panel = panel
        self._sleep = sleep

    async def generate_secret_key(self) -> str:
        """SECRET_KEY для docker-compose ноды. Не сохраняется нигде, кроме самой ноды."""
        response = await self._panel.request('GET', '/api/keygen')
        return response['secretKey']

    async def ensure_profile(self, layout: NodeLayout) -> ProfileRef:
        listing = await self._panel.request('GET', '/api/config-profiles')
        profile = next((p for p in listing['configProfiles'] if p['name'] == layout.name), None)
        if profile is None:
            profile = await self._panel.request(
                'POST', '/api/config-profiles', {'name': layout.name, 'config': _profile_config(layout)}
            )
        inbound = next((i for i in profile['inbounds'] if i['tag'] == layout.inbound_tag), None)
        if inbound is None:
            raise PanelStateError(f'в профиле {layout.name} нет инбаунда {layout.inbound_tag}')
        return ProfileRef(uuid=profile['uuid'], inbound_uuid=inbound['uuid'])

    async def ensure_node(self, layout: NodeLayout, profile: ProfileRef) -> str:
        nodes = await self._panel.request('GET', '/api/nodes')
        existing = next((n for n in nodes if n['name'] == layout.name), None)
        if existing is not None:
            if existing['address'] != layout.address:
                raise PanelStateError(
                    f'в панели уже есть нода {layout.name} с адресом {existing["address"]}, а не {layout.address}'
                )
            return existing['uuid']
        created = await self._panel.request('POST', '/api/nodes', _node_body(layout, profile))
        return created['uuid']

    async def wait_connected(self, node_uuid: str) -> None:
        status = 'нет ответа'
        for _attempt in range(CONNECT_TIMEOUT_SECONDS // POLL_INTERVAL_SECONDS):
            node = await self._panel.request('GET', f'/api/nodes/{node_uuid}')
            if node.get('isConnected'):
                return
            status = node.get('lastStatusMessage') or status
            await self._sleep(POLL_INTERVAL_SECONDS)
        raise NodeNotConnectedError(f'панель не подключилась к ноде за {CONNECT_TIMEOUT_SECONDS} с: {status}')

    async def ensure_squad(self, name: str, inbound_uuid: str, *, squad_uuid: str | None = None) -> str:
        """Сквад владельца с этим инбаундом: создать для первой ноды, дополнить для следующих."""
        if squad_uuid is None:
            listing = await self._panel.request('GET', '/api/internal-squads')
            existing = next((s for s in listing['internalSquads'] if s['name'] == name), None)
            if existing is None:
                created = await self._panel.request(
                    'POST', '/api/internal-squads', {'name': name, 'inbounds': [inbound_uuid]}
                )
                return created['uuid']
            # По имени подхватываем только свой сквад, созданный до потерянного
            # ответа: в нём нет ничего, кроме этого инбаунда. Чужой — не трогаем.
            if not {inbound['uuid'] for inbound in existing['inbounds']}.issubset({inbound_uuid}):
                raise PanelStateError(f'в панели уже есть чужой сквад с именем {name}')
            squad_uuid = existing['uuid']

        squad = await self._panel.request('GET', f'/api/internal-squads/{squad_uuid}')
        current = [inbound['uuid'] for inbound in squad['inbounds']]
        if inbound_uuid not in current:
            await self._panel.request(
                'PATCH', '/api/internal-squads', {'uuid': squad_uuid, 'inbounds': [*current, inbound_uuid]}
            )
        return squad_uuid

    async def ensure_host(self, layout: NodeLayout, profile: ProfileRef, squad_uuid: str) -> str:
        hosts = await self._panel.request('GET', '/api/hosts')
        existing = next((h for h in hosts if h['address'] == layout.fqdn), None)
        if existing is not None:
            return existing['uuid']
        created = await self._panel.request('POST', '/api/hosts', _host_body(layout, profile, squad_uuid))
        return created['uuid']

    async def assert_isolated(self, squad_uuid: str, *, expected_nodes: Collection[str]) -> None:
        response = await self._panel.request('GET', f'/api/internal-squads/{squad_uuid}/accessible-nodes')
        visible = sorted(node['nodeName'] for node in response.get('accessibleNodes') or [])
        if visible != sorted(expected_nodes):
            raise IsolationError(f'сквад должен видеть только ноды {sorted(expected_nodes)}, а видит {visible}')
