"""Итог хода пишется на каждом выходе прогона — ровно одна запись на ход.

Каркас честный: настоящие `acreate_run_context`, `AgentExecutor.arun`, цикл
агента и файловое хранилище в `tmp_path`; подменён только запрос к модели
(`_arequest_backend_canonical`). Монотоника часов хода — подменная: время
двигает тест, поэтому длительности точные.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from gc_core import agent_executor as ae
from gc_core import processing_responses as pr
from gc_core import run_summary_coordinator as rsc
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.root import AppConfig
from gc_core.execution import turn_clock
from gc_core.execution.turn_clock import TurnClock, current_turn_clock
from gc_core.persistence.turn_outcome import (
    CANCELLED,
    TURN_OUTCOME_TYPE,
    USER_STOP,
    is_turn_outcome_record,
    parse_turn_outcome,
    turn_shown_in,
)
from gc_core.run_context import acreate_run_context
from gc_core.security.log_redaction import MASK
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.states import State
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import ThreadBusy
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.utils.connect_backend import _aguard_model_call
from gc_core.utils.logging_module import release_run_logger

_SYS = "test"
_TID = "t1"
_KEY = ThreadKey(system=_SYS, thread=_TID)
_LIMITS = (
    "step_limit",
    "tool_call_limit",
    "token_limit",
    "tool_errors",
    "judge_unavailable",
)


class _Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _call(name: str, call_id: str, arguments: dict) -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "status": "completed",
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def _finish(call_id: str = "finish") -> dict:
    return _call(
        "turn_finish",
        call_id,
        {
            "solution_type": "full_solution",
            "task": "сделать",
            "task_report": "Готово",
            "handoff": "{}",
        },
    )


def _ask(call_id: str = "ask") -> dict:
    return _call("turn_ask_human", call_id, {"question": "Какой файл?"})


class _Run:
    """Исполнитель на файловом треде и сценарий ответов модели."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.threads = tmp_path / "threads"
        self.store = FileThreadStore(self.threads)
        self.mono = _Mono()
        self.requests: list[list[dict]] = []
        self.script: list = []
        self.ex: AgentExecutor | None = None
        self.ctx = None
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            turn_clock, "time", SimpleNamespace(monotonic=self.mono)
        )
        monkeypatch.setattr(pr, "_arequest_backend_canonical", self._backend)

    async def build(
        self,
        *,
        api_mode: str = "chat_completions",
        text_answer_mode: str = "deny",
        **agent,
    ):
        config = AppConfig.model_validate(
            {
                "level": "default",
                "backend": {"api_mode": api_mode, "vision": False},
                "logging": {"save_logs": False, "console_trace": False},
                "agent": {
                    "strict_turn_finish_validation": False,
                    "stop_tools": ["project_memory"],
                    "use_local_subagents": False,
                    **agent,
                },
                "models": {
                    "mid": {
                        "model": "mid",
                        "url": "http://local/v1/chat/completions",
                        "token": "",
                    }
                },
                "slots": {"light": "mid", "default": "mid", "max": "mid"},
            }
        )
        self.ctx = await acreate_run_context(
            LoadedSettings(
                settings=config,
                runtime=RuntimeEnv(
                    launch_root=str(self.tmp_path),
                    text_answer_mode=text_answer_mode,
                ),
            ),
            thread_store=self.store,
        )
        self.ex = AgentExecutor(self.ctx)
        return self

    async def _backend(self, _messages, state, _ctx, **_kwargs):
        self.requests.append(list(state.messages))
        step = self.script.pop(0)
        if callable(step):
            step = await step()
        return step, "resp", {}

    async def arun(self, text: str = "сделай", mid: str = "m_user", **kw):
        try:
            return await self.ex.arun(
                [text],
                system=_SYS,
                thread_id=_TID,
                message_ids=[mid],
                **kw,
            )
        finally:
            release_run_logger(self.ctx.logger)

    def messages(self) -> list[dict]:
        entries = run_sync(self.store.read_stream(_KEY, "messages"))
        return [
            {"type": "message", "seq": e.seq, "payload": e.data}
            for e in entries
        ]

    def records(self) -> list[dict]:
        """Записи потока итогов — конвертами, как их видят читатели."""
        entries = run_sync(self.store.read_stream(_KEY, "turns"))
        return [
            {"type": TURN_OUTCOME_TYPE, "seq": e.seq, **e.data}
            for e in entries
        ]

    def lines(self) -> list[dict]:
        """История с записями итога за сообщением своей привязки."""
        lines = self.messages()
        for record in self.records():
            at = next(
                i
                for i, line in enumerate(lines)
                if line.get("payload", {}).get("id") == record["after"]
            )
            lines.insert(at + 1, record)
        return lines


