"""Запросы BYO к панели идут через общий клиент бота.

У бота уже есть клиент панели (``RemnaWaveAPI``) с повторами при 5xx и общей
паузой при 429. ``BotPanel`` пускает через него запросы BYO и снимает обёртку
``response``, в которую панель кладёт любой ответ.
"""

from __future__ import annotations

import pytest

from app.byo.panel_client import BotPanel


class FakeApi:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, dict | None]] = []

    async def _make_request(self, method: str, endpoint: str, data: dict | None = None) -> object:
        self.calls.append((method, endpoint, data))
        return self.payload


@pytest.mark.asyncio
async def test_request_goes_through_the_bot_client_and_unwraps_the_response():
    api = FakeApi({'response': {'uuid': 'squad-1'}})

    result = await BotPanel(api).request('POST', '/api/internal-squads', {'name': 'o-lab-a', 'inbounds': []})

    assert result == {'uuid': 'squad-1'}
    assert api.calls == [('POST', '/api/internal-squads', {'name': 'o-lab-a', 'inbounds': []})]


@pytest.mark.asyncio
async def test_list_responses_are_unwrapped_too():
    api = FakeApi({'response': [{'uuid': 'node-1'}]})
    assert await BotPanel(api).request('GET', '/api/nodes') == [{'uuid': 'node-1'}]


@pytest.mark.asyncio
async def test_a_response_without_payload_becomes_an_empty_dict():
    # DELETE и часть действий панели отвечают без тела.
    api = FakeApi({})
    assert await BotPanel(api).request('DELETE', '/api/hosts/host-1') == {}
