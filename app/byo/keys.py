"""Наш SSH-ключ к серверам владельцев.

Ключ ed25519 создаётся один раз при первом старте и дальше только читается:
смена ключа отрезала бы бота от всех уже подключённых серверов. Поэтому он лежит
в томе ``data`` (переживает пересборку образа) с правами 600. Владельцам
показывается только публичная строка — её они добавляют на свой VPS.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncssh


KEY_ALGORITHM = 'ssh-ed25519'
KEY_COMMENT = 'byo'
PRIVATE_KEY_MODE = 0o600
KEY_DIRECTORY_MODE = 0o700


def load_or_create_key(path: Path) -> asyncssh.SSHKey:
    if path.exists():
        return asyncssh.read_private_key(path)
    key = asyncssh.generate_private_key(KEY_ALGORITHM, comment=KEY_COMMENT)
    path.parent.mkdir(parents=True, exist_ok=True, mode=KEY_DIRECTORY_MODE)
    # O_EXCL: два одновременных старта не перепишут ключ друг другу.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_KEY_MODE)
    with os.fdopen(descriptor, 'wb') as file:
        file.write(key.export_private_key())
    return key


def public_key_line(key: asyncssh.SSHKey) -> str:
    return key.export_public_key().decode().strip()
