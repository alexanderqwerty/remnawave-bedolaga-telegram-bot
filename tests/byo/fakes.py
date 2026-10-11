"""Подделки для тестов BYO: панель, сервер владельца, reg.ru, бот, журнал.

``FakePanel`` хранит профили, ноды, сквады и хосты в памяти и отвечает так же,
как настоящая панель 3.4.4 после снятия обёртки ``response``. Умеет «терять
ответ»: объект создаётся, а вызывающий получает ошибку — так проверяется, что
повтор шага находит созданное, а не плодит дубль.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from app.byo.models import ByoStepStatus
from app.byo.ssh import RemoteCommandError


class PanelUnavailableError(RuntimeError):
    """Панель не ответила (объект при этом мог быть создан)."""


class FakePanel:
    def __init__(self, *, polls_before_connect: int = 0, never_connects: bool = False) -> None:
        self.profiles: dict[str, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.squads: dict[str, dict] = {}
        self.hosts: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.secret_key = 'PANELSECRETKEY'
        self.lose_response_of: set[str] = set()
        # Имена «чужих» нод, которые панель по ошибке показывает скваду.
        self.extra_visible_nodes: list[str] = []
        self._polls_before_connect = polls_before_connect
        self._never_connects = never_connects
        self._polls: dict[str, int] = {}
        self._inbounds: dict[str, dict] = {}
        self._ids = itertools.count(1)

    # --- помощники для тестов -------------------------------------------------

    def calls_to(self, method: str, path: str) -> list[dict | None]:
        return [
            body for called_method, called_path, body in self.calls if (called_method, called_path) == (method, path)
        ]

    def body_of(self, method: str, path: str) -> dict:
        bodies = self.calls_to(method, path)
        assert bodies, f'не было вызова {method} {path}'
        return bodies[-1]

    def add_foreign_node(self, name: str, inbound_uuid: str) -> None:
        """Чужая нода, которую сквад владельца видеть не должен."""
        uuid = self._new_id('node')
        self.nodes[uuid] = {
            'uuid': uuid,
            'name': name,
            'address': '198.51.100.1',
            'isConnected': True,
            'configProfile': {'activeInbounds': [inbound_uuid]},
        }

    # --- сам запросчик ----------------------------------------------------------

    async def request(self, method: str, path: str, body: dict | None = None) -> Any:
        self.calls.append((method, path, body))
        response = self._route(method, path, body)
        if f'{method} {path}' in self.lose_response_of:
            self.lose_response_of.discard(f'{method} {path}')
            raise PanelUnavailableError(f'{method} {path}: ответ потерян')
        return response

    def _route(self, method: str, path: str, body: dict | None) -> Any:
        routes = {
            ('GET', '/api/keygen'): lambda: {'secretKey': self.secret_key},
            ('GET', '/api/config-profiles'): lambda: {
                'total': len(self.profiles),
                'configProfiles': list(self.profiles.values()),
            },
            ('POST', '/api/config-profiles'): lambda: self._create_profile(body),
            ('GET', '/api/nodes'): lambda: list(self.nodes.values()),
            ('POST', '/api/nodes'): lambda: self._create_node(body),
            ('GET', '/api/internal-squads'): lambda: {
                'total': len(self.squads),
                'internalSquads': [self._squad_view(uuid) for uuid in self.squads],
            },
            ('POST', '/api/internal-squads'): lambda: self._create_squad(body),
            ('PATCH', '/api/internal-squads'): lambda: self._patch_squad(body),
            ('GET', '/api/hosts'): lambda: list(self.hosts.values()),
            ('POST', '/api/hosts'): lambda: self._create_host(body),
        }
        if (method, path) in routes:
            return routes[(method, path)]()
        if method == 'GET' and (match := re.fullmatch(r'/api/nodes/([^/]+)', path)):
            return self._poll_node(match.group(1))
        if method == 'GET' and (match := re.fullmatch(r'/api/internal-squads/([^/]+)/accessible-nodes', path)):
            return self._accessible_nodes(match.group(1))
        if method == 'GET' and (match := re.fullmatch(r'/api/internal-squads/([^/]+)', path)):
            return self._squad_view(match.group(1))
        raise AssertionError(f'поддельная панель не знает {method} {path}')

    def _new_id(self, kind: str) -> str:
        return f'{kind}-{next(self._ids)}'

    def _create_profile(self, body: dict) -> dict:
        if any(profile['name'] == body['name'] for profile in self.profiles.values()):
            raise AssertionError(f'профиль {body["name"]} уже есть — повтор должен был его найти')
        uuid = self._new_id('profile')
        inbounds = []
        for inbound in body['config']['inbounds']:
            inbound_view = {'uuid': self._new_id('inbound'), 'profileUuid': uuid, 'tag': inbound['tag']}
            self._inbounds[inbound_view['uuid']] = inbound_view
            inbounds.append(inbound_view)
        self.profiles[uuid] = {'uuid': uuid, 'name': body['name'], 'config': body['config'], 'inbounds': inbounds}
        return self.profiles[uuid]

    def _create_node(self, body: dict) -> dict:
        uuid = self._new_id('node')
        self.nodes[uuid] = {**body, 'uuid': uuid, 'isConnected': False, 'lastStatusMessage': 'connecting'}
        return self.nodes[uuid]

    def _poll_node(self, uuid: str) -> dict:
        node = self.nodes[uuid]
        self._polls[uuid] = self._polls.get(uuid, 0) + 1
        if not self._never_connects and self._polls[uuid] > self._polls_before_connect:
            node['isConnected'] = True
        else:
            node['lastStatusMessage'] = 'connect ETIMEDOUT 203.0.113.10:2222'
        return node

    def _squad_view(self, uuid: str) -> dict:
        squad = self.squads[uuid]
        return {'uuid': uuid, 'name': squad['name'], 'inbounds': [self._inbounds[i] for i in squad['inbounds']]}

    def _create_squad(self, body: dict) -> dict:
        if any(squad['name'] == body['name'] for squad in self.squads.values()):
            raise AssertionError(f'сквад {body["name"]} уже есть — повтор должен был его найти')
        uuid = self._new_id('squad')
        self.squads[uuid] = {'name': body['name'], 'inbounds': list(body['inbounds'])}
        return self._squad_view(uuid)

    def _patch_squad(self, body: dict) -> dict:
        self.squads[body['uuid']]['inbounds'] = list(body['inbounds'])
        return self._squad_view(body['uuid'])

    def _create_host(self, body: dict) -> dict:
        uuid = self._new_id('host')
        self.hosts[uuid] = {**body, 'uuid': uuid}
        return self.hosts[uuid]

    def _accessible_nodes(self, squad_uuid: str) -> dict:
        squad_inbounds = set(self.squads[squad_uuid]['inbounds'])
        visible = [
            {'uuid': node['uuid'], 'nodeName': node['name']}
            for node in self.nodes.values()
            if squad_inbounds & set(node['configProfile']['activeInbounds'])
        ]
        visible += [{'uuid': f'foreign-{name}', 'nodeName': name} for name in self.extra_visible_nodes]
        return {'squadUuid': squad_uuid, 'accessibleNodes': visible}


class FakeServer:
    """VPS владельца по SSH: отвечает на сбор фактов и выполняет команды установки."""

    def __init__(
        self,
        *,
        country: str = 'DE',
        uid: str = '0',
        tls_failures: int = 0,
        fail_on: str | None = None,
    ) -> None:
        self.checked: list[tuple[str, float]] = []
        self._facts = {
            'os-release': 'ID=debian\nVERSION_ID="13"\n',
            'meminfo': 'MemTotal:        1999376 kB\n',
            'listening-ports': '22\n',
            'country': f'{country}\n',
        }
        self._uid = uid
        self._tls_failures = tls_failures
        self._fail_on = fail_on

    async def run(self, command: str) -> str:
        return next(output for needle, output in self._facts.items() if needle in command)

    async def run_checked(self, command: str, *, timeout: float = 30) -> str:
        self.checked.append((command, timeout))
        if self._fail_on is not None and self._fail_on in command:
            raise RemoteCommandError('код выхода 1: no space left on device')
        if command == 'id -u':
            return f'{self._uid}\n'
        if command.startswith('curl ') and self._tls_failures > 0:
            self._tls_failures -= 1
            raise RemoteCommandError('код выхода 60: SSL certificate problem')
        return ''

    def written_files(self) -> dict[str, str]:
        files = {}
        for command, _timeout in self.checked:
            if match := re.search(r'echo (\S+) \| base64 -d > (\S+)', command):
                files[match.group(2)] = base64.b64decode(match.group(1)).decode()
        return files

    def ran(self, needle: str) -> int:
        return sum(needle in command for command, _timeout in self.checked)


class FakeSsh:
    """Открывает ``FakeServer`` вместо настоящего SSH и считает подключения."""

    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.opened: list[str] = []

    @contextlib.asynccontextmanager
    async def __call__(self, ip: str) -> AsyncIterator[FakeServer]:
        self.opened.append(ip)
        yield self.server


class FakeDns:
    def __init__(self, existing: dict[str, list[str]] | None = None) -> None:
        self.records: dict[str, list[str]] = {name: list(ips) for name, ips in (existing or {}).items()}
        self.added: list[tuple[str, str, str]] = []

    async def a_records(self, domain: str, subdomain: str) -> list[str]:
        return list(self.records.get(subdomain, []))

    async def add_record(self, domain: str, subdomain: str, ip: str) -> None:
        self.added.append((domain, subdomain, ip))
        self.records.setdefault(subdomain, []).append(ip)


class FakeBotObjects:
    def __init__(self) -> None:
        self.ensured: list[str] = []

    async def ensure_for(self, owner: Any) -> None:
        self.ensured.append(owner.slug)
        owner.promo_code = owner.promo_code or 'LABA0000CODE'


class MemoryJournal:
    """Журнал шагов в памяти вместо таблицы byo_steps."""

    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.detail: dict[str, str] = {}

    async def is_done(self, name: str) -> bool:
        return self.status.get(name) == ByoStepStatus.DONE.value

    async def mark_running(self, name: str) -> None:
        self.status[name] = ByoStepStatus.PENDING.value

    async def mark_done(self, name: str) -> None:
        self.status[name] = ByoStepStatus.DONE.value

    async def mark_failed(self, name: str, detail: str) -> None:
        self.status[name] = ByoStepStatus.FAILED.value
        self.detail[name] = detail

    def done(self) -> list[str]:
        return [name for name, status in self.status.items() if status == ByoStepStatus.DONE.value]


class CountingRng:
    """Предсказуемые «случайные» токены: 00000001, 00000002…"""

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def token(self, length: int) -> str:
        return f'{next(self._ids):0{length}d}'


class FakeBot:
    """Telegram: запоминает отправленные и исправленные сообщения."""

    def __init__(self, *, edit_errors: list[Exception] | None = None) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._edit_errors = list(edit_errors or [])
        self._ids = itertools.count(100)

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **_kwargs) -> SimpleNamespace:
        message_id = next(self._ids)
        self.sent.append({'chat_id': chat_id, 'text': text, 'markup': reply_markup, 'message_id': message_id})
        return SimpleNamespace(message_id=message_id)

    async def edit_message_text(self, text: str, chat_id: int, message_id: int, reply_markup=None, **_kwargs):
        if self._edit_errors:
            raise self._edit_errors.pop(0)
        self.edited.append({'chat_id': chat_id, 'message_id': message_id, 'text': text, 'markup': reply_markup})
