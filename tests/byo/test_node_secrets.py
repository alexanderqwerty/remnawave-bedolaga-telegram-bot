"""Секреты и имена одной ноды.

Перед созданием объектов бот выбирает для ноды: случайный label (поддомен),
полный fqdn, случайный путь WebSocket, порт инбаунда и тег ``o-<slug>-<n>-tws``.
Здесь проверяются форма и инварианты этих значений. Случайность берётся из
переданного генератора, поэтому тест детерминирован.
"""

from __future__ import annotations

import re

import pytest

from app.byo.node_secrets import (
    MAX_NODES_PER_OWNER,
    MAX_SLUG_LENGTH,
    PANEL_NAME_LIMIT,
    NodeSecrets,
    build_node_secrets,
    node_name,
    squad_name,
)


ZONE = 'vpn.miet-tv.ru'


def _counter_rng() -> object:
    """Предсказуемый источник «случайности»: отдаёт a, b, c… посимвольно."""
    letters = iter('abcdefghijklmnopqrstuvwxyz0123456789')

    class Rng:
        def token(self, length: int) -> str:
            return ''.join(next(letters) for _ in range(length))

    return Rng()


def test_builds_all_fields_for_the_first_node():
    secrets = build_node_secrets(slug='lab-a', ordinal=1, zone=ZONE, rng=_counter_rng())

    assert isinstance(secrets, NodeSecrets)
    assert secrets.fqdn == f'{secrets.label}.{ZONE}'
    assert secrets.inbound_tag == 'o-lab-a-1-tws'
    assert secrets.ws_path.startswith('/')
    assert secrets.inbound_port > 0


def test_tag_encodes_slug_and_ordinal():
    assert build_node_secrets('miet', 1, ZONE, _counter_rng()).inbound_tag == 'o-miet-1-tws'
    assert build_node_secrets('miet', 2, ZONE, _counter_rng()).inbound_tag == 'o-miet-2-tws'


def test_label_is_random_lowercase_alnum():
    label = build_node_secrets('lab-a', 1, ZONE, _counter_rng()).label
    assert re.fullmatch(r'[a-z0-9]+', label)
    assert len(label) >= 8


def test_ws_path_is_a_random_hex_path():
    ws_path = build_node_secrets('lab-a', 1, ZONE, _counter_rng()).ws_path
    assert re.fullmatch(r'/[a-z0-9]+', ws_path)
    assert len(ws_path) >= 9  # '/' + >= 8


def test_label_and_path_differ():
    secrets = build_node_secrets('lab-a', 1, ZONE, _counter_rng())
    assert secrets.label not in secrets.ws_path


def test_port_is_stable_for_the_same_owner_and_ordinal():
    first = build_node_secrets('lab-a', 1, ZONE, _counter_rng()).inbound_port
    again = build_node_secrets('lab-a', 1, ZONE, _counter_rng()).inbound_port
    assert first == again, 'порт выводится из владельца и номера, не случаен'


def test_port_differs_between_two_nodes_of_one_owner():
    first = build_node_secrets('lab-a', 1, ZONE, _counter_rng()).inbound_port
    second = build_node_secrets('lab-a', 2, ZONE, _counter_rng()).inbound_port
    assert first != second


def test_port_stays_in_the_private_range():
    for ordinal in range(1, 4):
        port = build_node_secrets('lab-a', ordinal, ZONE, _counter_rng()).inbound_port
        assert 1024 <= port <= 65535


@pytest.mark.parametrize('bad_slug', ['', 'Lab-A', 'lab_a', 'лаб', 'a' * 40])
def test_invalid_slug_is_rejected(bad_slug: str):
    with pytest.raises(ValueError):
        build_node_secrets(bad_slug, 1, ZONE, _counter_rng())


def test_panel_names_follow_the_owner_convention():
    assert node_name('lab-a', 2) == 'o-lab-a-2'
    assert squad_name('lab-a') == 'o-lab-a'


def test_names_of_the_longest_slug_fit_the_panel_limit():
    # Панель принимает имена профилей, нод и сквадов не длиннее 30 символов.
    longest = 'a' * MAX_SLUG_LENGTH
    assert len(node_name(longest, MAX_NODES_PER_OWNER)) <= PANEL_NAME_LIMIT
    assert len(squad_name(longest)) <= PANEL_NAME_LIMIT


def test_a_slug_too_long_for_the_panel_is_rejected():
    with pytest.raises(ValueError):
        build_node_secrets('a' * (MAX_SLUG_LENGTH + 1), 1, ZONE, _counter_rng())
