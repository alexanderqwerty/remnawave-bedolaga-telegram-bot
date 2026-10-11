"""Проверка VPS перед установкой ноды.

Бот заходит на сервер и собирает факты (ОС, память, занятость портов, страна),
а чистая функция ``evaluate`` превращает их в вердикт «годен / не годен» с
понятной владельцу причиной. SSH здесь не участвует — только разбор фактов.
Требования из плана: Debian/Ubuntu, RAM ≥ 1 ГБ, свободны 80/443/2222, IP не в РФ.
"""

from __future__ import annotations

import pytest

from app.byo.vps_check import VpsFacts, evaluate


GOOD = VpsFacts(
    os_id='debian',
    os_version='13',
    total_memory_mb=1024,
    busy_ports=(),
    country_code='DE',
)


def test_a_fresh_debian_passes():
    verdict = evaluate(GOOD)
    assert verdict.ok
    assert verdict.reason is None


def test_ubuntu_also_passes():
    assert evaluate(GOOD._replace(os_id='ubuntu', os_version='24.04')).ok


def test_other_distros_are_rejected():
    verdict = evaluate(GOOD._replace(os_id='centos'))
    assert not verdict.ok
    assert 'debian' in verdict.reason.lower() or 'ubuntu' in verdict.reason.lower()


def test_too_little_memory_is_rejected():
    verdict = evaluate(GOOD._replace(total_memory_mb=512))
    assert not verdict.ok
    assert 'пам' in verdict.reason.lower() or 'ram' in verdict.reason.lower()


def test_slightly_under_a_gib_still_passes():
    # Хостеры отдают «1 ГБ» как ~970 МБ; порог ниже круглого гигабайта.
    assert evaluate(GOOD._replace(total_memory_mb=960)).ok


@pytest.mark.parametrize('port', [80, 443, 2222])
def test_a_busy_required_port_is_rejected(port: int):
    verdict = evaluate(GOOD._replace(busy_ports=(port,)))
    assert not verdict.ok
    assert str(port) in verdict.reason


def test_other_busy_ports_do_not_matter():
    assert evaluate(GOOD._replace(busy_ports=(22, 25, 8080))).ok


def test_a_russian_ip_is_rejected():
    verdict = evaluate(GOOD._replace(country_code='RU'))
    assert not verdict.ok
    assert 'рф' in verdict.reason.lower() or 'росси' in verdict.reason.lower() or 'ru' in verdict.reason.lower()


def test_unknown_country_is_rejected_to_stay_safe():
    # Не смогли определить страну — не рискуем: нода в РФ ляжет под ТСПУ.
    verdict = evaluate(GOOD._replace(country_code=None))
    assert not verdict.ok


def test_the_first_failing_requirement_is_reported():
    # Несколько причин сразу — владельцу показываем одну, но конкретную.
    verdict = evaluate(GOOD._replace(os_id='centos', total_memory_mb=256, country_code='RU'))
    assert not verdict.ok
    assert verdict.reason
