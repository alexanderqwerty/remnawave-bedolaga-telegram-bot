"""Секреты и имена одной ноды владельца.

``build_node_secrets`` выбирает для ноды случайный label (поддомен) и путь
WebSocket, собирает fqdn и тег ``o-<slug>-<n>-tws`` и выводит порт инбаунда из
владельца и номера ноды. Случайность берётся из переданного генератора
(протокол ``Rng``) — в бою это обёртка над ``secrets``, в тестах предсказуемый.
Никаких паролей и ключей здесь нет: label и path публичны.
"""

from __future__ import annotations

import re
import secrets
from typing import NamedTuple, Protocol


LABEL_LENGTH = 8
WS_PATH_LENGTH = 12
SLUG_PATTERN = re.compile(r'[a-z0-9][a-z0-9-]{0,30}[a-z0-9]')
# Диапазон портов инбаундов: приватный верх, подальше от занятых сервисов.
PORT_BASE = 8500
PORT_SPAN = 1000
MAX_NODES_PER_OWNER = 10


class Rng(Protocol):
    def token(self, length: int) -> str: ...


class _SecretsRng:
    def token(self, length: int) -> str:
        # token_hex(n) даёт 2n hex-символов; берём нужную длину.
        return secrets.token_hex(length)[:length]


def default_rng() -> Rng:
    return _SecretsRng()


class NodeSecrets(NamedTuple):
    label: str
    fqdn: str
    ws_path: str
    inbound_port: int
    inbound_tag: str


def _port_for(slug: str, ordinal: int) -> int:
    # Детерминированный порт: один владелец + номер → один и тот же порт.
    seed = f'{slug}:{ordinal}'
    offset = sum(ord(char) * (index + 1) for index, char in enumerate(seed)) % PORT_SPAN
    return PORT_BASE + offset


def build_node_secrets(slug: str, ordinal: int, zone: str, rng: Rng | None = None) -> NodeSecrets:
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError(f'недопустимый slug владельца: {slug!r}')
    if not 1 <= ordinal <= MAX_NODES_PER_OWNER:
        raise ValueError(f'номер ноды вне диапазона 1..{MAX_NODES_PER_OWNER}: {ordinal}')

    rng = rng or default_rng()
    label = rng.token(LABEL_LENGTH)
    return NodeSecrets(
        label=label,
        fqdn=f'{label}.{zone}',
        ws_path='/' + rng.token(WS_PATH_LENGTH),
        inbound_port=_port_for(slug, ordinal),
        inbound_tag=f'o-{slug}-{ordinal}-tws',
    )