@pytest.fixture
async def run(tmp_path, monkeypatch):
    return await _Run(tmp_path, monkeypatch).build()


def _only_outcomes(run: _Run):
    return [
        parse_turn_outcome(r) for r in run.lines() if is_turn_outcome_record(r)
    ]


# ---- финал ----------------------------------------------------------------


async def test_finish_writes_done_after_last_message(run):
    async def _work():
        run.mono.now += 12.0
        return [_finish()]

    run.script = [_work]
    await run.arun()

    lines = run.lines()
    assert is_turn_outcome_record(lines[-1])
    assert sum(is_turn_outcome_record(r) for r in lines) == 1
    outcome = parse_turn_outcome(lines[-1])
    assert (outcome.status, outcome.reason) == ("done", None)
    assert outcome.opened_by == "m_user"
    assert outcome.duration_sec == 12.0
    assert outcome.error is None
    assert outcome.started_at.endswith("Z")
    assert outcome.ended_at.endswith("Z")
    assert lines[-1]["after"] == lines[-2]["payload"]["id"]


async def test_accepted_text_writes_done(tmp_path, monkeypatch):
    run = await _Run(tmp_path, monkeypatch).build(text_answer_mode="allow")
    run.script = [
        [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Ответ текстом"}],
            }
        ]
    ]
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("done", None)]


async def test_responses_mode_finish_writes_done(tmp_path, monkeypatch):
    run = await _Run(tmp_path, monkeypatch).build(api_mode="responses")
    run.script = [[_finish()]]
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("done", None)]


async def test_next_turn_model_does_not_see_record(run):
    run.script = [[_finish("f1")], [_finish("f2")]]
    await run.arun("первый", "m1")
    await run.arun("второй", "m2")

    second_request = run.requests[-1]
    assert all(m.get("type") != "turn_outcome" for m in second_request)
    assert all("outcome" not in m for m in second_request)
    assert all("outcome" not in m["payload"] for m in run.messages())
    lines = run.lines()
    kinds = [r["type"] for r in lines]
    assert kinds.count("turn_outcome") == 2
    first_outcome = kinds.index("turn_outcome")
    assert lines[first_outcome + 1]["payload"]["id"] == "m2"


async def test_record_lands_before_turn_idle_and_lease_release(
    run, monkeypatch
):
    """Итог хода пишется по аренде хода: до отметки «ход окончен» и до
    того, как тред отпущен другому процессу."""
    seen: list = []
    mark, release = ThreadSession.amark_turn, ThreadSession.arelease_lease

    async def _mark(session, *, running):
        if not running:
            seen.append(("idle", len(run.records())))
        return await mark(session, running=running)

    async def _release(session):
        seen.append(("release", len(run.records())))
        return await release(session)

    monkeypatch.setattr(ThreadSession, "amark_turn", _mark)
    monkeypatch.setattr(ThreadSession, "arelease_lease", _release)

    async def _fail():
        raise RuntimeError("boom")

    run.script = [[_finish("f1")], _fail]
    await run.arun("первый", "m1")
    with pytest.raises(RuntimeError):
        await run.arun("второй", "m2")

    assert seen == [("idle", 1), ("release", 1), ("idle", 2), ("release", 2)]


async def test_record_lands_before_turn_end_compaction(run, monkeypatch):
    """Итог хода — первым в закрытии хода: компакция конца хода видит его
    в файле и уносит в архив вместе с ходом."""
    seen: list = []

    async def _compact(_coordinator):
        seen.append(len(run.records()))

    monkeypatch.setattr(
        rsc.RunSummaryCoordinator, "aturn_end_compact_if_needed", _compact
    )
    run.script = [[_finish()]]
    await run.arun()

    assert seen == [1]


