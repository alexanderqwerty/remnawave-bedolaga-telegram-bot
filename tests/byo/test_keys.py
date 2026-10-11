"""Наш SSH-ключ к серверам владельцев.

Ключ создаётся один раз при первом старте и дальше только читается: если он
сменится, бот потеряет доступ ко всем уже подключённым серверам. Приватная часть
лежит с правами 600, владельцам показывается только публичная строка.
"""

from __future__ import annotations

import stat

from app.byo.keys import load_or_create_key, public_key_line


def test_a_missing_key_is_created_private(tmp_path):
    path = tmp_path / 'byo' / 'id_ed25519'

    load_or_create_key(path)

    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_existing_key_is_reused_not_replaced(tmp_path):
    path = tmp_path / 'id_ed25519'
    first = public_key_line(load_or_create_key(path))
    again = public_key_line(load_or_create_key(path))
    assert first == again


def test_owners_get_a_single_line_ed25519_public_key(tmp_path):
    line = public_key_line(load_or_create_key(tmp_path / 'id_ed25519'))
    assert line.startswith('ssh-ed25519 ')
    assert '\n' not in line
    assert 'PRIVATE' not in line
