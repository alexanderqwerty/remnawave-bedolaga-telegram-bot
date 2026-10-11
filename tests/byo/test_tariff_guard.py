"""Чужой тариф нельзя получить подделанной кнопкой.

Upstream фильтрует тарифы по промо-группе только при показе списка. Обработчики
за кнопками (выбор тарифа, периода, подтверждение, продление, смена) верят номеру
тарифа из callback и проверяют лишь `is_active`. Участник группы A, отправив
`tariff_confirm:<тариф B>:<период>`, получал бесплатный тариф владельца B и его ноды.
Страж стоит перед всеми такими обработчиками и пропускает дальше только тариф,
доступный хотя бы одной группе пользователя.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.byo.tariff_guard import (
    TARIFF_ID_FIELD,
    TariffAccessMiddleware,
    is_tariff_allowed,
    load_user_group_ids,
    tariff_id_from_callback,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER_A_GROUP = 2
OWNER_B_GROUP = 3
OWNER_A_TARIFF = 20
OPEN_TARIFF = 21
SUBSCRIPTION_ID = 7

# Кнопки из тех же модулей, в данных которых номера тарифа нет: его либо нет вовсе,
# либо он уже лежит в состоянии диалога после проверенной кнопки.
CALLBACKS_WITHOUT_TARIFF_ID = {
    'tariff_list',
    'buy_subscription_tariffs',
    'tariff_switch',
    'instant_switch',
    'subscription_gift',
    'gift_my',
    'gift_my_page',
    'gift_my_open',
    'gift_my_back',
    'gift_my_qr',
    'gift_my_text',
    'gift_enter_code',
    'gift_activation_cancel',
    'gift_back_tariffs',
    'gift_back_periods',
    'gift_cancel',
    'gift_confirm',
    'return_to_gift_cart',
}
GUARDED_HANDLER_MODULES = (
    'app/handlers/subscription/tariff_purchase.py',
    'app/handlers/subscription/gift.py',
)
CALLBACK_REGISTRATION = re.compile(r"callback_query\.register\(\s*\w+,\s*F\.data(?:\.startswith\(|\s*==\s*)'([^']+)'")


def _callback(data: str | None) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _middleware(tariff_groups: dict[int, set[int]], user_groups: set[int]) -> TariffAccessMiddleware:
    async def tariff_groups_loader(_db, tariff_id: int) -> set[int]:
        return tariff_groups.get(tariff_id, set())

    async def user_groups_loader(_db, _db_user) -> set[int]:
        return user_groups

    return TariffAccessMiddleware(tariff_groups_loader, user_groups_loader)


async def _press(middleware: TariffAccessMiddleware, data: str | None):
    handler = AsyncMock(return_value='handled')
    callback = _callback(data)
    result = await middleware(handler, callback, {'db': object(), 'db_user': MagicMock(language='ru')})
    return handler, callback, result


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        ('tariff_select:20', 20),
        ('tariff_period:20:2000', 20),
        ('tariff_confirm:20:2000', 20),
        ('tariff_period_traffic:20:30', 20),
        ('tariff_sbp:20:30', 20),
        ('daily_tariff_confirm:20', 20),
        ('custom_confirm:20', 20),
        ('tariff_sw_confirm:20:30', 20),
        ('daily_tariff_switch_confirm:20', 20),
        ('instant_sw_confirm:20', 20),
        ('gift_tariff:20', 20),
        ('gift_period:20:30', 20),
        # У продления первым идёт id подписки, тариф — вторым.
        ('tariff_extend:7:20:30', 20),
        ('tariff_extend:7:20', 20),
        ('tariff_ext_confirm:7:20:30', 20),
    ],
)
def test_tariff_id_is_read_from_its_field(data: str, expected: int):
    assert tariff_id_from_callback(data) == expected


@pytest.mark.parametrize(
    'data',
    [
        None,
        '',
        'menu_buy',
        'tariff_list',
        # Админские кнопки страж не трогает: у них своя проверка прав.
        'admin_tariff_view:20',
        # Битые данные оставляем обработчику: он сам отвечает «сессия устарела».
        'tariff_select:abc',
        'tariff_select',
        'tariff_ext_confirm:7',
    ],
)
def test_callback_without_readable_tariff_id_gives_none(data: str | None):
    assert tariff_id_from_callback(data) is None


def test_tariff_without_group_restrictions_is_allowed_for_everyone():
    assert is_tariff_allowed(set(), set())
    assert is_tariff_allowed(set(), {OWNER_B_GROUP})


def test_member_of_an_allowed_group_is_allowed():
    assert is_tariff_allowed({OWNER_A_GROUP}, {OWNER_A_GROUP})
    assert is_tariff_allowed({OWNER_A_GROUP}, {OWNER_A_GROUP, OWNER_B_GROUP})


def test_member_of_another_group_is_not_allowed():
    assert not is_tariff_allowed({OWNER_A_GROUP}, {OWNER_B_GROUP})


def test_user_without_groups_is_not_allowed_on_restricted_tariff():
    assert not is_tariff_allowed({OWNER_A_GROUP}, set())


@pytest.mark.asyncio
async def test_member_reaches_the_handler():
    middleware = _middleware({OWNER_A_TARIFF: {OWNER_A_GROUP}}, {OWNER_A_GROUP})

    handler, callback, result = await _press(middleware, f'tariff_confirm:{OWNER_A_TARIFF}:2000')

    assert result == 'handled'
    handler.assert_awaited_once()
    callback.answer.assert_not_awaited()


@pytest.mark.parametrize(
    'data',
    [
        f'tariff_select:{OWNER_A_TARIFF}',
        f'tariff_period:{OWNER_A_TARIFF}:2000',
        f'tariff_confirm:{OWNER_A_TARIFF}:2000',
        f'tariff_sw_confirm:{OWNER_A_TARIFF}:2000',
        f'instant_sw_confirm:{OWNER_A_TARIFF}',
        f'tariff_ext_confirm:{SUBSCRIPTION_ID}:{OWNER_A_TARIFF}:2000',
    ],
)
@pytest.mark.asyncio
async def test_foreign_tariff_never_reaches_the_handler(data: str):
    middleware = _middleware({OWNER_A_TARIFF: {OWNER_A_GROUP}}, {OWNER_B_GROUP})

    handler, callback, result = await _press(middleware, data)

    assert result is None
    handler.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_extend_checks_the_tariff_not_the_subscription_id():
    # id подписки совпадает с номером чужого тарифа — это не повод отказывать.
    middleware = _middleware({SUBSCRIPTION_ID: {OWNER_A_GROUP}, OPEN_TARIFF: set()}, {OWNER_B_GROUP})

    handler, _callback_query, result = await _press(middleware, f'tariff_extend:{SUBSCRIPTION_ID}:{OPEN_TARIFF}:30')

    assert result == 'handled'
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_callback_passes_without_any_lookup():
    async def must_not_be_called(*_args):
        raise AssertionError('страж полез в базу ради кнопки без тарифа')

    middleware = TariffAccessMiddleware(must_not_be_called, must_not_be_called)

    handler, _callback_query, result = await _press(middleware, 'menu_buy')

    assert result == 'handled'
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_tariff_callback_is_blocked_when_groups_cannot_be_checked():
    # Без сессии БД (AuthMiddleware её не дал) проверить доступ нечем — не пускаем.
    middleware = _middleware({OWNER_A_TARIFF: {OWNER_A_GROUP}}, {OWNER_A_GROUP})
    handler = AsyncMock(return_value='handled')
    callback = _callback(f'tariff_confirm:{OWNER_A_TARIFF}:2000')

    result = await middleware(handler, callback, {})

    assert result is None
    handler.assert_not_awaited()
    callback.answer.assert_awaited_once()


class _FakeSession:
    """Минимальная замена AsyncSession: отдаёт заранее заданные строки."""

    def __init__(self, rows: list[int]) -> None:
        self._rows = rows

    async def execute(self, _statement):
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._rows
        return result


@pytest.mark.asyncio
async def test_user_groups_combine_main_group_and_memberships():
    db_user = MagicMock(id=1, promo_group_id=OWNER_A_GROUP)

    groups = await load_user_group_ids(_FakeSession([OWNER_B_GROUP]), db_user)

    assert groups == {OWNER_A_GROUP, OWNER_B_GROUP}


@pytest.mark.asyncio
async def test_unknown_user_has_no_groups():
    assert await load_user_group_ids(_FakeSession([OWNER_B_GROUP]), None) == set()


def test_every_callback_of_the_purchase_modules_is_classified():
    """Сторож на случай обновления upstream: новая кнопка с тарифом не пройдёт молча."""
    registered = set()
    for module in GUARDED_HANDLER_MODULES:
        source = (PROJECT_ROOT / module).read_text(encoding='utf-8')
        registered |= {name.rstrip(':') for name in CALLBACK_REGISTRATION.findall(source)}

    unclassified = registered - set(TARIFF_ID_FIELD) - CALLBACKS_WITHOUT_TARIFF_ID
    assert not unclassified, f'новые кнопки, решите, несут ли они номер тарифа: {sorted(unclassified)}'
    assert set(TARIFF_ID_FIELD) <= registered, 'страж следит за кнопками, которых в upstream больше нет'


def test_guard_runs_after_the_user_is_loaded():
    source = (PROJECT_ROOT / 'app/bot.py').read_text(encoding='utf-8')
    auth = source.index('dp.callback_query.middleware(AuthMiddleware())')
    guard = source.index('dp.callback_query.middleware(TariffAccessMiddleware())')
    assert auth < guard, 'страж должен стоять после AuthMiddleware: ему нужен db_user'
