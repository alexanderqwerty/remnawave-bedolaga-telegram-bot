"""Точки касания upstream: модуль подключён к боту в трёх местах.

Обработчики регистрируются в ``app/bot.py`` раньше общего «ловца» upstream, кнопка
появляется в главном меню, ``main.py`` запускает и останавливает фоновую службу.
При слиянии с новой версией Bedolaga конфликт в этих файлах легко решить, потеряв
нашу строку, — и модуль молча пропадёт из бота. Эти тесты ловят такую потерю.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.byo import menu


ROOT = Path(__file__).resolve().parents[2]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_handlers_are_registered_before_the_catch_all():
    source = (ROOT / 'app' / 'bot.py').read_text(encoding='utf-8')
    assert 'register_byo_handlers(dp)' in source
    assert source.index('register_byo_handlers(dp)') < source.index('common.register_handlers(dp)')


def test_main_starts_and_stops_the_background_service():
    source = (ROOT / 'main.py').read_text(encoding='utf-8')
    assert 'await byo_runtime.start(bot)' in source
    assert 'await byo_runtime.stop()' in source


@pytest.mark.parametrize(('active', 'shown'), [(True, True), (False, False)])
def test_main_menu_shows_the_button_only_while_the_module_runs(monkeypatch, active: bool, shown: bool):
    from app.keyboards.inline import get_main_menu_keyboard

    monkeypatch.setattr(menu, 'byo_runtime', SimpleNamespace(is_active=active))
    keyboard = get_main_menu_keyboard(language='ru')

    assert ('byo:menu' in _callbacks(keyboard)) is shown