async def test_subagent_loop_writes_nothing_and_keeps_parent_clock(run):
    """Цикл ребёнка хука не получает; подхват своей очереди часы
    родителя не перепривязывает и не закрывает."""
    parent = TurnClock.start(mono=run.mono, wall=turn_clock.utc_now)
    turn_clock.bind_turn_clock(run.ctx, parent)
    child = State(
        verbose=False,
        model="mid",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
        parent_state_id="parent",
    )
    child.backend_api_mode = "chat_completions"
    queue: list = []

    async def _first():
        queue.append([{"role": "user", "content": "ещё", "id": "m_child"}])
        return [_finish("f1")]

    run.script = [_first, [_finish("f2")]]

    async def _pending():
        return queue.pop() if queue else []

    await pr.arun_agent(
        [{"role": "user", "content": "задача", "id": "m_task"}],
        child,
        run.ctx,
        get_pending_messages=_pending,
    )

    assert len(run.requests) == 2
    assert current_turn_clock(run.ctx) is parent
    assert parent.recorded is False
    assert parent.closed is False
    assert run.records() == []


# ---- очередь после финала: две записи за один прогон ----------------------


async def test_queued_reply_after_finish_closes_turn_first(run):
    async def _first():
        run.mono.now += 3.0
        run.ex.add_message(
            {"role": "user", "content": "и ещё", "id": "m_next"}
        )
        return [_finish("f1")]

    async def _second():
        run.mono.now += 5.0
        return [_finish("f2")]

    run.script = [_first, _second]
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.opened_by) for o in outcomes] == [
        ("done", "m_user"),
        ("done", "m_next"),
    ]
    assert [o.duration_sec for o in outcomes] == [3.0, 5.0]
    lines = run.lines()
    first = next(i for i, r in enumerate(lines) if is_turn_outcome_record(r))
    assert lines[first + 1]["payload"]["id"] == "m_next"


# ---- стоп -----------------------------------------------------------------


async def test_stop_between_finish_and_hook_is_user_stop(run, monkeypatch):
    original = pr._AgentLoop._aevaluate_finish_gate

    async def _gate(loop, *args, **kwargs):
        decision = await original(loop, *args, **kwargs)
        run.ex.stop_run()
        return decision

    monkeypatch.setattr(pr._AgentLoop, "_aevaluate_finish_gate", _gate)
    run.script = [[_finish()]]
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("stopped", USER_STOP)]


@pytest.mark.parametrize("discard", [False, True])
async def test_stop_mid_turn_writes_user_stop_at_stop_moment(run, discard):
    stopped_at: dict = {}

    async def _stop_while_thinking():
        run.mono.now += 4.0
        run.ex.stop_run()
        stopped_at["ended"] = current_turn_clock(run.ctx).ended_at
        run.mono.now += 60.0
        return [_finish()]

    run.script = [_stop_while_thinking]
    await run.arun(discard_writes_after_stop=discard)

    lines = run.lines()
    assert is_turn_outcome_record(lines[-1])
    outcome = parse_turn_outcome(lines[-1])
    assert (outcome.status, outcome.reason) == ("stopped", USER_STOP)
    assert outcome.ended_at == stopped_at["ended"]
    assert outcome.duration_sec == 4.0
    assert len(run.records()) == 1


async def test_stop_interrupting_model_request_writes_its_time(run):
    """Стоп рвёт ожидание ответа модели (снятие хода): прогон выходит сразу,
    и итог «stopped» пишется с моментом стопа — у «■ Отменено» есть время."""

    async def _hang():
        run.mono.now += 3.0

        async def _never():
            await asyncio.sleep(30)

        return await _aguard_model_call(_never)

    async def _stop_soon():
        await asyncio.sleep(0.2)
        run.ex.stop_run()

    run.script = [_hang]
    stopper = asyncio.create_task(_stop_soon())
    await asyncio.wait_for(run.arun(), 10)
    await stopper

    (outcome,) = _only_outcomes(run)
    assert (outcome.status, outcome.reason) == ("stopped", USER_STOP)
    assert outcome.ended_at is not None
    assert outcome.duration_sec == 3.0


