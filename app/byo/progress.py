"""Сообщение владельцу о ходе подключения и итог.

Бот правит одно сообщение: пройденные шаги с галочкой, текущий с часами, будущие
пустые. Промежуточные правки — по возможности: Telegram ограничивает частоту, а
первые шаги идут за доли секунды, так что пропущенную правку перекроет следующая.
Итог (успех или ошибка) доставляется обязательно. Если владелец удалил сообщение,
бот присылает новое.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.byo import texts
from app.byo.bot_objects import tariff_name
from app.byo.connect import STEP_NAMES
from app.byo.models import ByoOwner


logger = structlog.get_logger(__name__)

NOT_MODIFIED = 'message is not modified'


def render_progress(ip: str, *, current: str | None, done: Collection[str]) -> str:
    lines = [texts.PROGRESS_HEADER.format(ip=html.escape(ip)), '']
    for name in STEP_NAMES:
        if name in done:
            mark = '✅'
        elif name == current:
            mark = '⏳'
        else:
            mark = '▫️'
        lines.append(f'{mark} {texts.STEP_LABELS[name]}')
    return '\n'.join(lines)


def _keyboard(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
    )


def render_done(ip: str, owner: ByoOwner, bot_username: str) -> str:
    code = html.escape(owner.promo_code or '')
    tariff = html.escape(tariff_name(owner.slug))
    invitation = texts.INVITATION.format(bot=html.escape(bot_username), code=code, tariff=tariff)
    return texts.DONE.format(ip=html.escape(ip), code=code, invitation=invitation, tariff=tariff)


@dataclass
class ProgressMessage:
    bot: Any
    chat_id: int
    ip: str
    message_id: int | None = None
    done: set[str] = field(default_factory=set)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _current: str | None = None

    @property
    def current(self) -> str | None:
        return self._current

    async def step(self, name: str) -> None:
        if self._current is not None:
            self.done.add(self._current)
        self._current = name
        try:
            await self._show(render_progress(self.ip, current=name, done=self.done), None)
        except TelegramRetryAfter:
            logger.debug('BYO: правка прогресса пропущена из-за ограничения частоты', step=name)

    async def failed(self, *, node_id: int, step: str, reason: str) -> None:
        text = texts.FAILED.format(
            ip=html.escape(self.ip), step=texts.STEP_LABELS.get(step, step), reason=html.escape(reason)
        )
        keyboard = _keyboard((texts.BUTTON_RETRY, f'byo:retry:{node_id}'), (texts.BUTTON_MY_SERVERS, 'byo:menu'))
        await self._deliver(text, keyboard)

    async def finished(self, owner: ByoOwner, *, bot_username: str) -> None:
        keyboard = _keyboard((texts.BUTTON_MY_SERVERS, 'byo:menu'))
        await self._deliver(render_done(self.ip, owner, bot_username), keyboard)

    async def _deliver(self, text: str, keyboard: InlineKeyboardMarkup) -> None:
        try:
            await self._show(text, keyboard)
        except TelegramRetryAfter as error:
            await self.sleep(error.retry_after)
            await self._show(text, keyboard)

    async def _show(self, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
        if self.message_id is not None:
            try:
                await self.bot.edit_message_text(
                    text=text, chat_id=self.chat_id, message_id=self.message_id, reply_markup=keyboard
                )
                return
            except TelegramBadRequest as error:
                if NOT_MODIFIED in str(error):
                    return
                logger.info('BYO: сообщение о прогрессе пропало, шлём новое', error=str(error))
        sent = await self.bot.send_message(self.chat_id, text, reply_markup=keyboard)
        self.message_id = sent.message_id
