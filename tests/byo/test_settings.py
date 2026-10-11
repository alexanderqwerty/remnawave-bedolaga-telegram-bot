"""Настройки BYO из окружения (префикс ``BYO_``).

Модуль работает, только когда он включён и заданы все обязательные значения.
Ошибка в настройках не должна ронять бота при импорте: модуль просто остаётся
выключенным, а ``problems`` объясняет почему.
"""

from __future__ import annotations

from app.byo.settings import ByoSettings


COMPLETE = {
    'enabled': True,
    'node_zone': 'vpn.miet-tv.ru',
    'dns_domain': 'miet-tv.ru',
    'gateway_ip': '194.87.254.244',
    'egress_proxy_url': 'socks5://xray:1080',
    'regru_username': 'api-user',
    'regru_password': 'api-pass',
}


def _settings(**overrides) -> ByoSettings:
    return ByoSettings(_env_file=None, **{**COMPLETE, **overrides})


def test_the_module_is_off_by_default():
    settings = ByoSettings(_env_file=None)
    assert not settings.is_active


def test_complete_settings_switch_the_module_on():
    settings = _settings()
    assert settings.problems() == []
    assert settings.is_active


def test_missing_values_are_named_by_their_variables():
    settings = _settings(gateway_ip='', regru_password='')
    assert not settings.is_active
    assert any('BYO_GATEWAY_IP' in problem for problem in settings.problems())
    assert any('BYO_REGRU_PASSWORD' in problem for problem in settings.problems())


def test_the_egress_must_be_a_socks5_proxy():
    # Панель принимает proxyUrl ноды только в виде socks5://хост:порт.
    settings = _settings(egress_proxy_url='http://xray:8888')
    assert not settings.is_active
    assert any('BYO_EGRESS_PROXY_URL' in problem for problem in settings.problems())


def test_the_node_zone_must_be_inside_the_dns_domain():
    settings = _settings(node_zone='vpn.example.org')
    assert not settings.is_active
    assert any('BYO_NODE_ZONE' in problem for problem in settings.problems())


def test_connect_settings_are_built_from_the_environment():
    connect = _settings().connect_settings()
    assert connect.zone == 'vpn.miet-tv.ru'
    assert connect.dns_domain == 'miet-tv.ru'
    assert connect.gateway_ip == '194.87.254.244'
    assert connect.proxy_url == 'socks5://xray:1080'


def test_the_reg_ru_password_never_shows_in_repr():
    assert 'api-pass' not in repr(_settings())


def test_values_come_from_byo_prefixed_variables(monkeypatch):
    monkeypatch.setenv('BYO_ENABLED', 'true')
    monkeypatch.setenv('BYO_GATEWAY_IP', '194.87.254.244')
    settings = ByoSettings(_env_file=None)
    assert settings.enabled is True
    assert settings.gateway_ip == '194.87.254.244'


def test_limits_default_to_the_plan():
    settings = _settings()
    assert settings.max_nodes_per_owner == 3
    assert settings.max_new_nodes_per_day == 3
