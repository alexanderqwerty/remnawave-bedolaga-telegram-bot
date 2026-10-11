"""Подключение сервера владельца: все шаги одной цепочкой.

``connect_node`` превращает VPS владельца в изолированную ноду по шагам плана:
SSH → проверка → секреты → DNS → профиль → установка → сертификат → нода →
подключение → сквад → хост → изоляция → объекты в боте. Каждый шаг пишется
в журнал (``byo_steps``); повтор после сбоя продолжает с упавшего шага.

Всё, что шаги узнают, сразу сохраняется в строках ``ByoNode``/``ByoOwner``,
поэтому пропущенным на повторе шагам нечего восстанавливать. SSH открывается
лениво — только если оставшимся шагам нужен сервер. SECRET_KEY ноды берётся у
панели прямо перед установкой и никуда, кроме самой ноды, не пишется.

Внешний мир приходит через ``ConnectDeps``: в бою настоящие панель, reg.ru, SSH
и БД бота, в тестах — подделки.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner
from app.byo.node_install import InstallSpec, install_commands, tls_check_command
from app.byo.node_secrets import Rng, build_node_secrets, node_name, squad_name
from app.byo.panel import NodeLayout, PanelProvisioner, ProfileRef
from app.byo.provisioning import ProvisioningError, Step, StepJournal, run_steps
from app.byo.ssh import CommandRunner, RemoteCommandError, gather_facts
from app.byo.vps_check import evaluate


STEP_NAMES = (
    'ssh',
    'check',
    'secrets',
    'dns',
    'profile',
    'install',
    'tls',
    'node',
    'online',
    'squad',
    'host',
    'isolation',
    'bot',
)
# DNS у reg.ru расходится минутами, Caddy получает сертификат с повторами.
TLS_WAIT_SECONDS = 15 * 60
TLS_POLL_SECONDS = 20
ROOT_UID = '0'


class ConnectError(RuntimeError):
    """Понятная владельцу причина, по которой сервер подключить нельзя."""


@dataclass(frozen=True)
class ConnectSettings:
    zone: str  # vpn.miet-tv.ru: домены нод — <label>.<zone>
    dns_domain: str  # miet-tv.ru: зона, которой управляет reg.ru
    gateway_ip: str  # только с него открыт порт 2222 ноды
    proxy_url: str | None  # socks5-выход: через него панель ходит к ноде

    def __post_init__(self) -> None:
        if self.zone != self.dns_domain and not self.zone.endswith(f'.{self.dns_domain}'):
            raise ValueError(f'зона нод {self.zone} должна лежать внутри домена {self.dns_domain}')


class RemoteRunner(CommandRunner, Protocol):
    async def run_checked(self, command: str, *, timeout: float = ...) -> str: ...


class DnsRecords(Protocol):
    async def a_records(self, domain: str, subdomain: str) -> list[str]: ...
    async def add_record(self, domain: str, subdomain: str, ip: str) -> None: ...


class OwnerObjects(Protocol):
    async def ensure_for(self, owner: ByoOwner) -> None: ...


@dataclass(frozen=True)
class ConnectDeps:
    settings: ConnectSettings
    panel: PanelProvisioner
    dns: DnsRecords
    open_ssh: Callable[[str], AbstractAsyncContextManager[RemoteRunner]]
    bot: OwnerObjects
    save: Callable[[], Awaitable[None]]
    rng: Rng | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


Progress = Callable[[str], Awaitable[None]]


class _LazyRemote:
    """SSH открывается при первом шаге, которому нужен сервер, и закрывается в конце."""

    def __init__(self, deps: ConnectDeps, ip: str, stack: contextlib.AsyncExitStack) -> None:
        self._deps = deps
        self._ip = ip
        self._stack = stack
        self._runner: RemoteRunner | None = None

    async def get(self) -> RemoteRunner:
        if self._runner is None:
            self._runner = await self._stack.enter_async_context(self._deps.open_ssh(self._ip))
        return self._runner


class _NodeConnection:
    def __init__(self, node: ByoNode, deps: ConnectDeps, remote: _LazyRemote) -> None:
        self._node = node
        self._owner = node.owner
        self._deps = deps
        self._remote = remote

    def steps(self) -> list[Step]:
        actions = {
            'ssh': self._ssh,
            'check': self._check,
            'secrets': self._secrets,
            'dns': self._dns,
            'profile': self._profile,
            'install': self._install,
            'tls': self._tls,
            'node': self._register_node,
            'online': self._online,
            'squad': self._squad,
            'host': self._host,
            'isolation': self._isolation,
            'bot': self._bot,
        }
        return [Step(name, self._ignoring_context(actions[name])) for name in STEP_NAMES]

    @staticmethod
    def _ignoring_context(action: Callable[[], Awaitable[None]]) -> Callable[[dict], Awaitable[None]]:
        # Состояние живёт в строках ноды и владельца, общий контекст шагам не нужен.
        async def run(_context: dict) -> None:
            await action()

        return run

    def _layout(self) -> NodeLayout:
        node = self._node
        return NodeLayout(
            name=node_name(self._owner.slug, node.ordinal),
            inbound_tag=node.inbound_tag,
            inbound_port=node.inbound_port,
            ws_path=node.ws_path,
            fqdn=node.fqdn,
            address=node.ip,
            host_remark=f'{self._owner.slug} {node.ordinal}',
            proxy_url=self._deps.settings.proxy_url,
        )

    def _profile_ref(self) -> ProfileRef:
        return ProfileRef(uuid=self._node.config_profile_uuid, inbound_uuid=self._node.inbound_uuid)

    async def _ssh(self) -> None:
        uid = (await (await self._remote.get()).run_checked('id -u')).strip()
        if uid != ROOT_UID:
            raise ConnectError('Нужен вход под root по нашему ключу, а сервер пустил другого пользователя.')

    async def _check(self) -> None:
        verdict = evaluate(await gather_facts(await self._remote.get()))
        if not verdict.ok:
            raise ConnectError(verdict.reason)

    async def _secrets(self) -> None:
        secrets = build_node_secrets(self._owner.slug, self._node.ordinal, self._deps.settings.zone, self._deps.rng)
        self._node.fqdn = secrets.fqdn
        self._node.ws_path = secrets.ws_path
        self._node.inbound_port = secrets.inbound_port
        self._node.inbound_tag = secrets.inbound_tag
        await self._deps.save()

    async def _dns(self) -> None:
        domain = self._deps.settings.dns_domain
        subdomain = self._node.fqdn.removesuffix(f'.{domain}')
        addresses = await self._deps.dns.a_records(domain, subdomain)
        if self._node.ip in addresses:
            return
        if addresses:
            raise ConnectError(f'Имя {self._node.fqdn} уже указывает на другой адрес: {", ".join(addresses)}.')
        await self._deps.dns.add_record(domain, subdomain, self._node.ip)

    async def _profile(self) -> None:
        profile = await self._deps.panel.ensure_profile(self._layout())
        self._node.config_profile_uuid = profile.uuid
        self._node.inbound_uuid = profile.inbound_uuid
        await self._deps.save()

    async def _install(self) -> None:
        runner = await self._remote.get()
        secret_key = await self._deps.panel.generate_secret_key()
        node = self._node
        spec = InstallSpec(node.fqdn, node.ws_path, node.inbound_port, secret_key, self._deps.settings.gateway_ip)
        for command in install_commands(spec):
            try:
                await runner.run_checked(command.command, timeout=command.timeout)
            except RemoteCommandError as error:
                # В тексте ошибки команды нет, но ключ не должен утечь и через stderr.
                reason = str(error).replace(secret_key, '***')
                raise ConnectError(f'{command.label}: {reason}') from None

    async def _tls(self) -> None:
        runner = await self._remote.get()
        command = tls_check_command(self._node.fqdn)
        last_error = 'нет ответа'
        for _attempt in range(TLS_WAIT_SECONDS // TLS_POLL_SECONDS):
            try:
                await runner.run_checked(command)
                return
            except RemoteCommandError as error:
                last_error = str(error)
            await self._deps.sleep(TLS_POLL_SECONDS)
        raise ConnectError(f'{self._node.fqdn} не отдал сертификат за {TLS_WAIT_SECONDS // 60} мин: {last_error}')

    async def _register_node(self) -> None:
        self._node.node_uuid = await self._deps.panel.ensure_node(self._layout(), self._profile_ref())
        await self._deps.save()

    async def _online(self) -> None:
        await self._deps.panel.wait_connected(self._node.node_uuid)

    async def _squad(self) -> None:
        self._owner.squad_uuid = await self._deps.panel.ensure_squad(
            squad_name(self._owner.slug), self._node.inbound_uuid, squad_uuid=self._owner.squad_uuid
        )
        await self._deps.save()

    async def _host(self) -> None:
        self._node.host_uuid = await self._deps.panel.ensure_host(
            self._layout(), self._profile_ref(), self._owner.squad_uuid
        )
        await self._deps.save()

    async def _isolation(self) -> None:
        expected = {node_name(self._owner.slug, node.ordinal) for node in self._owner.nodes if node.node_uuid}
        await self._deps.panel.assert_isolated(self._owner.squad_uuid, expected_nodes=expected)

    async def _bot(self) -> None:
        await self._deps.bot.ensure_for(self._owner)
        await self._deps.save()


def _announced(step: Step, progress: Progress) -> Step:
    async def action(context: dict) -> None:
        await progress(step.name)
        await step.action(context)

    return Step(step.name, action, step.restore)


async def connect_node(
    node: ByoNode,
    deps: ConnectDeps,
    journal: StepJournal,
    *,
    progress: Progress | None = None,
) -> None:
    """Проводит ноду через все шаги. ``node.owner`` и ``owner.nodes`` должны быть загружены."""
    node.status = ByoNodeStatus.PROVISIONING.value
    await deps.save()
    async with contextlib.AsyncExitStack() as stack:
        steps = _NodeConnection(node, deps, _LazyRemote(deps, node.ip, stack)).steps()
        if progress is not None:
            steps = [_announced(step, progress) for step in steps]
        try:
            await run_steps(steps, journal, context={})
        except ProvisioningError:
            node.status = ByoNodeStatus.FAILED.value
            await deps.save()
            raise
    node.status = ByoNodeStatus.ONLINE.value
    await deps.save()