async def test_discarded_stop_does_not_flush_deferred(run, monkeypatch):
    seen: list = []
    original = ThreadSession.aappend_turn_outcome

    async def _spy(session, outcome, *, discarded):
        seen.append(discarded)
        return await original(session, outcome, discarded=discarded)

    monkeypatch.setattr(ThreadSession, "aappend_turn_outcome", _spy)

    async def _stop():
        run.ex.stop_run()
        return [_finish()]

    run.script = [_stop]
    await run.arun(discard_writes_after_stop=True)

    assert seen == [True]


async def test_esc_rollback_orphans_record(run):
    """Откат реплики снимает только сообщение: запись её хода остаётся в
    потоке сиротой и не видна читателям."""

    async def _stop():
        run.ex.stop_run()
        return [_finish()]

    run.script = [_stop]
    await run.arun(discard_writes_after_stop=True)
    (record,) = run.records()
    assert turn_shown_in(record, {"m_user"})

    assert await Threads(run.store).discard_message(_KEY, "m_user")
    ids = {m["payload"]["id"] for m in run.messages()}
    assert run.records() == [record]
    assert not turn_shown_in(record, ids)


def _stop_in_setup(run, monkeypatch) -> None:
    """Стоп приходит в сетапе, до часов хода; после того как стоп лёг на
    ход, сетап ещё идёт 30 с (сжатие перед прогоном и т. п.)."""
    build = AgentExecutor._abuild_run_state
    bind = AgentExecutor._bind_state

    async def _build(ex, **kwargs):
        ex.stop_run()
        run.mono.now += 2.0
        return await build(ex, **kwargs)

    def _bind(ex, state, **kwargs):
        bind(ex, state, **kwargs)
        run.mono.now += 30.0

    monkeypatch.setattr(AgentExecutor, "_abuild_run_state", _build)
    monkeypatch.setattr(AgentExecutor, "_bind_state", _bind)


async def test_stop_in_setup_closes_new_clock(run, monkeypatch):
    """Новые часы закрываются моментом, когда стоп лёг на ход; ход без
    сообщений в истории записи не получает — откату нечего снимать."""
    _stop_in_setup(run, monkeypatch)
    run.script = [[_finish()]]
    await run.arun()

    clock = current_turn_clock(run.ctx)
    assert clock.closed is True
    assert clock.elapsed() == 0.0
    assert clock.recorded is True
    assert run.records() == []
    assert run.requests == []


async def test_stop_in_setup_after_previous_turn(run, monkeypatch):
    """Прошлый ход записан; стоп в сетапе следующего не даёт ни второй
    записи прошлому, ни записи-сироты новому ходу."""
    run.script = [[_finish("f1")]]
    await run.arun("первый", "m1")
    before = run.records()
    _stop_in_setup(run, monkeypatch)
    run.script = [[_finish("f2")]]
    await run.arun("второй", "m2")

    assert run.records() == before
    assert [o.opened_by for o in _only_outcomes(run)] == ["m1"]


# ---- лимиты, ошибки, отмена -----------------------------------------------


async def test_step_limit_writes_stopped(tmp_path, monkeypatch):
    run = await _Run(tmp_path, monkeypatch).build(max_steps=1)
    run.script = [[_ask("a1")]] + [[_finish()]] * 3
    run.ctx.ask_human = lambda _q: "a.py"
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [
        ("stopped", "step_limit")
    ]


@pytest.mark.parametrize("limit", _LIMITS)
async def test_limit_outcome_is_stopped_with_reason(run, limit):
    """Лимит прогона (``state.terminal_outcome``) закрывает ход в
    ``finally`` — из цикла хук не зовётся."""

    async def _loop(messages, state, _ctx, **_kw):
        state.mark_turn_start()
        state.messages += [m for m in messages if isinstance(m, dict)]
        state.terminal_outcome = limit
        return None

    run.monkeypatch.setattr(ae, "arun_agent", _loop)
    await Threads(run.store).append_messages(
        _KEY, [{"role": "user", "content": "x", "id": "m_old"}]
    )
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("stopped", limit)]


async def test_backend_error_writes_masked_error(run):
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0"

    async def _fail():
        run.mono.now += 1.5
        raise RuntimeError(f"401 for https://api.example/v1?key={key}\nmore")

    run.script = [_fail]
    with pytest.raises(RuntimeError):
        await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("error", None)]
    assert outcomes[0].error == {
        "type": "RuntimeError",
        "message": f"RuntimeError: 401 for https://api.example/v1?key={MASK}",
    }
    assert outcomes[0].duration_sec == 1.5
    path = run.threads / f"data_{_SYS}_{_TID}.jsonl"
    assert key not in path.read_text()


