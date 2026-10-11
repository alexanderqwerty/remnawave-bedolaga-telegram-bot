"""Поток владельца в боте: «Свой сервер».

Новичок видит условия и подключает первый сервер: имя группы → наш ключ и IP →
сервер подключается в фоне. Владелец видит свои серверы, промокод, причину сбоя
и может повторить или убрать неудачный сервер. Команды вроде /start ввод не
съедает. Обработчики вызываются напрямую: Telegram и фоновая служба подменены,
БД — SQLite в памяти, состояния — настоящий FSM aiogram в памяти.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.byo import handlers
from app.byo.handlers import NOT_A_COMMAND, ByoStates
from app.byo.models import ByoNode, ByoNodeStatus, ByoOwner, ByoStep, ByoStepStatus
from app.byo.owners import NodeLimits, NodeRequest, register_node
from app.database.models import PromoGroup, Tariff
from tests.fixtures.sqlite_memory import memory_session


TABLES = (ByoOwner.__table__, ByoNode.__table__, ByoStep.__table__, PromoGroup.__table__, Tariff.__table__)
OWNER_TG = 7001
PUBLIC_KEY = 'ssh-ed25519 AAAATESTKEY byo'


class FakeRuntime:
    def __init__(self, *, active: bool = True, running: set[int] | None = None, max_nodes: int = 3) -> None:
        self.is_active = active
        self.running = running or set()
        self.launched: list[tuple[int, int, int | None]] = []
        self._limits = NodeLimits(max_nodes=max_nodes, max_new_per_day=10)

    def limits(self) -> NodeLimits:
        return self._limits

    def public_key(self) -> str:
        return PUBLIC_KEY

    def is_running(self, node_id: int) -> bool:
        return node_id in self.running

    def launch(self, node_id: int, *, chat_id: int, message_id: int | None):
        self.launched.append((node_id, chat_id, message_id))


class FakeMessage:
    def __init__(self, text: str = '') -> None:
        self.text = text
        self.chat = SimpleNamespace(id=OWNER_TG)
        self.edits: list[tuple[str, object]] = []
        self.answers: list[tuple[str, object]] = []

    async def edit_text(self, text: str, reply_markup=None, **_kwargs) -> None:
        self.edits.append((text, reply_markup))

    async def answer(self, text: str, reply_markup=None, **_kwargs) -> SimpleNamespace:
        self.answers.append((text, reply_markup))
        return SimpleNamespace(message_id=500 + len(self.answers))

    async def delete(self) -> None:
        return None

    def last(self) -> tuple[str, object]:
        return (self.edits + self.answers)[-1]


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(data=data, message=FakeMessage(), answer=AsyncMock())


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=OWNER_TG, user_id=OWNER_TG))


def _user() -> SimpleNamespace:
    return SimpleNamespace(telegram_id=OWNER_TG, language='ru')


def _buttons(markup) -> list[tuple[str, str]]:
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


def _alert(callback) -> str:
    args, kwargs = callback.answer.call_args
    return args[0] if args else kwargs.get('text', '')


async def _owner_with_node(db, *, status: str = ByoNodeStatus.ONLINE.value) -> ByoNode:
    node = await register_node(db, NodeRequest(OWNER_TG, '203.0.114.10', 'miet'), NodeLimits(3, 3))
    node.status = status
    node.owner.promo_code = 'MIET1A2B3C4D'
    await db.commit()
    return node


# --- новичок -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_newcomer_sees_the_terms_and_a_connect_button(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        callback = _callback('byo:menu')
        await handlers.show_menu(callback, _user(), db, _state(), FakeRuntime())

        text, markup = callback.message.last()
        assert 'Свой сервер для вашей группы' in text
        assert 'не в России' in text
        assert ('➕ Подключить сервер', 'byo:start') in _buttons(markup)


@pytest.mark.asyncio
async def test_the_menu_is_closed_when_the_module_is_off(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        callback = _callback('byo:menu')
        await handlers.show_menu(callback, _user(), db, _state(), FakeRuntime(active=False))

        assert 'недоступно' in _alert(callback)
        assert callback.message.edits == []


@pytest.mark.asyncio
async def test_a_newcomer_is_asked_for_a_group_name_first(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state, callback = _state(), _callback('byo:start')
        await handlers.start_connect(callback, _user(), db, state, FakeRuntime())

        assert await state.get_state() == ByoStates.waiting_name.state
        assert 'Как назвать вашу группу' in callback.message.last()[0]


@pytest.mark.asyncio
async def test_an_unusable_group_name_is_asked_again(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = _state()
        await state.set_state(ByoStates.waiting_name)
        message = FakeMessage('МИЭТ!')

        await handlers.receive_group_name(message, _user(), db, state, FakeRuntime())

        assert await state.get_state() == ByoStates.waiting_name.state
        assert 'Не подходит' in message.last()[0]


@pytest.mark.asyncio
async def test_a_busy_group_name_is_asked_again(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(PromoGroup(name='owner:lab-a'))
        await db.commit()
        state = _state()
        await state.set_state(ByoStates.waiting_name)
        message = FakeMessage('lab-a')

        await handlers.receive_group_name(message, _user(), db, state, FakeRuntime())

        assert await state.get_state() == ByoStates.waiting_name.state
        assert 'занято' in message.last()[0]


@pytest.mark.asyncio
async def test_a_good_group_name_leads_to_our_key_and_the_ip_question(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = _state()
        await state.set_state(ByoStates.waiting_name)
        message = FakeMessage('MIET TV')

        await handlers.receive_group_name(message, _user(), db, state, FakeRuntime())

        assert await state.get_state() == ByoStates.waiting_ip.state
        assert (await state.get_data())['byo_slug'] == 'miet-tv'
        text = message.last()[0]
        assert PUBLIC_KEY in text
        assert 'IP-адрес' in text


@pytest.mark.asyncio
async def test_a_bad_ip_is_asked_again(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = _state()
        await state.set_state(ByoStates.waiting_ip)
        await state.update_data(byo_slug='miet')
        message = FakeMessage('192.168.1.10')

        await handlers.receive_ip(message, _user(), db, state, FakeRuntime())

        assert await state.get_state() == ByoStates.waiting_ip.state
        assert 'публичный IPv4' in message.last()[0]


@pytest.mark.asyncio
async def test_a_good_ip_creates_the_node_and_starts_connecting(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state, runtime = _state(), FakeRuntime()
        await state.set_state(ByoStates.waiting_ip)
        await state.update_data(byo_slug='miet')
        message = FakeMessage(' 203.0.114.10 ')

        await handlers.receive_ip(message, _user(), db, state, runtime)

        assert await state.get_state() is None
        ((node_id, chat_id, message_id),) = runtime.launched
        node = await db.get(ByoNode, node_id)
        assert node.ip == '203.0.114.10'
        assert chat_id == OWNER_TG
        assert message_id == 501, 'фоновая служба правит то самое сообщение с прогрессом'
        assert 'Подключаю сервер 203.0.114.10' in message.answers[0][0]


@pytest.mark.asyncio
async def test_a_refused_node_explains_why_and_ends_the_dialog(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _owner_with_node(db)  # 203.0.114.10 уже подключён
        state, runtime = _state(), FakeRuntime()
        await state.set_state(ByoStates.waiting_ip)
        message = FakeMessage('203.0.114.10')

        await handlers.receive_ip(message, _user(), db, state, runtime)

        assert runtime.launched == []
        assert await state.get_state() is None
        assert 'уже подключён' in message.last()[0]


# --- владелец ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_owner_adding_a_server_skips_the_name(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _owner_with_node(db)
        state, callback = _state(), _callback('byo:start')

        await handlers.start_connect(callback, _user(), db, state, FakeRuntime())

        assert await state.get_state() == ByoStates.waiting_ip.state
        assert PUBLIC_KEY in callback.message.last()[0]


@pytest.mark.asyncio
async def test_an_owner_at_the_limit_is_stopped_before_any_question(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _owner_with_node(db)
        state, callback = _state(), _callback('byo:start')

        await handlers.start_connect(callback, _user(), db, state, FakeRuntime(max_nodes=1))

        assert await state.get_state() is None
        assert 'не больше 1' in _alert(callback)


@pytest.mark.asyncio
async def test_the_owner_menu_lists_servers_the_code_and_actions(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _owner_with_node(db, status=ByoNodeStatus.FAILED.value)
        db.add(ByoStep(node_id=node.id, name='check', status=ByoStepStatus.FAILED.value, detail='Сервер в России <RU>'))
        await db.commit()
        callback = _callback('byo:menu')

        await handlers.show_menu(callback, _user(), db, _state(), FakeRuntime())

        text, markup = callback.message.last()
        assert '203.0.114.10' in text
        assert 'не подключён' in text
        assert '«Проверка сервера»' in text
        assert '&lt;RU&gt;' in text
        assert 'MIET1A2B3C4D' in text
        buttons = _buttons(markup)
        assert ('🔁 Повторить 1', f'byo:retry:{node.id}') in buttons
        assert ('🗑 Убрать 1', f'byo:forget:{node.id}') in buttons
        assert ('➕ Добавить ещё сервер', 'byo:start') in buttons


@pytest.mark.asyncio
async def test_a_running_connection_shows_as_connecting(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _owner_with_node(db, status=ByoNodeStatus.FAILED.value)
        callback = _callback('byo:menu')

        await handlers.show_menu(callback, _user(), db, _state(), FakeRuntime(running={node.id}))

        text, markup = callback.message.last()
        assert 'подключается' in text
        assert all('byo:retry' not in data for _, data in _buttons(markup))


# --- повтор и «убрать» --------------------------------------------------------------------


async def _failed_long_ago(db) -> ByoNode:
    node = await _owner_with_node(db, status=ByoNodeStatus.FAILED.value)
    node.updated_at = datetime.now(UTC) - timedelta(minutes=5)
    await db.commit()
    return node


@pytest.mark.asyncio
async def test_retry_starts_the_connection_again(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _failed_long_ago(db)
        runtime, callback = FakeRuntime(), _callback(f'byo:retry:{node.id}')

        await handlers.retry(callback, _user(), db, runtime)

        assert runtime.launched == [(node.id, OWNER_TG, 501)]


@pytest.mark.asyncio
async def test_retry_right_after_a_failure_asks_to_wait(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _owner_with_node(db, status=ByoNodeStatus.FAILED.value)
        node.updated_at = datetime.now(UTC)
        await db.commit()
        runtime, callback = FakeRuntime(), _callback(f'byo:retry:{node.id}')

        await handlers.retry(callback, _user(), db, runtime)

        assert runtime.launched == []
        assert 'Подождите' in _alert(callback)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'running', 'telegram_id', 'expected'),
    [
        (ByoNodeStatus.ONLINE.value, False, OWNER_TG, 'не в ошибке'),
        (ByoNodeStatus.FAILED.value, True, OWNER_TG, 'уже идёт'),
        (ByoNodeStatus.FAILED.value, False, 9999, 'не найден'),
    ],
)
async def test_retry_is_refused_when_it_makes_no_sense(monkeypatch, status, running, telegram_id, expected):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _failed_long_ago(db)
        node.status = status
        await db.commit()
        runtime = FakeRuntime(running={node.id} if running else set())
        callback = _callback(f'byo:retry:{node.id}')

        await handlers.retry(callback, SimpleNamespace(telegram_id=telegram_id, language='ru'), db, runtime)

        assert runtime.launched == []
        assert expected in _alert(callback)


@pytest.mark.asyncio
async def test_forget_removes_a_server_that_failed_early(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _failed_long_ago(db)
        callback = _callback(f'byo:forget:{node.id}')

        await handlers.forget(callback, _user(), db, _state(), FakeRuntime())

        assert node.status == ByoNodeStatus.REMOVED.value
        assert 'Свой сервер для вашей группы' in callback.message.last()[0], 'серверов не осталось'


@pytest.mark.asyncio
async def test_forget_refuses_a_server_with_outside_objects(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        node = await _failed_long_ago(db)
        db.add(ByoStep(node_id=node.id, name='dns', status=ByoStepStatus.DONE.value))
        await db.commit()
        callback = _callback(f'byo:forget:{node.id}')

        await handlers.forget(callback, _user(), db, _state(), FakeRuntime())

        assert node.status == ByoNodeStatus.FAILED.value
        assert 'администратор' in _alert(callback)


@pytest.mark.asyncio
async def test_cancel_ends_the_dialog(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = _state()
        await state.set_state(ByoStates.waiting_ip)
        callback = _callback('byo:cancel')

        await handlers.cancel(callback, _user(), db, state, FakeRuntime())

        assert await state.get_state() is None


@pytest.mark.parametrize(('text', 'accepted'), [('203.0.114.10', True), ('miet', True), ('/start', False)])
def test_commands_are_never_taken_as_answers(text: str, accepted: bool):
    assert bool(NOT_A_COMMAND.resolve(SimpleNamespace(text=text))) is accepted
