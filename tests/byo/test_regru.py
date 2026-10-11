"""Клиент reg.ru: A-запись поддомена ноды.

Бот заводит ``<label>.vpn.miet-tv.ru -> <IP VPS>`` через REST API reg.ru
(``zone/add_alias``) и снимает её при удалении ноды (``zone/remove_record``).
HTTP подменён ``httpx.MockTransport``, поэтому проверяются форма запроса и разбор
ответа без обращения к сети и без настоящих учётных данных.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.byo.regru import RegruClient, RegruError


ZONE_DOMAIN = 'miet-tv.ru'
SUBDOMAIN = 'abc123.vpn'
NODE_IP = '203.0.113.10'
SUCCESS = {'result': 'success', 'answer': {'domains': [{'dname': ZONE_DOMAIN, 'result': 'success'}]}}


class Capture:
    """Запоминает последний запрос и отдаёт заданный ответ."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else SUCCESS
        self.request: httpx.Request | None = None
        self.fields: dict[str, str] = {}
        self.input_data: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        self.fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if 'input_data' in self.fields:
            self.input_data = json.loads(self.fields['input_data'])
        return httpx.Response(self.status, json=self.body)


def _client(capture: Capture) -> RegruClient:
    transport = httpx.MockTransport(capture.handler)
    http = httpx.AsyncClient(transport=transport)
    return RegruClient(username='api-user', password='api-pass', http=http)


@pytest.mark.asyncio
async def test_add_record_posts_to_add_alias_with_credentials_and_data():
    capture = Capture()
    async with _client(capture) as client:
        await client.add_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)

    assert capture.request.method == 'POST'
    assert str(capture.request.url).endswith('/zone/add_alias')
    assert capture.fields['username'] == 'api-user'
    assert capture.fields['password'] == 'api-pass'
    assert capture.fields['input_format'] == 'json'
    assert capture.input_data['domains'] == [{'dname': ZONE_DOMAIN}]
    assert capture.input_data['subdomain'] == SUBDOMAIN
    assert capture.input_data['ipaddr'] == NODE_IP


@pytest.mark.asyncio
async def test_remove_record_posts_to_remove_record_with_type_and_content():
    capture = Capture()
    async with _client(capture) as client:
        await client.remove_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)

    assert str(capture.request.url).endswith('/zone/remove_record')
    assert capture.input_data['subdomain'] == SUBDOMAIN
    assert capture.input_data['content'] == NODE_IP
    assert capture.input_data['record_type'] == 'A'


@pytest.mark.asyncio
async def test_credentials_never_appear_in_input_data():
    # input_data логируется при отладке — пароль туда попадать не должен.
    capture = Capture()
    async with _client(capture) as client:
        await client.add_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)

    assert 'password' not in capture.input_data
    assert 'api-pass' not in json.dumps(capture.input_data)


@pytest.mark.asyncio
async def test_api_level_error_raises_with_reason():
    capture = Capture(body={'result': 'error', 'error_code': 'NO_SUCH_SERVICE', 'error_text': 'нет такого домена'})
    async with _client(capture) as client:
        with pytest.raises(RegruError) as error:
            await client.add_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)

    assert 'NO_SUCH_SERVICE' in str(error.value) or 'нет такого домена' in str(error.value)


@pytest.mark.asyncio
async def test_per_domain_error_raises_even_when_top_result_is_success():
    capture = Capture(
        body={
            'result': 'success',
            'answer': {'domains': [{'dname': ZONE_DOMAIN, 'result': 'error', 'error_text': 'zone is locked'}]},
        }
    )
    async with _client(capture) as client:
        with pytest.raises(RegruError) as error:
            await client.add_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)

    assert 'zone is locked' in str(error.value)


@pytest.mark.asyncio
async def test_http_error_status_raises():
    capture = Capture(status=403, body={'result': 'error', 'error_text': 'IP не в белом списке'})
    async with _client(capture) as client:
        with pytest.raises(RegruError):
            await client.add_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)


@pytest.mark.asyncio
async def test_success_returns_without_raising():
    capture = Capture()
    async with _client(capture) as client:
        await client.add_record(ZONE_DOMAIN, SUBDOMAIN, NODE_IP)  # не бросает — и этого достаточно
