"""SSH к серверу владельца и сбор фактов о нём.

``connect`` поднимает соединение asyncssh по ключу, ``SshRunner`` выполняет
команды: ``run`` — для сбора фактов (вывод как есть), ``run_checked`` — для
установки (ненулевой код выхода или тайм-аут — ошибка). ``gather_facts``
запускает проверочные команды через любой runner и разбирает вывод в ``VpsFacts``.

Доступ строго по ключу: root-пароль в этой схеме не используется, поэтому в
коде его нет. Исходящий SSH идёт через SOCKS-выход входа: владелец видит адрес
входа, а не малины.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

import asyncssh
from python_socks.async_.asyncio import Proxy

from app.byo.vps_check import VpsFacts


SSH_PORT = 22
CONNECT_TIMEOUT_SECONDS = 20
COMMAND_TIMEOUT_SECONDS = 30
KILOBYTES_PER_MEGABYTE = 1024
STDERR_TAIL_LINES = 3

# Команды сбора фактов. Ключи-подстроки совпадают с тем, что ищет разбор.
_OS_RELEASE = 'cat /etc/os-release  # os-release'
_MEMINFO = 'cat /proc/meminfo  # meminfo'
# Уникальные локальные порты, которые кто-то слушает (без дублей по процессам).
_LISTENING_PORTS = (
    "ss -tlnH 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\\1/' | sort -un  # listening-ports"
)
_COUNTRY = 'curl -s -m 10 https://ipinfo.io/country  # country'


class CommandRunner(Protocol):
    async def run(self, command: str) -> str: ...


class RemoteCommandError(RuntimeError):
    """Команда на сервере не удалась. Текста команды здесь нет: в нём бывают секреты."""


def _text(output: object) -> str:
    return output if isinstance(output, str) else ''


def _tail(stderr: object) -> str:
    lines = [line for line in _text(stderr).splitlines() if line.strip()]
    return ' | '.join(lines[-STDERR_TAIL_LINES:]) or 'без вывода'


class SshRunner:
    """Выполняет команды на сервере по SSH-ключу через asyncssh."""

    def __init__(self, connection: asyncssh.SSHClientConnection) -> None:
        self._connection = connection

    async def run(self, command: str) -> str:
        result = await self._connection.run(command, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
        return _text(result.stdout)

    async def run_checked(self, command: str, *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
        try:
            result = await self._connection.run(command, timeout=timeout, check=False)
        except TimeoutError as error:  # asyncssh.TimeoutError — наследник asyncio.TimeoutError
            raise RemoteCommandError(f'не уложилась в {timeout:.0f} с') from error
        if result.returncode != 0:
            raise RemoteCommandError(f'код выхода {result.returncode}: {_tail(result.stderr)}')
        return _text(result.stdout)


async def _socket_through(proxy_url: str, host: str) -> socket.socket:
    proxy = Proxy.from_url(proxy_url)
    return await proxy.connect(dest_host=host, dest_port=SSH_PORT, timeout=CONNECT_TIMEOUT_SECONDS)


@contextlib.asynccontextmanager
async def connect(
    host: str,
    *,
    client_keys: Sequence[str],
    username: str = 'root',
    proxy_url: str | None = None,
) -> AsyncIterator[SshRunner]:
    """Соединение по ключу; с ``proxy_url`` (socks5://…) — через этот выход."""
    sock = await _socket_through(proxy_url, host) if proxy_url else None
    async with asyncssh.connect(
        host,
        port=SSH_PORT,
        username=username,
        client_keys=list(client_keys),
        known_hosts=None,  # первое подключение к только что созданному VPS
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        sock=sock,
    ) as connection:
        yield SshRunner(connection)


def _parse_os(os_release: str) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in os_release.splitlines():
        key, separator, value = line.partition('=')
        if separator:
            values[key.strip()] = value.strip().strip('"')
    return values.get('ID', '').lower(), values.get('VERSION_ID', '')


def _parse_total_memory_mb(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith('MemTotal:'):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdecimal():
                return int(parts[1]) // KILOBYTES_PER_MEGABYTE
    return 0


def _parse_ports(listening: str) -> tuple[int, ...]:
    return tuple(int(line) for line in listening.split() if line.isdecimal())


def _parse_country(country: str) -> str | None:
    code = country.strip().upper()
    return code or None


async def gather_facts(runner: CommandRunner) -> VpsFacts:
    os_id, os_version = _parse_os(await runner.run(_OS_RELEASE))
    total_memory_mb = _parse_total_memory_mb(await runner.run(_MEMINFO))
    busy_ports = _parse_ports(await runner.run(_LISTENING_PORTS))
    country_code = _parse_country(await runner.run(_COUNTRY))
    return VpsFacts(
        os_id=os_id,
        os_version=os_version,
        total_memory_mb=total_memory_mb,
        busy_ports=busy_ports,
        country_code=country_code,
    )
