"""Установка ноды на сервере владельца по SSH.

По спецификации ноды и SECRET_KEY из панели ``render_node_files`` собирает три
файла (compose ноды, Caddyfile, compose Caddy), а ``install_commands`` — список
команд: поставить Docker, записать файлы, открыть ufw (22/80/443 всем, 2222
только со входа), поднять контейнеры. Здесь проверяется содержимое файлов и
команд; настоящий SSH подменён.
"""

from __future__ import annotations

import pytest

from app.byo.node_install import InstallSpec, install_commands, render_node_files


SPEC = InstallSpec(
    fqdn='abc123.vpn.miet-tv.ru',
    ws_path='/secretpath',
    inbound_port=8501,
    secret_key='SECRETKEYFROMPANEL',
    gateway_ip='194.87.254.244',
)
FILES = render_node_files(SPEC)


def test_node_compose_pins_the_image_and_keeps_secret_key_unquoted():
    compose = FILES['/opt/remnanode/docker-compose.yml']
    assert 'remnawave/node:3.4.1' in compose
    # SECRET_KEY без кавычек — иначе нода не стартует (грабли §23.2).
    assert 'SECRET_KEY=SECRETKEYFROMPANEL' in compose
    assert 'SECRET_KEY="' not in compose
    assert "SECRET_KEY='" not in compose


def test_node_runs_on_host_network_so_it_can_bind_2222():
    assert 'network_mode: host' in FILES['/opt/remnanode/docker-compose.yml']


def test_caddyfile_serves_the_fqdn_and_proxies_the_ws_path():
    caddy = FILES['/opt/webvpn/Caddyfile']
    assert 'abc123.vpn.miet-tv.ru {' in caddy
    assert 'handle /secretpath*' in caddy
    assert 'reverse_proxy 127.0.0.1:8501' in caddy
    assert 'admin off' in caddy


def test_caddyfile_answers_other_paths_so_it_looks_like_a_site():
    caddy = FILES['/opt/webvpn/Caddyfile']
    # На всё, кроме секретного пути, — обычный ответ, а не ошибка.
    assert 'respond' in caddy


def test_secret_key_appears_only_in_the_node_compose():
    for path, content in FILES.items():
        if path != '/opt/remnanode/docker-compose.yml':
            assert 'SECRETKEYFROMPANEL' not in content, f'ключ просочился в {path}'


def test_install_opens_the_required_ports():
    commands = '\n'.join(install_commands(SPEC))
    assert 'ufw allow 22/tcp' in commands
    assert 'ufw allow 80/tcp' in commands
    assert 'ufw allow 443/tcp' in commands


def test_ufw_is_installed_before_any_rule():
    # Минимальные образы хостеров (и чистый Debian) приходят без ufw.
    commands = install_commands(SPEC)
    install = next(i for i, c in enumerate(commands) if 'apt-get install' in c and 'ufw' in c)
    first_rule = next(i for i, c in enumerate(commands) if c.startswith('ufw '))
    assert install < first_rule


def test_port_2222_is_restricted_to_the_gateway():
    commands = '\n'.join(install_commands(SPEC))
    assert 'ufw allow from 194.87.254.244 to any port 2222' in commands
    # И не открыт всем подряд.
    assert 'ufw allow 2222/tcp' not in commands


def test_install_brings_up_both_stacks():
    commands = '\n'.join(install_commands(SPEC))
    assert commands.count('docker compose') >= 2
    assert '/opt/remnanode' in commands
    assert '/opt/webvpn' in commands


def test_install_sets_up_docker_before_compose():
    commands = install_commands(SPEC)
    docker_setup = next(i for i, c in enumerate(commands) if 'get.docker.com' in c or 'docker-ce' in c)
    first_compose = next(i for i, c in enumerate(commands) if 'docker compose' in c and 'up' in c)
    assert docker_setup < first_compose


@pytest.mark.parametrize('secret', ['has space', 'has"quote', "has'quote", 'has\nnewline'])
def test_a_malformed_secret_key_is_rejected(secret: str):
    with pytest.raises(ValueError):
        render_node_files(SPEC._replace(secret_key=secret))
