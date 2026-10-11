"""Выполнение команд установки на сервере по SSH.

``run_checked`` нужен шагам установки: ненулевой код выхода — это сбой шага,
а не «пустой вывод». В тексте ошибки нет самой команды: в командах установки
бывает SECRET_KEY ноды, а ошибка уходит в журнал и владельцу. Подключение идёт
через SOCKS-выход входа, чтобы владелец видел адрес входа, а не малины.
Соединение asyncssh и SOCKS-прокси подменены.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from app.byo import ssh
from app.byo.ssh import RemoteCommandError, SshRunner


SECRET_COMMAND = 'echo SECRETKEYVALUE | base64 -d > /opt/remnanode/docker-compose.yml'


class FakeConnection:
    def __init__(self, *, returncode: int = 0, stdout: str = '', stderr: str = '', hang: bool = False) -> None:
        self._result = SimpleNamespace(returncode=returncode, exit_status=returncode, stdout=stdout, stderr=stderr)
        self._hang = hang
        self.timeouts: list[float | None] = []

    async def run(self, command: str, *, timeout: float | None = None, check: bool = False) -> SimpleNamespace:
        self.timeouts.append(timeout)
        if self._hang:
            raise TimeoutError
        return self._result


@pytest.mark.asyncio
async def test_successful_command_returns_its_output():
    runner = SshRunner(FakeConnection(stdout='0\n'))
    assert await runner.run_checked('id -u') == '0\n'


@pytest.mark.asyncio
async def test_non_zero_exit_raises_with_the_end_of_stderr():
    stderr = 'line 1\nline 2\nE: Unable to locate package ufw\n'
    runner = SshRunner(FakeConnection(returncode=100, stderr=stderr))

    with pytest.raises(RemoteCommandError) as error:
        await runner.run_checked('apt-get install -y ufw')

    assert '100' in str(error.value)
    assert 'Unable to locate package ufw' in str(error.value)


@pytest.mark.asyncio
async def test_error_text_never_contains_the_command():
    runner = SshRunner(FakeConnection(returncode=1, stderr='write error'))

    with pytest.raises(RemoteCommandError) as error:
        await runner.run_checked(SECRET_COMMAND)

    assert 'SECRETKEYVALUE' not in str(error.value)


@pytest.mark.asyncio
async def test_long_commands_get_their_own_timeout():
    connection = FakeConnection()
    await SshRunner(connection).run_checked('curl -fsSL https://get.docker.com | sh', timeout=600)
    assert connection.timeouts == [600]


@pytest.mark.asyncio
async def test_a_hanging_command_becomes_a_command_error():
    runner = SshRunner(FakeConnection(hang=True))

    with pytest.raises(RemoteCommandError) as error:
        await runner.run_checked('docker compose up -d', timeout=30)

    assert '30' in str(error.value)


class FakeProxy:
    instances: list[FakeProxy] = []

    def __init__(self, url: str) -> None:
        self.url = url
        self.targets: list[tuple[str, int]] = []
        FakeProxy.instances.append(self)

    @classmethod
    def from_url(cls, url: str) -> FakeProxy:
        return cls(url)

    async def connect(self, dest_host: str, dest_port: int, timeout: float | None = None) -> str:
        self.targets.append((dest_host, dest_port))
        return 'socket-through-proxy'


def _capture_asyncssh(monkeypatch) -> dict:
    captured: dict = {}

    @contextlib.asynccontextmanager
    async def fake_connect(host, **kwargs):
        captured['host'] = host
        captured.update(kwargs)
        yield FakeConnection()

    monkeypatch.setattr(ssh.asyncssh, 'connect', fake_connect)
    monkeypatch.setattr(ssh, 'Proxy', FakeProxy)
    FakeProxy.instances.clear()
    return captured


@pytest.mark.asyncio
async def test_connection_goes_through_the_egress_proxy(monkeypatch):
    captured = _capture_asyncssh(monkeypatch)

    async with ssh.connect('203.0.113.10', client_keys=['/keys/id'], proxy_url='socks5://xray:1080'):
        pass

    (proxy,) = FakeProxy.instances
    assert proxy.url == 'socks5://xray:1080'
    assert proxy.targets == [('203.0.113.10', 22)]
    assert captured['sock'] == 'socket-through-proxy'
    assert captured['username'] == 'root'


@pytest.mark.asyncio
async def test_without_a_proxy_the_connection_is_direct(monkeypatch):
    captured = _capture_asyncssh(monkeypatch)

    async with ssh.connect('172.28.0.200', client_keys=['/keys/id']):
        pass

    assert FakeProxy.instances == []
    assert captured.get('sock') is None
