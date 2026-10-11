"""Установка ноды Remnawave на сервере владельца.

``render_node_files`` собирает три файла (compose ноды с SECRET_KEY, Caddyfile,
compose Caddy), ``install_commands`` — список shell-команд для выполнения по SSH:
поставить Docker, записать файлы, настроить ufw, поднять контейнеры. У каждой
команды есть название (оно, а не сама команда, попадает в журнал) и тайм-аут.
``tls_check_command`` проверяет, что домен ноды уже отдаёт настоящий сертификат.

Нода слушает 2222 на host-сети; Caddy держит 80/443 и отдаёт секретный путь в
инбаунд, а на всё прочее — обычный ответ (сервер выглядит как настоящий сайт).
Порт 2222 открыт только со входа. SECRET_KEY пишется без кавычек (грабли §23.2).
"""

from __future__ import annotations

import base64
import re
from typing import NamedTuple


NODE_IMAGE = 'remnawave/node:3.4.1'
CADDY_IMAGE = 'caddy:2.11.7-alpine'
NODE_PORT = 2222
NODE_DIR = '/opt/remnanode'
CADDY_DIR = '/opt/webvpn'
NODE_COMPOSE = f'{NODE_DIR}/docker-compose.yml'
CADDY_FILE = f'{CADDY_DIR}/Caddyfile'
CADDY_COMPOSE = f'{CADDY_DIR}/docker-compose.yml'
# SECRET_KEY от панели — base64 от JSON (с паддингом «=»): без кавычек, пробелов и
# переводов строк. Что угодно иное подозрительно и сломает compose.
SECRET_KEY_PATTERN = re.compile(r'[A-Za-z0-9+/=_-]+')
# Docker ставится, а образы качаются минутами; прочие команды — секунды.
SLOW_COMMAND_SECONDS = 600
QUICK_COMMAND_SECONDS = 60
TLS_CHECK_SECONDS = 20


class InstallSpec(NamedTuple):
    fqdn: str
    ws_path: str
    inbound_port: int
    secret_key: str
    gateway_ip: str


class RemoteCommand(NamedTuple):
    label: str
    command: str
    timeout: int


def _node_compose(spec: InstallSpec) -> str:
    return f"""\
services:
  remnanode:
    image: {NODE_IMAGE}
    container_name: remnanode
    hostname: remnanode
    network_mode: host
    restart: always
    environment:
      - NODE_PORT={NODE_PORT}
      - SECRET_KEY={spec.secret_key}
"""


def _caddyfile(spec: InstallSpec) -> str:
    return f"""\
{{
    admin off
}}

{spec.fqdn} {{
    encode gzip
    handle {spec.ws_path}* {{
        reverse_proxy 127.0.0.1:{spec.inbound_port}
    }}
    handle {{
        respond "OK" 200
    }}
}}
"""


def _caddy_compose() -> str:
    return f"""\
services:
  caddy:
    image: {CADDY_IMAGE}
    container_name: webvpn-caddy
    network_mode: host
    restart: always
    mem_limit: 64m
    volumes:
      - {CADDY_FILE}:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
"""


def render_node_files(spec: InstallSpec) -> dict[str, str]:
    if not SECRET_KEY_PATTERN.fullmatch(spec.secret_key):
        raise ValueError('SECRET_KEY должен быть строкой base64 без кавычек, пробелов и переводов строк')
    return {
        NODE_COMPOSE: _node_compose(spec),
        CADDY_FILE: _caddyfile(spec),
        CADDY_COMPOSE: _caddy_compose(),
    }


def _write_file(label: str, path: str, content: str) -> RemoteCommand:
    # Пишем через base64, чтобы содержимое (в т. ч. SECRET_KEY) не ломалось о кавычки.
    encoded = base64.b64encode(content.encode()).decode()
    directory = path.rsplit('/', 1)[0]
    return RemoteCommand(label, f'mkdir -p {directory} && echo {encoded} | base64 -d > {path}', QUICK_COMMAND_SECONDS)


def _firewall(spec: InstallSpec) -> list[RemoteCommand]:
    rules = [
        'ufw allow 22/tcp',
        'ufw allow 80/tcp',
        'ufw allow 443/tcp',
        f'ufw allow from {spec.gateway_ip} to any port {NODE_PORT}',
        'ufw --force enable',
    ]
    return [RemoteCommand('настройка ufw', rule, QUICK_COMMAND_SECONDS) for rule in rules]


def install_commands(spec: InstallSpec) -> list[RemoteCommand]:
    files = render_node_files(spec)
    return [
        RemoteCommand(
            'установка Docker',
            'command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh',
            SLOW_COMMAND_SECONDS,
        ),
        _write_file('запись настроек ноды', NODE_COMPOSE, files[NODE_COMPOSE]),
        _write_file('запись настроек Caddy', CADDY_FILE, files[CADDY_FILE]),
        _write_file('запись настроек Caddy', CADDY_COMPOSE, files[CADDY_COMPOSE]),
        # Минимальные образы хостеров приходят без ufw.
        RemoteCommand(
            'установка ufw',
            'command -v ufw >/dev/null || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw)',
            SLOW_COMMAND_SECONDS,
        ),
        *_firewall(spec),
        RemoteCommand('запуск Caddy', f'docker compose -f {CADDY_COMPOSE} up -d', SLOW_COMMAND_SECONDS),
        RemoteCommand('запуск ноды', f'docker compose -f {NODE_COMPOSE} up -d', SLOW_COMMAND_SECONDS),
    ]


def tls_check_command(fqdn: str) -> str:
    """Успешна, только когда домен смотрит на сервер и Caddy уже получил сертификат."""
    return f'curl -f -sS -o /dev/null --max-time {TLS_CHECK_SECONDS} https://{fqdn}/'
