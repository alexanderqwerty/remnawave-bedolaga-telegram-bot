"""Идемпотентное подключение ноды с журналом шагов.

Подключение — это список именованных шагов. Каждый выполняется под защитой
журнала: успех записывается, после сбоя повтор пропускает уже сделанное и
продолжает с упавшего шага. Так «повтор с места сбоя» из плана работает, даже
если бот перезапустился между шагами.

Здесь проверяется только машина шагов на поддельных шагах — без SSH, DNS и
панели. Настоящие шаги подключаются и тестируются отдельно.
"""

from __future__ import annotations

import pytest

from app.byo.models import ByoStepStatus
from app.byo.provisioning import ProvisioningError, Step, run_steps


class FakeJournal:
    """Журнал в памяти вместо таблицы byo_steps."""

    def __init__(self, already_done: set[str] | None = None) -> None:
        self.status: dict[str, str] = dict.fromkeys(already_done or set(), ByoStepStatus.DONE.value)
        self.detail: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        self.writes: list[tuple[str, str]] = []

    async def is_done(self, name: str) -> bool:
        return self.status.get(name) == ByoStepStatus.DONE.value

    async def mark_running(self, name: str) -> None:
        self.attempts[name] = self.attempts.get(name, 0) + 1
        self.writes.append((name, 'running'))

    async def mark_done(self, name: str) -> None:
        self.status[name] = ByoStepStatus.DONE.value
        self.writes.append((name, 'done'))

    async def mark_failed(self, name: str, detail: str) -> None:
        self.status[name] = ByoStepStatus.FAILED.value
        self.detail[name] = detail
        self.writes.append((name, 'failed'))


def step(name: str, log: list[str], *, fail_with: str | None = None) -> Step:
    async def action(context: dict) -> None:
        log.append(name)
        if fail_with is not None:
            raise RuntimeError(fail_with)

    return Step(name=name, action=action)


@pytest.mark.asyncio
async def test_all_steps_run_in_order_on_a_clean_journal():
    log: list[str] = []
    journal = FakeJournal()
    steps = [step('ssh', log), step('verify', log), step('dns', log)]

    await run_steps(steps, journal, context={})

    assert log == ['ssh', 'verify', 'dns']
    assert [name for name, status in journal.writes if status == 'done'] == ['ssh', 'verify', 'dns']


@pytest.mark.asyncio
async def test_finished_steps_are_skipped_on_resume():
    log: list[str] = []
    journal = FakeJournal(already_done={'ssh', 'verify'})
    steps = [step('ssh', log), step('verify', log), step('dns', log)]

    await run_steps(steps, journal, context={})

    assert log == ['dns'], 'повтор не должен переделывать ssh и verify'


@pytest.mark.asyncio
async def test_a_failing_step_stops_the_rest_and_is_recorded():
    log: list[str] = []
    journal = FakeJournal()
    steps = [
        step('ssh', log),
        step('verify', log, fail_with='no free port 443'),
        step('dns', log),
    ]

    with pytest.raises(ProvisioningError) as error:
        await run_steps(steps, journal, context={})

    assert log == ['ssh', 'verify'], 'шаги после упавшего не выполняются'
    assert error.value.step == 'verify'
    assert journal.status['verify'] == ByoStepStatus.FAILED.value
    assert 'no free port 443' in journal.detail['verify']


@pytest.mark.asyncio
async def test_resume_after_a_failure_reruns_only_the_failed_step_onward():
    log: list[str] = []
    journal = FakeJournal(already_done={'ssh'})
    # На повторе verify уже чинится: тот же список шагов, но без сбоя.
    steps = [step('ssh', log), step('verify', log), step('dns', log)]

    await run_steps(steps, journal, context={})

    assert log == ['verify', 'dns']
    assert journal.attempts == {'verify': 1, 'dns': 1}


@pytest.mark.asyncio
async def test_each_attempt_is_counted():
    log: list[str] = []
    journal = FakeJournal()
    steps = [step('ssh', log, fail_with='timeout')]

    for _ in range(3):
        with pytest.raises(ProvisioningError):
            await run_steps(steps, journal, context={})

    assert journal.attempts['ssh'] == 3


@pytest.mark.asyncio
async def test_steps_share_a_context_so_later_steps_see_earlier_results():
    async def produce(context: dict) -> None:
        context['fqdn'] = 'node.example'

    async def consume(context: dict) -> None:
        context['seen'] = context['fqdn']

    context: dict = {}
    await run_steps([Step('produce', produce), Step('consume', consume)], FakeJournal(), context=context)

    assert context['seen'] == 'node.example'


@pytest.mark.asyncio
async def test_a_skipped_step_still_contributes_its_context():
    # Повтор пропускает выполнение шага, но следующий шаг всё равно должен получить
    # его результат — иначе продолжение с середины развалится.
    async def rebuild_context(context: dict) -> None:
        context['ws_path'] = '/known'

    async def use_it(context: dict) -> None:
        context['used'] = context['ws_path']

    context: dict = {}
    await run_steps(
        [Step('secrets', rebuild_context, restore=rebuild_context), Step('host', use_it)],
        FakeJournal(already_done={'secrets'}),
        context=context,
    )

    assert context['used'] == '/known'