async def test_cancelled_run_writes_cancelled(run):
    """Отмена без запроса стопа — не стоп пользователя: отменить задачу
    может и вызывающий код (таймаут, остановка приложения)."""

    async def _cancel():
        raise asyncio.CancelledError

    run.script = [_cancel]
    with pytest.raises(asyncio.CancelledError):
        await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("stopped", CANCELLED)]


async def test_task_cancel_writes_cancelled(run):
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.sleep(30)
        return [_finish()]

    run.script = [_hang]
    task = asyncio.ensure_future(run.arun())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Запись под отменой не снимает её с задачи: `asyncio.timeout` и
    # TaskGroup вызывающего опираются на счётчик.
    assert task.cancelling() > 0
    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("stopped", CANCELLED)]


async def test_stop_then_task_cancel_writes_user_stop(run):
    """Стоп запрошен (`stop_run`), затем задачу отменили: отмена —
    следствие стопа, исход — стоп пользователя."""
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.sleep(30)
        return [_finish()]

    run.script = [_hang]
    task = asyncio.ensure_future(run.arun())
    await started.wait()
    run.ex.stop_run()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("stopped", USER_STOP)]


async def test_error_after_loop_record_keeps_done(run, monkeypatch):
    """Решение плана 9: ход уже записан из цикла — сбой после записи
    второй записи не даёт (ошибка в неё не попадает — непокрыто)."""

    run.script = [[_finish()]]
    original = pr._AgentLoop._aclose_turn

    async def _close(loop, result, **kw):
        await original(loop, result, **kw)
        raise RuntimeError("после записи")

    monkeypatch.setattr(pr._AgentLoop, "_aclose_turn", _close)
    with pytest.raises(RuntimeError):
        await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [("done", None)]


async def test_error_before_loop_writes_nothing(run):
    with pytest.raises(ValueError):
        await run.ex.arun([], system=_SYS, thread_id=_TID)
    release_run_logger(run.ctx.logger)

    assert run.records() == []


@pytest.mark.parametrize(
    "error", [OSError("disk full"), ThreadBusy("тред ведёт другой процесс")]
)
async def test_record_write_failure_does_not_fail_run(run, monkeypatch, error):
    """Сбой записи и отказ по аренде (``OwnershipError``) — предупреждение
    в журнал прогона, ход не падает."""

    async def _broken(*_a, **_kw):
        raise error

    monkeypatch.setattr(ThreadSession, "aappend_turn_outcome", _broken)
    run.script = [[_finish()]]
    await run.arun()

    assert run.records() == []


async def test_record_build_failure_does_not_fail_run(run, monkeypatch):
    """Сбой сборки записи (кривой факт) — тоже только предупреждение."""

    def _broken(*_a, **_kw):
        raise ValueError("bad outcome")

    monkeypatch.setattr(ae, "TurnOutcome", _broken)
    run.script = [[_finish()]]
    await run.arun()

    assert run.records() == []


# ---- вопрос агента --------------------------------------------------------


async def test_ask_human_wait_is_not_counted(run):
    def _answer(_question):
        run.mono.now += 30.0
        return "a.py"

    async def _think():
        run.mono.now += 2.0
        return [_ask()]

    async def _done():
        run.mono.now += 3.0
        return [_finish()]

    run.ctx.ask_human = _answer
    run.script = [_think, _done]
    await run.arun()

    outcomes = _only_outcomes(run)
    assert len(outcomes) == 1
    assert outcomes[0].duration_sec == 5.0


# ---- план ------------------------------------------------------------------


async def test_plan_proposed_writes_done_plan(run):
    run.ex.set_permission_mode("plan")
    run.script = [
        [_call("turn_propose_plan", "plan", {"plan": "1. сделать\n2. всё"})]
    ]
    await run.arun()

    outcomes = _only_outcomes(run)
    assert [(o.status, o.reason) for o in outcomes] == [
        ("done", "plan_proposed")
    ]


