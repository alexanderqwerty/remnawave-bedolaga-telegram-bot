"""Кнопка «Свой сервер» для главного меню бота.

Upstream зовёт ``byo_menu_buttons`` одной строкой из построителя главного меню.
Пока модуль не запущен (выключен или настроен не полностью), кнопки нет.
"""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton

from app.byo import texts
from app.byo.runtime import byo_runtime


def byo_menu_buttons(runtime: Any | None = None) -> list[InlineKeyboardButton]:
    runtime = runtime or byo_runtime
    if not runtime.is_active:
        return []
    return [InlineKeyboardButton(text=texts.MENU_BUTTON, callback_data='byo:menu')]
