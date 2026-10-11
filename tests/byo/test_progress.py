"""Сообщение владельцу о ходе подключения и итог.

Бот правит одно сообщение: пройденные шаги с галочкой, текущий с часами, будущие
пустые. Промежуточные правки — по возможности (Telegram ограничивает частоту, а
первые шаги идут за доли секунды), итог доставляется обязательно. Если владелец
удалил сообщение, бот присылает новое. Telegram подменён поддельным ботом.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.byo.connect import STEP_NAMES
from app.byo.models import ByoOwner
from app.byo.progress import ProgressMessage, render_progress
from tests.byo.fakes import FakeBot


IP = '203.0.114.10'


def _buttons(markup) -> list[tuple[str, str]]:
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def test_done_current_and_future_steps_are_marked():
    text = render_progress(IP, current='install', done={'ssh', 'check', 'secrets', 'dns', 'profile'})
    lines = text.splitlines()

    assert IP in lines[0]
    marks = [line.split(' ', 1)[0] for line in lines[2:]]
    assert len(marks) == len(STEP_NAMES)
    assert marks[:5] == ['✅'] * 5
    assert marks[5] == '⏳'
    assert set(marks[6:]) == {'▫️'}


@pytest.mark.asyncio
async def test_steps_edit_the_message_the_owner_already_sees():
    bot = FakeBot()
    progress = ProgressMessage(bot=bot, chat_id=7001, ip=IP, message_id=55)

    await progress.step('ssh')
    await progress.step('check')

    assert bot.sent == []
    assert [edit['message_id'] for edit in bot.edited] == [55, 55]
    assert '✅ Доступ по SSH' in bot.edited[-1]['text']
    assert '⏳ Проверка сервера' in bot.edited[-1]['text']


@pytest.mark.asyncio
async def test_a_resumed_connection_starts_with_a_new_message_and_known_steps():
    bot = FakeBot()
    progress = ProgressMessage(bot=bot, chat_id=7001, ip=IP, done={'ssh', 'check'})

    await progress.step('secrets')

    (sent,) = bot.sent
    assert '✅ Проверка сервера' in sent['text']
    assert '⏳ Имя и адрес ноды' in sent['text']


@pytest.mark.asyncio
async def test_a_deleted_message_is_replaced_by_a_new_one():
    bot = FakeBot(edit_errors=[TelegramBadRequest(method=None, message='message to edit not found')])
    progress = ProgressMessage(bot=bot, chat_id=7001, ip=IP, message_id=55)

    await progress.step('ssh')
    await progress.step('check')

    assert len(bot.sent) == 1
    assert bot.edited[-1]['message_id'] == bot.sent[0]['message_id']


@pytest.mark.asyncio
async def test_flood_control_skips_a_progress_update_silently():
    bot = FakeBot(edit_errors=[TelegramRetryAfter(method=None, message='flood', retry_after=3)])
    progress = ProgressMessage(bot=bot, chat_id=7001, ip=IP, message_id=55)

    await progress.step('secrets')  # пропущено
    await progress.step('dns')

    assert len(bot.edited) == 1
    assert '✅ Имя и адрес ноды' in bot.edited[0]['text']


@pytest.mark.asyncio
async def test_failure_names_the_step_escapes_the_reason_and_offers_a_retry():
    bot = FakeBot()
    progress = ProgressMessage(bot=bot, chat_id=7001, ip=IP, message_id=55)

    await progress.failed(node_id=42, step='check', reason='Нужен Debian, а на сервере <alpine>.')

    text = bot.edited[-1]['text']
    assert '«Проверка сервера»' in text
    assert '&lt;alpine&gt;' in text
    assert ('🔁 Повторить', 'byo:retry:42') in _buttons(bot.edited[-1]['markup'])


@pytest.mark.asyncio
async def test_the_final_message_is_delivered_even_through_flood_control():
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    bot = FakeBot(edit_errors=[TelegramRetryAfter(method=None, message='flood', retry_after=3)])
    progress = ProgressMessage(bot=bot, chat_id=7001, ip=IP, message_id=55, sleep=no_sleep)
    owner = ByoOwner(slug='miet', promo_code='MIET1A2B3C4D')

    await progress.finished(owner, bot_username='miettvvpnbot')

    assert sleeps == [3]
    text = bot.edited[-1]['text']
    assert '<code>MIET1A2B3C4D</code>' in text
    assert '@miettvvpnbot' in text
    assert 'VPN miet' in text
    assert ('🖥 Мои серверы', 'byo:menu') in _buttons(bot.edited[-1]['markup'])
