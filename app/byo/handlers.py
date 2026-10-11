"""Поток владельца в боте: «Свой сервер».

Кнопка в главном меню (``byo:menu``) открывает экран: новичку — условия и кнопку
«Подключить сервер», владельцу — его серверы, промокод и действия. Подключение:
имя группы (только для нового владельца) → наш ключ и IP → нода создаётся и
подключается в фоне (``ByoRuntime``). Ответы ждутся в состояниях FSM; команды
вроде /start их не съедают и выводят из диалога как обычно.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.byo import texts
from app.byo.journal import DatabaseStepJournal
from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner
from app.byo.owners import (
    NodeRequest,
    OwnerError,
    can_forget,
    check_limits,
    failure_reasons,
    find_owner,
    forget_node,
    is_slug_free,
    load_node,
    parse_group_name,
    parse_public_ipv4,
    register_node,
)
from app.byo.progress import render_progress
from app.byo.runtime import byo_runtime
from app.utils.photo_message import safe_edit_or_resend


# Повтор не чаще раза в минуту: каждый — это SSH к чужому адресу с нашего выхода.
RETRY_COOLDOWN = timedelta(minutes=1)
SLUG_KEY = 'byo_slug'
MAIN_MENU = 'back_to_menu'
NOT_A_COMMAND = F.text & ~F.text.startswith('/')


class ByoStates(StatesGroup):
    waiting_name = State()
    waiting_ip = State()


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


CANCEL_KEYBOARD = _keyboard([[(texts.BUTTON_CANCEL, 'byo:cancel')]])
MY_SERVERS_KEYBOARD = _keyboard([[(texts.BUTTON_MY_SERVERS, 'byo:menu')]])


def _live_nodes(owner: ByoOwner | None) -> list[ByoNode]:
    nodes = owner.nodes if owner is not None else []
    return sorted((node for node in nodes if node.status != ByoNodeStatus.REMOVED.value), key=lambda n: n.ordinal)


def _node_lines(node: ByoNode, runtime: Any, failure: tuple[str, str] | None) -> list[str]:
    status = ByoNodeStatus.PROVISIONING.value if runtime.is_running(node.id) else node.status
    mark, state = texts.NODE_STATES[status]
    lines = [texts.NODE_LINE.format(mark=mark, ordinal=node.ordinal, ip=html.escape(node.ip), state=state)]
    if failure is not None and status == ByoNodeStatus.FAILED.value:
        step, reason = failure
        lines.append(texts.FAILURE_LINE.format(step=texts.STEP_LABELS.get(step, step), reason=html.escape(reason)))
    return lines


async def _node_buttons(db: AsyncSession, nodes: list[ByoNode], runtime: Any) -> list[list[tuple[str, str]]]:
    rows = []
    for node in nodes:
        if node.status != ByoNodeStatus.FAILED.value or runtime.is_running(node.id):
            continue
        row = [(texts.BUTTON_RETRY_NODE.format(ordinal=node.ordinal), f'byo:retry:{node.id}')]
        if await can_forget(db, node):
            row.append((texts.BUTTON_FORGET.format(ordinal=node.ordinal), f'byo:forget:{node.id}'))
        rows.append(row)
    return rows


async def _menu_screen(db: AsyncSession, telegram_id: int, runtime: Any) -> tuple[str, InlineKeyboardMarkup]:
    owner = await find_owner(db, telegram_id)
    nodes = _live_nodes(owner)
    if not nodes:
        return texts.INTRO, _keyboard([[(texts.BUTTON_CONNECT, 'byo:start')], [(texts.BUTTON_BACK, MAIN_MENU)]])
    failures = await failure_reasons(db, [node.id for node in nodes])
    lines = [texts.OWNER_HEADER.format(slug=html.escape(owner.slug)), '']
    for node in nodes:
        lines.extend(_node_lines(node, runtime, failures.get(node.id)))
    if owner.promo_code:
        lines.append(texts.PROMO_LINE.format(code=html.escape(owner.promo_code)))
    rows = await _node_buttons(db, nodes, runtime)
    if len(nodes) < runtime.limits().max_nodes:
        rows.append([(texts.BUTTON_ADD, 'byo:start')])
    rows.append([(texts.BUTTON_BACK, MAIN_MENU)])
    return '\n'.join(lines), _keyboard(rows)


async def _render_menu(callback: types.CallbackQuery, db: AsyncSession, telegram_id: int, runtime: Any) -> None:
    text, keyboard = await _menu_screen(db, telegram_id, runtime)
    await safe_edit_or_resend(callback.message, text, reply_markup=keyboard)


async def show_menu(
    callback: types.CallbackQuery, db_user: Any, db: AsyncSession, state: FSMContext, byo_runtime: Any
) -> None:
    if not byo_runtime.is_active:
        await callback.answer(texts.NOT_AVAILABLE, show_alert=True)
        return
    await state.clear()
    await _render_menu(callback, db, db_user.telegram_id, byo_runtime)
    await callback.answer()


def _ask_ip_text(runtime: Any) -> str:
    return texts.ASK_IP.format(key=html.escape(runtime.public_key()))


async def start_connect(
    callback: types.CallbackQuery, db_user: Any, db: AsyncSession, state: FSMContext, byo_runtime: Any
) -> None:
    if not byo_runtime.is_active:
        await callback.answer(texts.NOT_AVAILABLE, show_alert=True)
        return
    owner = await find_owner(db, db_user.telegram_id)
    if owner is None:
        await state.set_state(ByoStates.waiting_name)
        await safe_edit_or_resend(callback.message, texts.ASK_GROUP_NAME, reply_markup=CANCEL_KEYBOARD)
        await callback.answer()
        return
    try:
        await check_limits(db, owner, byo_runtime.limits())
    except OwnerError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await state.set_state(ByoStates.waiting_ip)
    await safe_edit_or_resend(callback.message, _ask_ip_text(byo_runtime), reply_markup=CANCEL_KEYBOARD)
    await callback.answer()


async def receive_group_name(
    message: types.Message, db_user: Any, db: AsyncSession, state: FSMContext, byo_runtime: Any
) -> None:
    slug = parse_group_name(message.text or '')
    if slug is None:
        await message.answer(texts.BAD_GROUP_NAME, reply_markup=CANCEL_KEYBOARD)
        return
    if not await is_slug_free(db, slug):
        await message.answer(texts.BUSY_GROUP_NAME, reply_markup=CANCEL_KEYBOARD)
        return
    await state.update_data(**{SLUG_KEY: slug})
    await state.set_state(ByoStates.waiting_ip)
    await message.answer(_ask_ip_text(byo_runtime), reply_markup=CANCEL_KEYBOARD)


async def receive_ip(
    message: types.Message, db_user: Any, db: AsyncSession, state: FSMContext, byo_runtime: Any
) -> None:
    ip = parse_public_ipv4(message.text or '')
    if ip is None:
        await message.answer(texts.BAD_IP, reply_markup=CANCEL_KEYBOARD)
        return
    slug = (await state.get_data()).get(SLUG_KEY)
    try:
        node = await register_node(db, NodeRequest(db_user.telegram_id, ip, slug), byo_runtime.limits())
    except OwnerError as error:
        await state.clear()
        await message.answer(html.escape(str(error)), reply_markup=MY_SERVERS_KEYBOARD)
        return
    await state.clear()
    sent = await message.answer(render_progress(ip, current=None, done=()))
    byo_runtime.launch(node.id, chat_id=message.chat.id, message_id=sent.message_id)


async def _own_node(callback: types.CallbackQuery, db: AsyncSession, telegram_id: int) -> ByoNode | None:
    _, _, raw_id = callback.data.rpartition(':')
    node = await load_node(db, int(raw_id)) if raw_id.isdecimal() else None
    if node is None or node.owner.telegram_id != telegram_id:
        await callback.answer(texts.NOT_FOUND, show_alert=True)
        return None
    return node


def _retry_refusal(node: ByoNode, runtime: Any) -> str | None:
    if runtime.is_running(node.id):
        return texts.ALREADY_RUNNING
    if node.status != ByoNodeStatus.FAILED.value:
        return texts.NOT_FAILED
    if node.updated_at is not None and datetime.now(UTC) - node.updated_at < RETRY_COOLDOWN:
        return texts.TOO_SOON
    return None


async def retry(callback: types.CallbackQuery, db_user: Any, db: AsyncSession, byo_runtime: Any) -> None:
    node = await _own_node(callback, db, db_user.telegram_id)
    if node is None:
        return
    refusal = _retry_refusal(node, byo_runtime)
    if refusal is not None:
        await callback.answer(refusal, show_alert=True)
        return
    done = await DatabaseStepJournal(db, node.id).done_steps()
    sent = await callback.message.answer(render_progress(node.ip, current=None, done=done))
    byo_runtime.launch(node.id, chat_id=callback.message.chat.id, message_id=sent.message_id)
    await callback.answer()


async def forget(
    callback: types.CallbackQuery, db_user: Any, db: AsyncSession, state: FSMContext, byo_runtime: Any
) -> None:
    node = await _own_node(callback, db, db_user.telegram_id)
    if node is None:
        return
    if byo_runtime.is_running(node.id):
        await callback.answer(texts.ALREADY_RUNNING, show_alert=True)
        return
    try:
        await forget_node(db, node)
    except OwnerError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await _render_menu(callback, db, db_user.telegram_id, byo_runtime)
    await callback.answer()


async def cancel(
    callback: types.CallbackQuery, db_user: Any, db: AsyncSession, state: FSMContext, byo_runtime: Any
) -> None:
    await state.clear()
    await _render_menu(callback, db, db_user.telegram_id, byo_runtime)
    await callback.answer()


def register_handlers(dp: Dispatcher, runtime: Any = byo_runtime) -> None:
    dp['byo_runtime'] = runtime
    dp.callback_query.register(show_menu, F.data == 'byo:menu')
    dp.callback_query.register(start_connect, F.data == 'byo:start')
    dp.callback_query.register(cancel, F.data == 'byo:cancel')
    dp.callback_query.register(retry, F.data.startswith('byo:retry:'))
    dp.callback_query.register(forget, F.data.startswith('byo:forget:'))
    dp.message.register(receive_group_name, StateFilter(ByoStates.waiting_name), NOT_A_COMMAND)
    dp.message.register(receive_ip, StateFilter(ByoStates.waiting_ip), NOT_A_COMMAND)
