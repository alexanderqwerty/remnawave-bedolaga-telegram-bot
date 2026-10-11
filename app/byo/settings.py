"""Настройки BYO из окружения (префикс ``BYO_``).

Модуль включается ``BYO_ENABLED=true`` и работает, только когда заданы все
обязательные значения. Ошибка в настройках не роняет бота при импорте: модуль
остаётся выключенным (кнопки в меню нет), а ``problems`` объясняет почему.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.byo.connect import ConnectSettings


SOCKS5_PREFIX = 'socks5://'


class ByoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='BYO_', env_file='.env', env_file_encoding='utf-8', extra='ignore')

    enabled: bool = False
    # Домены нод: <label>.<node_zone>; зона лежит внутри домена, которым управляет reg.ru.
    node_zone: str = ''
    dns_domain: str = ''
    # Адрес VPS-входа: только с него на нодах открыт порт 2222.
    gateway_ip: str = ''
    # SOCKS-выход входа: через него идут SSH, reg.ru и подключение панели к нодам.
    egress_proxy_url: str = ''
    # Наш SSH-ключ к серверам владельцев; создаётся при первом старте, живёт в томе data.
    ssh_key_path: str = 'data/byo/id_ed25519'
    regru_username: str = ''
    regru_password: SecretStr = SecretStr('')
    max_nodes_per_owner: int = 3
    max_new_nodes_per_day: int = 3

    def problems(self) -> list[str]:
        required = {
            'BYO_NODE_ZONE': self.node_zone,
            'BYO_DNS_DOMAIN': self.dns_domain,
            'BYO_GATEWAY_IP': self.gateway_ip,
            'BYO_EGRESS_PROXY_URL': self.egress_proxy_url,
            'BYO_REGRU_USERNAME': self.regru_username,
            'BYO_REGRU_PASSWORD': self.regru_password.get_secret_value(),
        }
        problems = [f'не задан {name}' for name, value in required.items() if not value.strip()]
        if self.egress_proxy_url and not self.egress_proxy_url.startswith(SOCKS5_PREFIX):
            problems.append('BYO_EGRESS_PROXY_URL должен быть socks5://хост:порт')
        if self.node_zone and self.dns_domain:
            try:
                self.connect_settings()
            except ValueError as error:
                problems.append(f'BYO_NODE_ZONE: {error}')
        return problems

    @property
    def is_active(self) -> bool:
        return self.enabled and not self.problems()

    def connect_settings(self) -> ConnectSettings:
        return ConnectSettings(
            zone=self.node_zone,
            dns_domain=self.dns_domain,
            gateway_ip=self.gateway_ip,
            proxy_url=self.egress_proxy_url or None,
        )


byo_settings = ByoSettings()