async def test_accepted_plan_continuation_has_no_opener(run):
    run.ex.set_permission_mode("plan")
    run.script = [
        [_call("turn_propose_plan", "plan", {"plan": "1. сделать\n2. всё"})],
        [_finish()],
    ]
    await run.arun()
    try:
        await run.ex.aexecute_accepted_plan("auto", _TID, system=_SYS)
    finally:
        release_run_logger(run.ctx.logger)

    outcomes = _only_outcomes(run)
    assert [(o.status, o.opened_by) for o in outcomes] == [
        ("done", "m_user"),
        ("done", None),
    ]


# ---- классификация --------------------------------------------------------


def _executor_with_state(**state_fields) -> AgentExecutor:
    ex = AgentExecutor(SimpleNamespace())
    fields = {"stop_requested": False, "terminal_outcome": None}
    ex._state = SimpleNamespace(**{**fields, **state_fields})
    return ex


@pytest.mark.parametrize(
    ("reason", "error", "fields", "expected"),
    [
        (None, None, {}, ("done", None)),
        ("plan_proposed", None, {}, ("done", "plan_proposed")),
        (None, None, {"stop_requested": True}, ("stopped", USER_STOP)),
        (
            "plan_proposed",
            None,
            {"stop_requested": True},
            ("stopped", USER_STOP),
        ),
        (
            None,
            RuntimeError("x"),
            {"stop_requested": True},
            ("stopped", USER_STOP),
        ),
        (None, asyncio.CancelledError(), {}, ("stopped", CANCELLED)),
        (
            None,
            asyncio.CancelledError(),
            {"stop_requested": True},
            ("stopped", USER_STOP),
        ),
        (None, KeyboardInterrupt(), {}, ("stopped", USER_STOP)),
        (None, RuntimeError("x"), {}, ("error", None)),
        *[
            (None, None, {"terminal_outcome": lim}, ("stopped", lim))
            for lim in _LIMITS
        ],
        (None, None, {"terminal_outcome": "plan_proposed"}, ("done", None)),
    ],
)
def test_classify_turn_end(reason, error, fields, expected):
    ex = _executor_with_state(**fields)
    assert ex._classify_turn_end(reason, error) == expected


async def test_recorded_clock_is_not_written_twice(run):
    run.script = [[_finish()]]
    await run.arun()
    clock = current_turn_clock(run.ctx)
    assert isinstance(clock, TurnClock) and clock.recorded is True

    await run.ex._arecord_turn_outcome(None)

    assert len(run.records()) == 1


# ---- время вызовов хода в записи (P4: `outcome.calls`) --------------------


def _read(call_id: str) -> dict:
    return _call("fs_read_file", call_id, {"path": "a.txt"})


