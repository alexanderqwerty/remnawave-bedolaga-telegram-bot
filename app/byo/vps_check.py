"""Вердикт по VPS владельца: годится ли сервер под ноду.

``VpsFacts`` — факты, собранные на сервере по SSH. ``evaluate`` — чистая функция,
превращающая их в ``Verdict`` с первой нарушенной причиной (по-русски, для
владельца). Требования из плана: Debian/Ubuntu, RAM ≥ 1 ГБ, свободны 80/443/2222,
IP не в России.
"""

from __future__ import annotations

from typing import NamedTuple


SUPPORTED_OS_IDS = frozenset({'debian', 'ubuntu'})
# Хостеры показывают «1 ГБ» как ~950–1000 МБ, поэтому порог ниже круглого гигабайта.
MIN_MEMORY_MB = 900
REQUIRED_FREE_PORTS = (80, 443, 2222)
BLOCKED_COUNTRIES = frozenset({'RU'})


class VpsFacts(NamedTuple):
    os_id: str
    os_version: str
    total_memory_mb: int
    busy_ports: tuple[int, ...]
    # ISO-код страны по IP, либо None, если определить не удалось.
    country_code: str | None


class Verdict(NamedTuple):
    ok: bool
    reason: str | None


def _first_problem(facts: VpsFacts) -> str | None:
    if facts.os_id.lower() not in SUPPORTED_OS_IDS:
        return f'Нужен Debian или Ubuntu, а на сервере {facts.os_id or "неизвестная система"}.'
    if facts.total_memory_mb < MIN_MEMORY_MB:
        return f'Нужно хотя бы 1 ГБ памяти, на сервере {facts.total_memory_mb} МБ.'
    busy_required = [port for port in REQUIRED_FREE_PORTS if port in facts.busy_ports]
    if busy_required:
        ports = ', '.join(str(port) for port in busy_required)
        return f'Порты {ports} должны быть свободны, но они заняты.'
    if facts.country_code is None:
        return 'Не удалось определить страну сервера — подключить его нельзя.'
    if facts.country_code.upper() in BLOCKED_COUNTRIES:
        return 'Сервер в России не подойдёт: такую ноду заблокирует ТСПУ. Нужен сервер за пределами РФ.'
    return None


def evaluate(facts: VpsFacts) -> Verdict:
    problem = _first_problem(facts)
    return Verdict(ok=problem is None, reason=problem)
