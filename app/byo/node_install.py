"""Установка ноды Remnawave на сервере владельца.

``render_node_files`` собирает три файла (compose ноды с SECRET_KEY, Caddyfile,
compose Caddy), ``install_commands`` — список shell-команд для выполнения по SSH:
поставить Docker, записать файлы, настроить ufw, поднять контейнеры.

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
# SECRET_KEY от панели — base64url без кавычек и пробелов; что угодно иное подозрительно.
SECRET_KEY_PATTERN = re.compile(r'[A-Za-z0-9_-]+')


class InstallSpec(NamedTuple):
    fqdn: str
    ws_path: str
    inbound_port: int
    secret_key: str
    gateway_ip: str


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
        raise ValueError('SECRET_KEY должен быть строкой base64url без кавычек и пробелов')
    return {
        NODE_COMPOSE: _node_compose(spec),
        CADDY_FILE: _caddyfile(spec),
        CADDY_COMPOSE: _caddy_compose(),
    }


def _write_file_command(path: str, content: str) -> str:
    # Пишем через base64, чтобы содержимое (в т. ч. SECRET_KEY) не ломалось о кавычки.
    encoded = base64.b64encode(content.encode()).decode()
    directory = path.rsplit('/', 1)[0]
    return f'mkdir -p {directory} && echo {encoded} | base64 -d > {path}'


def install_commands(spec: InstallSpec) -> list[str]:
    files = render_node_files(spec)
    return [
        # Docker ставится официальным скриптом, если его ещё нет.
        'command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh',
        _write_file_command(NODE_COMPOSE, files[NODE_COMPOSE]),
        _write_file_command(CADDY_FILE, files[CADDY_FILE]),
        _write_file_command(CADDY_COMPOSE, files[CADDY_COMPOSE]),
        # Минимальные образы хостеров приходят без ufw.
        'command -v ufw >/dev/null || (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw)',
        'ufw allow 22/tcp',
        'ufw allow 80/tcp',
        'ufw allow 443/tcp',
        f'ufw allow from {spec.gateway_ip} to any port {NODE_PORT}',
        'ufw --force enable',
        f'docker compose -f {CADDY_COMPOSE} up -d',
        f'docker compose -f {NODE_COMPOSE} up -d',
    ]