def _timed_tools(run: _Run, steps: dict) -> None:
    """Вызов тула двигает часы на `steps[call_id]`; остальное — настоящее.

    Монотоника вызова (`processing_responses.time`) — та же подменная, что у
    часов хода: в рантайме у них один источник."""
    run.monkeypatch.setattr(pr, "time", SimpleNamespace(monotonic=run.mono))
    original = pr._ainvoke_tool

    async def _invoke(name, arguments, state, ctx, run_agent_func, call_id):
        step = steps.get(call_id)
        if callable(step):
            return await step(ctx)
        run.mono.now += step or 0.0
        return await original(
            name, arguments, state, ctx, run_agent_func, call_id
        )

    run.monkeypatch.setattr(pr, "_ainvoke_tool", _invoke)
    (run.tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")


async def test_calls_of_the_turn_are_recorded(run):
    _timed_tools(run, {"r1": 2.0, "r2": 0.5})
    run.script = [[_read("r1"), _read("r2")], [_finish()]]
    await run.arun()

    (outcome,) = _only_outcomes(run)
    # Финал — тоже завершившийся вызов; не пишутся только промежуточная
    # реплика и вопрос агента.
    assert outcome.calls == {"r1": 2.0, "r2": 0.5, "finish": 0.0}
    assert run.records()[0]["outcome"]["v"] == 1


async def test_confirmation_wait_is_not_in_call_time(run):
    async def _confirmed(ctx):
        run.mono.now += 1.0
        with turn_clock.human_wait(ctx):
            run.mono.now += 30.0
        run.mono.now += 1.0
        return '{"ok": true}', False

    _timed_tools(run, {"r1": _confirmed})
    run.script = [[_read("r1")], [_finish()]]
    await run.arun()

    (outcome,) = _only_outcomes(run)
    assert outcome.calls == {"r1": 2.0, "finish": 0.0}


async def test_call_stopped_midway_is_absent(run):
    async def _stopped(_ctx):
        run.mono.now += 3.0
        run.ex.stop_run()
        run.mono.now += 3.0
        return '{"ok": true}', False

    _timed_tools(run, {"r1": 1.0, "r2": _stopped})
    run.script = [[_read("r1"), _read("r2")], [_finish()]]
    await run.arun()

    (outcome,) = _only_outcomes(run)
    assert outcome.status == "stopped"
    assert outcome.calls == {"r1": 1.0}


async def test_refused_call_is_recorded_by_core(run):
    """Ядро пишет время и отказа: таймер не рисует фронтенд, по исходу."""

    async def _refused(_ctx):
        run.mono.now += 0.2
        refusal = {"ok": False, "status": "refused", "error": {"type": "x"}}
        return json.dumps(refusal), False

    _timed_tools(run, {"r1": _refused})
    run.script = [[_read("r1")], [_finish()]]
    await run.arun()

    (outcome,) = _only_outcomes(run)
    assert outcome.calls == {"r1": 0.2, "finish": 0.0}


async def test_turn_without_calls_has_no_calls_key(tmp_path, monkeypatch):
    run = await _Run(tmp_path, monkeypatch).build(text_answer_mode="allow")
    run.script = [
        [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Ответ"}],
            }
        ]
    ]
    await run.arun()

    assert "calls" not in run.records()[0]["outcome"]
    assert _only_outcomes(run)[0].calls is None


async def test_nested_call_lands_in_parent_clock(run, monkeypatch):
    """Ребёнок исполняет вызов на ctx с часами родителя — время в их карте."""
    monkeypatch.setattr(pr, "time", SimpleNamespace(monotonic=run.mono))
    parent = TurnClock.start(mono=run.mono, wall=turn_clock.utc_now)
    turn_clock.bind_turn_clock(run.ctx, parent)

    async def _invoke(name, arguments, state, ctx, run_agent_func, call_id):
        run.mono.now += 4.0
        return '{"ok": true}', False

    monkeypatch.setattr(pr, "_ainvoke_tool", _invoke)
    run.ctx.parent_call_id = "parent_fc"
    child = State(
        verbose=False,
        model="mid",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
        parent_state_id="parent",
    )
    await pr.aprocess_tool_calls(
        messages=[_read("nested")],
        state=child,
        ctx=run.ctx,
        run_agent_func=None,
    )

    assert parent.calls() == {"nested": 4.0}


async def test_running_call_counts_on_turn_clock(run, monkeypatch):
    """Идущий вызов — на часах хода с его старта (живой счёт фронтенда);
    законченный — то же время, что в карте записи итога."""
    monkeypatch.setattr(pr, "time", SimpleNamespace(monotonic=run.mono))
    clock = TurnClock.start(mono=run.mono, wall=turn_clock.utc_now)
    turn_clock.bind_turn_clock(run.ctx, clock)
    seen = []

    async def _invoke(name, arguments, state, ctx, run_agent_func, call_id):
        run.mono.now += 12.6
        seen.append(clock.call_elapsed("live"))
        run.mono.now += 0.44
        return '{"ok": true}', False

    monkeypatch.setattr(pr, "_ainvoke_tool", _invoke)
    state = State(
        verbose=False,
        model="mid",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    await pr.aprocess_tool_calls(
        messages=[_read("live")],
        state=state,
        ctx=run.ctx,
        run_agent_func=None,
    )

    assert seen == [pytest.approx(12.6)]
    assert clock.call_elapsed("live") == clock.calls()["live"] == 13.0


async def test_ask_human_is_not_a_timed_call(run):
    def _answer(_question):
        run.mono.now += 30.0
        return "a.py"

    run.ctx.ask_human = _answer
    run.script = [[_ask()], [_finish()]]
    await run.arun()

    (outcome,) = _only_outcomes(run)
    assert "ask" not in (outcome.calls or {})
