"""Таблицы BYO.

Лежат на общем ``Base``: на свежей БД upstream создаёт их вместе со всей схемой
(``Base.metadata.create_all``), а на существующей, где upstream только гонит
миграции, их нужно создать вызовом ``app.byo.schema.ensure_tables`` при старте.
Имена с префиксом ``byo_`` — чтобы не пересекаться с upstream.

Секретов здесь нет: SSH-ключ владельца, root-пароль и SECRET_KEY ноды живут
в памяти на время подключения и в файлах на самом VPS, но не в этой БД.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import relationship

from app.database.models import AwareDateTime, Base


class ByoNodeStatus(StrEnum):
    PROVISIONING = 'provisioning'  # идёт подключение
    ONLINE = 'online'  # нода на связи с панелью
    OFFLINE = 'offline'  # панель потеряла связь (нода жива сама по себе)
    FAILED = 'failed'  # подключение не удалось
    REMOVED = 'removed'  # забыта до первых внешних объектов; строка остаётся для суточного лимита


class ByoStepStatus(StrEnum):
    PENDING = 'pending'
    DONE = 'done'
    FAILED = 'failed'


class ByoOwner(Base):
    """Владелец группы, подключивший хотя бы один свой сервер."""

    __tablename__ = 'byo_owners'

    id = Column(Integer, primary_key=True)
    # Один владелец на Telegram-аккаунт (защита от злоупотреблений из плана).
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    # Короткий устойчивый идентификатор для тегов, сквадов и поддоменов: o-<slug>.
    slug = Column(String(32), unique=True, nullable=False)
    # Сквад в панели — один на все ноды владельца: вторая нода добавляет в него свой инбаунд.
    squad_uuid = Column(String(36), nullable=True)
    # Промо-группа, тариф и промокод владельца в самом боте (owner:<slug>).
    promo_group_id = Column(Integer, ForeignKey('promo_groups.id', ondelete='SET NULL'), nullable=True)
    tariff_id = Column(Integer, ForeignKey('tariffs.id', ondelete='SET NULL'), nullable=True)
    promo_code = Column(String(64), nullable=True)
    is_blocked = Column(Boolean, nullable=False, default=False, server_default='false')

    created_at = Column(AwareDateTime(), nullable=False, server_default=func.now())
    updated_at = Column(AwareDateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    nodes = relationship('ByoNode', back_populates='owner', cascade='all, delete-orphan')

    def __repr__(self) -> str:
        return f'<ByoOwner(id={self.id}, slug={self.slug!r}, telegram_id={self.telegram_id})>'


class ByoNode(Base):
    """Один сервер владельца, который бот превращает в ноду Remnawave."""

    __tablename__ = 'byo_nodes'

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey('byo_owners.id', ondelete='CASCADE'), nullable=False, index=True)
    # Порядковый номер ноды у владельца: o-<slug>-<n>-tws.
    ordinal = Column(Integer, nullable=False)
    ip = Column(String(45), nullable=False)
    fqdn = Column(String(255), nullable=True)
    # Тег, порт инбаунда и путь WebSocket — публичные, не секреты.
    inbound_tag = Column(String(64), nullable=True)
    inbound_port = Column(Integer, nullable=True)
    ws_path = Column(String(128), nullable=True)
    # UUID объектов в панели Remnawave: по ним идёт удаление ноды.
    config_profile_uuid = Column(String(36), nullable=True)
    inbound_uuid = Column(String(36), nullable=True)
    node_uuid = Column(String(36), nullable=True)
    host_uuid = Column(String(36), nullable=True)
    status = Column(String(20), nullable=False, default=ByoNodeStatus.PROVISIONING.value)

    created_at = Column(AwareDateTime(), nullable=False, server_default=func.now())
    updated_at = Column(AwareDateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    owner = relationship('ByoOwner', back_populates='nodes')
    steps = relationship('ByoStep', back_populates='node', cascade='all, delete-orphan')

    __table_args__ = (
        # Номер уникален среди живых нод владельца: забытая (removed) номер не держит.
        Index(
            'uq_byo_nodes_owner_ordinal_live',
            'owner_id',
            'ordinal',
            unique=True,
            postgresql_where=text("status <> 'removed'"),
            sqlite_where=text("status <> 'removed'"),
        ),
        Index('ix_byo_nodes_owner_status', 'owner_id', 'status'),
    )

    def __repr__(self) -> str:
        return f'<ByoNode(id={self.id}, owner_id={self.owner_id}, ip={self.ip!r}, status={self.status})>'


class ByoStep(Base):
    """Один шаг подключения ноды с результатом — журнал для повтора с места сбоя."""

    __tablename__ = 'byo_steps'

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey('byo_nodes.id', ondelete='CASCADE'), nullable=False, index=True)
    # Имя шага из ByoProvisioning (ssh, verify, dns, profile, …).
    name = Column(String(40), nullable=False)
    status = Column(String(20), nullable=False, default=ByoStepStatus.PENDING.value)
    # Короткое человекочитаемое объяснение неудачи для владельца и админа.
    detail = Column(String(500), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default='0')

    created_at = Column(AwareDateTime(), nullable=False, server_default=func.now())
    updated_at = Column(AwareDateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    node = relationship('ByoNode', back_populates='steps')

    __table_args__ = (UniqueConstraint('node_id', 'name', name='uq_byo_steps_node_name'),)

    def __repr__(self) -> str:
        return f'<ByoStep(node_id={self.node_id}, name={self.name!r}, status={self.status})>'
