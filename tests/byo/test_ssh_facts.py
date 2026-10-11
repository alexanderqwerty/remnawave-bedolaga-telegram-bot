"""Сбор фактов о сервере по SSH.

``gather_facts`` запускает несколько команд через переданный runner и
разбирает их вывод в ``VpsFacts``. Настоящий SSH подменён поддельным runner,
который отдаёт заранее заданный вывод, поэтому разбор проверяется без сети.
"""

from __future__ import annotations

import pytest

from app.byo.ssh import gather_facts


OS_RELEASE = 'ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian GNU/Linux 13"\n'
MEMINFO_1GB = 'MemTotal:         999376 kB\nMemFree:          800000 kB\n'
PORTS_NONE = ''
COUNTRY_DE = 'DE\n'


class FakeRunner:
    """Отдаёт заранее заданный вывод по подстроке команды."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.commands: list[str] = []

    async def run(self, command: str) -> str:
        self.commands.append(command)
        for needle, output in self._responses.items():
            if needle in command:
                return output
        raise AssertionError(f'поддельный runner не знает команду: {command}')


def _runner(**overrides: str) -> FakeRunner:
    responses = {
        'os-release': OS_RELEASE,
        'meminfo': MEMINFO_1GB,
        'listening-ports': PORTS_NONE,
        'country': COUNTRY_DE,
    }
    responses.update(overrides)
    return FakeRunner(responses)


@pytest.mark.asyncio
async def test_parses_a_plain_debian():
    facts = await gather_facts(_runner())

    assert facts.os_id == 'debian'
    assert facts.os_version == '13'
    assert facts.total_memory_mb == 975  # 999376 kB // 1024, округление вниз
    assert facts.busy_ports == ()
    assert facts.country_code == 'DE'


@pytest.mark.asyncio
async def test_busy_ports_are_collected_as_numbers():
    facts = await gather_facts(_runner(**{'listening-ports': '22\n80\n443\n'}))
    assert set(facts.busy_ports) == {22, 80, 443}


@pytest.mark.asyncio
async def test_country_is_none_when_lookup_is_empty():
    facts = await gather_facts(_runner(country=''))
    assert facts.country_code is None


@pytest.mark.asyncio
async def test_country_is_uppercased_and_trimmed():
    facts = await gather_facts(_runner(country='  de \n'))
    assert facts.country_code == 'DE'


@pytest.mark.asyncio
async def test_unparsable_memory_becomes_zero_so_the_check_rejects_it():
    facts = await gather_facts(_runner(meminfo='garbage\n'))
    assert facts.total_memory_mb == 0


@pytest.mark.asyncio
async def test_os_id_is_lowercased():
    facts = await gather_facts(_runner(**{'os-release': 'ID=Ubuntu\nVERSION_ID="24.04"\n'}))
    assert facts.os_id == 'ubuntu'
    assert facts.os_version == '24.04'
