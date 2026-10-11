"""Фоновое подключение серверов владельцев внутри бота.

Обработчик только создаёт ноду и просит ``ByoRuntime.launch`` подключить её:
подключение идёт отдельной задачей со своей сессией БД, владелец видит прогресс
и итог, админам приходит уведомление. ``start`` при старте бота создаёт таблицы
BYO, загружает (или создаёт) наш SSH-ключ и продолжает подключения, прерванные
перезапуском. Одна нода не подключается дважды одновременно. Если модуль
выключен или настроен не полностью, ``start`` ничего не делает.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.byo import ssh, texts
from app.byo.bot_objects import BotOwnerObjects
from app.byo.connect import ConnectDeps, connect_node
from app.byo.journal import DatabaseStepJournal
from app.byo.keys import load_or_create_key, public_key_line
from app.byo.models import ByoNode, ByoNodeStatus
from app.byo.owners import NodeLimits, load_node, pending_node_ids
from app.byo.panel import PanelProvisioner
from app.byo.panel_client import BotPanel
from app.byo.progress import ProgressMessage
from app.byo.provisioning import ProvisioningError
from app.byo.regru import RegruClient
from app.byo.schema import ensure_tables
from app.byo.settings import ByoSettings, byo_settings
from app.config import settings as app_settings
from app.database.database import AsyncSessionLocal, engine as default_engine
from app.services.remnawave_service import RemnaWaveService


logger = structlog.get_logger(__name__)

REGRU_TIMEOUT_SECONDS = 30

DepsFactory = Callable[[AsyncSession], AbstractAsyncContextManager[ConnectDeps]]


class ByoRuntime:
    def __init__(
        self,
        settings: ByoSettings,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = AsyncSessionLocal,
        deps_factory: DepsFactory | None = None,
        admin_ids: Callable[[], list[int]] = app_settings.get_admin_ids,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._deps_factory = deps_factory or self._production_deps
        self._admin_ids = admin_ids
        self._tasks: dict[int, asyncio.Task] = {}
        self._bot: Any = None
        self._key = None

    @property
    def is_active(self) -> bool:
        return self._bot is not None

    def limits(self) -> NodeLimits:
        return NodeLimits(self._settings.max_nodes_per_owner, self._settings.max_new_nodes_per_day)

    def public_key(self) -> str:
        return public_key_line(self._key)

    async def start(self, bot: Any, *, engine: AsyncEngine = default_engine) -> None:
        if not self._settings.enabled:
            logger.info('BYO выключен (BYO_ENABLED=false)')
            return
        problems = self._settings.problems()
        if problems:
            logger.warning('BYO не запущен: настройки неполные', problems=problems)
            return
        try:
            await ensure_tables(engine)
            self._key = load_or_create_key(Path(self._settings.ssh_key_path))
            async with self._session_factory() as db:
                pending = [await load_node(db, node_id) for node_id in await pending_node_ids(db)]
        except Exception:
            # Бот должен подняться и без модуля: кнопки не будет, причина — в логе.
            logger.exception('BYO не запущен: не удалось подготовить таблицы, ключ или очередь')
            return
        self._bot = bot
        for node in pending:
            self.launch(node.id, chat_id=node.owner.telegram_id, message_id=None)
        logger.info('BYO запущен', resumed=len(pending))

    async def stop(self) -> None:
        # Прерванное подключение не страшно: при следующем старте оно продолжится с места.
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def is_running(self, node_id: int) -> bool:
        task = self._tasks.get(node_id)
        return task is not None and not task.done()

    def launch(self, node_id: int, *, chat_id: int, message_id: int | None) -> asyncio.Task | None:
        if self.is_running(node_id):
            return None
        task = asyncio.create_task(self._connect(node_id, chat_id, message_id), name=f'byo-node-{node_id}')
        self._tasks[node_id] = task
        task.add_done_callback(lambda finished: self._forget_task(node_id, finished))
        return task

    def _forget_task(self, node_id: int, finished: asyncio.Task) -> None:
        if self._tasks.get(node_id) is finished:
            del self._tasks[node_id]

    async def _connect(self, node_id: int, chat_id: int, message_id: int | None) -> None:
        async with self._session_factory() as db:
            node = await load_node(db, node_id)
            if node is None:
                logger.warning('BYO: нода для подключения не найдена', node_id=node_id)
                return
            journal = DatabaseStepJournal(db, node.id)
            progress = ProgressMessage(bot=self._bot, chat_id=chat_id, ip=node.ip, message_id=message_id)
            progress.done.update(await journal.done_steps())
            try:
                async with self._deps_factory(db) as deps:
                    await connect_node(node, deps, journal, progress=progress.step)
            except ProvisioningError as error:
                await self._report_failure(node, progress, step=error.step, reason=error.detail)
                return
            except Exception:
                logger.exception('BYO: непредвиденная ошибка подключения', node_id=node_id)
                # После отката атрибуты протухают: перечитываем ноду, а не лезем в них лениво.
                await db.rollback()
                node = await load_node(db, node_id)
                node.status = ByoNodeStatus.FAILED.value
                await db.commit()
                await self._report_failure(node, progress, step=progress.current or '', reason=texts.INTERNAL_ERROR)
                return
            await self._report_success(node, progress)

    async def _report_success(self, node: ByoNode, progress: ProgressMessage) -> None:
        owner = node.owner
        with contextlib.suppress(Exception):
            await progress.finished(owner, bot_username=app_settings.BOT_USERNAME or '')
        await self._notify_admins(texts.ADMIN_CONNECTED.format(ip=node.ip, slug=owner.slug, ordinal=node.ordinal))

    async def _report_failure(self, node: ByoNode, progress: ProgressMessage, *, step: str, reason: str) -> None:
        with contextlib.suppress(Exception):
            await progress.failed(node_id=node.id, step=step, reason=reason)
        label = texts.STEP_LABELS.get(step, step)
        await self._notify_admins(
            texts.ADMIN_FAILED.format(ip=node.ip, slug=node.owner.slug, ordinal=node.ordinal, step=label, reason=reason)
        )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self._admin_ids():
            try:
                await self._bot.send_message(admin_id, text, parse_mode=None)
            except Exception as error:
                logger.warning('BYO: не удалось уведомить админа', admin_id=admin_id, error=str(error))

    @contextlib.asynccontextmanager
    async def _production_deps(self, db: AsyncSession) -> AsyncIterator[ConnectDeps]:
        settings = self._settings
        egress = settings.egress_proxy_url
        async with (
            RemnaWaveService().get_api_client() as api,
            httpx.AsyncClient(proxy=egress, timeout=REGRU_TIMEOUT_SECONDS) as http,
        ):
            yield ConnectDeps(
                settings=settings.connect_settings(),
                panel=PanelProvisioner(BotPanel(api)),
                dns=RegruClient(settings.regru_username, settings.regru_password.get_secret_value(), http=http),
                open_ssh=functools.partial(ssh.connect, client_keys=[self._key], proxy_url=egress),
                bot=BotOwnerObjects(db),
                save=db.commit,
            )


byo_runtime = ByoRuntime(byo_settings)
