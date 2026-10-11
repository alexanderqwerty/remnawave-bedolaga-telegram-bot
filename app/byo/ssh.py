"""SSH к серверу владельца и сбор фактов о нём.

``SshRunner`` поднимает соединение asyncssh по ключу и выполняет команды.
``gather_facts`` запускает несколько проверочных команд через любой runner
(в бою — ``SshRunner``, в тестах — поддельный) и разбирает вывод в ``VpsFacts``.

Доступ строго по ключу: root-пароль в этой схеме не используется, поэтому в
коде его нет. Весь исходящий SSH идёт через egress-прокси (socks5 на xray).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from typing import Protocol

import asyncssh

from app.byo.vps_check import VpsFacts


CONNECT_TIMEOUT_SECONDS = 20
COMMAND_TIMEOUT_SECONDS = 30
KILOBYTES_PER_MEGABYTE = 1024

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


class SshRunner:
    """Выполняет команды на сервере по SSH-ключу через asyncssh."""

    def __init__(self, connection: asyncssh.SSHClientConnection) -> None:
        self._connection = connection

    async def run(self, command: str) -> str:
        result = await self._connection.run(command, timeout=COMMAND_TIMEOUT_SECONDS, check=False)
        return result.stdout if isinstance(result.stdout, str) else ''


@contextlib.asynccontextmanager
async def connect(
    host: str,
    *,
    client_keys: Sequence[str],
    username: str = 'root',
    proxy_command: str | None = None,
) -> AsyncIterator[SshRunner]:
    """Соединение по ключу. ``proxy_command`` пускает SSH через egress-прокси."""
    async with asyncssh.connect(
        host,
        username=username,
        client_keys=list(client_keys),
        known_hosts=None,  # первое подключение к только что созданному VPS
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        tunnel=proxy_command,
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
