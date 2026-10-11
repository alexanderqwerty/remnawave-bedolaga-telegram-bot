"""``PanelRequester`` поверх общего клиента панели в боте.

У бота уже есть ``RemnaWaveAPI`` с повторами при 5xx, общей паузой при 429 и
нужными панели заголовками. Методов для профилей, хостов и сквадов в нём нет,
поэтому BYO ходит через его ``_make_request`` и снимает обёртку ``response``.
"""

from __future__ import annotations

from typing import Any, Protocol


class _RequestingApi(Protocol):
    async def _make_request(self, method: str, endpoint: str, data: dict | None = None) -> Any: ...


class BotPanel:
    def __init__(self, api: _RequestingApi) -> None:
        self._api = api

    async def request(self, method: str, path: str, body: dict | None = None) -> Any:
        payload = await self._api._make_request(method, path, body)
        if isinstance(payload, dict) and 'response' in payload:
            return payload['response']
        return payload or {}
