"""Таблицы BYO: владелец, его ноды и журнал шагов подключения.

Журнал нужен, чтобы подключение было идемпотентным: после сбоя (упал SSH,
хостер тормозит, перезапустился бот) повтор продолжает с незавершённого шага,
а уже сделанное не делается заново. Паролей и SECRET_KEY в этих таблицах нет —
только то, что не стыдно увидеть в дампе.
"""

from __future__ import annotations

import pytest

from app.byo.models import (
    ByoNode,
    ByoNodeStatus,
    ByoOwner,
    ByoStep,
    ByoStepStatus,
)
from app.database.models import Base


BYO_TABLES = {'byo_owners', 'byo_nodes', 'byo_steps'}


def test_byo_tables_register_on_the_shared_base():
    # Upstream создаёт свежую БД одним Base.metadata.create_all — наши таблицы
    # обязаны быть на том же Base, иначе их просто не будет.
    assert BYO_TABLES.issubset(Base.metadata.tables)


def test_no_secret_columns_leak_into_the_schema():
    forbidden = ('password', 'secret', 'secret_key', 'private_key', 'root_password')
    for table_name in BYO_TABLES:
        table = Base.metadata.tables[table_name]
        for column in table.columns:
            assert not any(word in column.name.lower() for word in forbidden), (
                f'{table_name}.{column.name} похоже на секрет — такому не место в БД'
            )


def test_owner_is_one_per_telegram_account():
    telegram_id = ByoOwner.__table__.c.telegram_id
    assert telegram_id.unique, 'один владелец на Telegram-аккаунт'
    assert not telegram_id.nullable


def test_node_belongs_to_an_owner_and_is_removed_with_it():
    owner_fk = next(iter(ByoNode.__table__.c.owner_id.foreign_keys))
    assert owner_fk.column.table.name == 'byo_owners'
    assert owner_fk.ondelete == 'CASCADE'


def test_squad_and_tariff_belong_to_the_owner_not_the_node():
    # Один сквад на все ноды владельца: вторая нода добавляет свой инбаунд
    # в тот же сквад, тариф и промокод тоже общие.
    assert {'squad_uuid', 'tariff_id'}.issubset(ByoOwner.__table__.c.keys())
    assert 'squad_uuid' not in ByoNode.__table__.c


def test_owner_survives_removal_of_its_tariff_in_the_bot():
    tariff_fk = next(iter(ByoOwner.__table__.c.tariff_id.foreign_keys))
    assert tariff_fk.column.table.name == 'tariffs'
    assert tariff_fk.ondelete == 'SET NULL'


def test_node_remembers_everything_its_removal_needs():
    # Удаление идёт обратным порядком: хост → инбаунд из сквада → нода →
    # профиль → DNS. Каждому шагу нужен свой идентификатор.
    needed = {'host_uuid', 'inbound_uuid', 'node_uuid', 'config_profile_uuid', 'fqdn', 'ip'}
    assert needed.issubset(ByoNode.__table__.c.keys())


def test_node_keeps_its_inbound_port():
    # Порт выводится из slug и номера, но формула может поменяться —
    # у живой ноды он должен остаться прежним.
    assert 'inbound_port' in ByoNode.__table__.c


def test_step_belongs_to_a_node_and_is_removed_with_it():
    node_fk = next(iter(ByoStep.__table__.c.node_id.foreign_keys))
    assert node_fk.column.table.name == 'byo_nodes'
    assert node_fk.ondelete == 'CASCADE'


def test_a_node_cannot_list_the_same_step_twice():
    unique_constraints = [
        constraint
        for constraint in ByoStep.__table__.constraints
        if constraint.__class__.__name__ == 'UniqueConstraint'
    ]
    pairs = [{column.name for column in constraint.columns} for constraint in unique_constraints]
    assert {'node_id', 'name'} in pairs, 'шаг (node_id, name) должен быть уникальным'


def test_statuses_are_plain_strings_not_native_enums():
    # Нативный ENUM в Postgres требует своей миграции при каждом новом значении;
    # строкам со значениями из Python-enum это не нужно.
    for column in (ByoNode.__table__.c.status, ByoStep.__table__.c.status):
        assert isinstance(column.type.python_type, type)
        assert column.type.python_type is str


@pytest.mark.parametrize(
    ('enum', 'member', 'value'),
    [
        (ByoNodeStatus, 'PROVISIONING', 'provisioning'),
        (ByoNodeStatus, 'ONLINE', 'online'),
        (ByoNodeStatus, 'OFFLINE', 'offline'),
        (ByoNodeStatus, 'FAILED', 'failed'),
        (ByoStepStatus, 'PENDING', 'pending'),
        (ByoStepStatus, 'DONE', 'done'),
        (ByoStepStatus, 'FAILED', 'failed'),
    ],
)
def test_status_values(enum, member, value):
    assert enum[member].value == value


def test_byo_uses_its_own_table_names():
    for model in (ByoOwner, ByoNode, ByoStep):
        assert model.__tablename__.startswith('byo_')
