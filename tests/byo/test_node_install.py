"""Установка ноды на сервере владельца по SSH.

По спецификации ноды и SECRET_KEY из панели ``render_node_files`` собирает три
файла (compose ноды, Caddyfile, compose Caddy), а ``install_commands`` — список
команд: поставить Docker, записать файлы, открыть ufw (22/80/443 всем, 2222
только со входа), поднять контейнеры. У каждой команды есть название для журнала
и свой тайм-аут. ``tls_check_command`` проверяет, что домен ноды уже отдаёт
настоящий сертификат. Здесь проверяется содержимое файлов и команд; настоящий
SSH подменён.
"""

from __future__ import annotations

import pytest

from app.byo.node_install import InstallSpec, install_commands, render_node_files, tls_check_command


SPEC = InstallSpec(
    fqdn='abc123.vpn.miet-tv.ru',
    ws_path='/secretpath',
    inbound_port=8501,
    secret_key='SECRETKEYFROMPANEL',
    gateway_ip='194.87.254.244',
)
FILES = render_node_files(SPEC)
COMMANDS = install_commands(SPEC)
SCRIPT = '\n'.join(step.command for step in COMMANDS)
LONG_RUNNING_SECONDS = 300


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
    assert 'ufw allow 22/tcp' in SCRIPT
    assert 'ufw allow 80/tcp' in SCRIPT
    assert 'ufw allow 443/tcp' in SCRIPT


def test_ufw_is_installed_before_any_rule():
    # Минимальные образы хостеров (и чистый Debian) приходят без ufw.
    commands = [step.command for step in COMMANDS]
    install = next(i for i, c in enumerate(commands) if 'apt-get install' in c and 'ufw' in c)
    first_rule = next(i for i, c in enumerate(commands) if c.startswith('ufw '))
    assert install < first_rule


def test_port_2222_is_restricted_to_the_gateway():
    assert 'ufw allow from 194.87.254.244 to any port 2222' in SCRIPT
    # И не открыт всем подряд.
    assert 'ufw allow 2222/tcp' not in SCRIPT


def test_install_brings_up_both_stacks():
    assert SCRIPT.count('docker compose') >= 2
    assert '/opt/remnanode' in SCRIPT
    assert '/opt/webvpn' in SCRIPT


def test_install_sets_up_docker_before_compose():
    commands = [step.command for step in COMMANDS]
    docker_setup = next(i for i, c in enumerate(commands) if 'get.docker.com' in c or 'docker-ce' in c)
    first_compose = next(i for i, c in enumerate(commands) if 'docker compose' in c and 'up' in c)
    assert docker_setup < first_compose


def test_every_command_has_a_label_for_the_journal():
    # В журнал и владельцу уходит название шага, а не сама команда с секретами.
    for step in COMMANDS:
        assert step.label.strip()
        assert 'SECRETKEYFROMPANEL' not in step.label


def test_slow_commands_get_long_timeouts():
    # Docker ставится и образы качаются минутами — короткий тайм-аут оборвал бы установку.
    for step in COMMANDS:
        if 'get.docker.com' in step.command or 'apt-get' in step.command or ' up -d' in step.command:
            assert step.timeout >= LONG_RUNNING_SECONDS, step.label
        assert step.timeout > 0


def test_tls_check_verifies_the_real_certificate_of_the_domain():
    command = tls_check_command('abc123.vpn.miet-tv.ru')
    assert 'https://abc123.vpn.miet-tv.ru/' in command
    assert command.startswith('curl ')
    assert '-f' in command.split(), 'код ответа ≥ 400 должен считаться ошибкой'
    assert '--insecure' not in command and ' -k' not in command


@pytest.mark.parametrize('secret', ['has space', 'has"quote', "has'quote", 'has\nnewline'])
def test_a_malformed_secret_key_is_rejected(secret: str):
    with pytest.raises(ValueError):
        render_node_files(SPEC._replace(secret_key=secret))


@pytest.mark.parametrize('secret', ['eyJub2RlQ2VydFBlbSI6Ii0tLS0=', 'eyJhIjoiYiJ9+/abc==', 'eyJ-url_safe'])
def test_a_real_panel_key_is_accepted(secret: str):
    # Панель отдаёт SECRET_KEY как base64 от JSON: с паддингом «=» на конце,
    # в общем случае — с «+» и «/». Всё это безопасно в compose без кавычек.
    compose = render_node_files(SPEC._replace(secret_key=secret))['/opt/remnanode/docker-compose.yml']
    assert f'SECRET_KEY={secret}\n' in compose
