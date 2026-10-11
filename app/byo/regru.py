"""Клиент REST API reg.ru для A-записей поддоменов нод.

``add_record`` заводит ``<subdomain>.<domain> -> IP`` через ``zone/add_alias``,
``remove_record`` снимает её. Учётные данные (логин + альтернативный пароль API)
уходят отдельными полями формы, не внутри ``input_data``. IP VPS-входа должен
быть в белом списке IP на странице «Настройки API» в кабинете reg.ru.

HTTP-клиент передаётся снаружи — в бою ``httpx.AsyncClient`` через egress-прокси,
в тестах ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from typing import Any, Self

import httpx


API_BASE_URL = 'https://api.reg.ru/api/regru2'
REQUEST_TIMEOUT_SECONDS = 30
A_RECORD = 'A'


class RegruError(RuntimeError):
    """reg.ru отклонил запрос (HTTP-ошибка или error в теле ответа)."""


class RegruClient:
    def __init__(self, username: str, password: str, *, http: httpx.AsyncClient) -> None:
        self._username = username
        self._password = password
        self._http = http

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def add_record(self, domain: str, subdomain: str, ip: str) -> None:
        await self._call(
            'zone/add_alias',
            {'domains': [{'dname': domain}], 'subdomain': subdomain, 'ipaddr': ip},
        )

    async def remove_record(self, domain: str, subdomain: str, ip: str) -> None:
        await self._call(
            'zone/remove_record',
            {'domains': [{'dname': domain}], 'subdomain': subdomain, 'content': ip, 'record_type': A_RECORD},
        )

    async def _call(self, method: str, input_data: dict[str, Any]) -> None:
        form = {
            'username': self._username,
            'password': self._password,
            'input_format': 'json',
            'input_data': json.dumps(input_data),
        }
        try:
            response = await self._http.post(f'{API_BASE_URL}/{method}', data=form, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            raise RegruError(f'reg.ru {method}: сетевая ошибка: {error}') from error

        self._raise_for_body(method, response)

    def _raise_for_body(self, method: str, response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            if response.is_success:
                return
            raise RegruError(f'reg.ru {method}: HTTP {response.status_code}') from None

        if body.get('result') != 'success':
            reason = body.get('error_text') or body.get('error_code') or f'HTTP {response.status_code}'
            raise RegruError(f'reg.ru {method}: {reason}')

        for domain in (body.get('answer') or {}).get('domains') or []:
            if domain.get('result') != 'success':
                reason = domain.get('error_text') or domain.get('error_code') or 'неизвестная ошибка'
                raise RegruError(f'reg.ru {method} ({domain.get("dname")}): {reason}')
