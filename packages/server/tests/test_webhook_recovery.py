"""Восстановление прерванных задач вебхука после падения (спека сервера, 13.4).

Тред, у которого ход был отмечен идущим (``runtime.turn == "running"``), а
аренды больше нет, или во входящем потоке которого лежит недоставленное, —
бесхозный. Обход инстанса берёт такой тред (захват аренды выигрывает один) и
продолжает ходом с техническим сообщением о сбое плюс недоставленные.
"""

import asyncio
import json

import httpx
import pytest
from gc_core.execution.environment import utc_now
from gc_core.threads.facade import Threads
from gc_core.threads.models import RuntimeStatus, ThreadKey
from gc_core.threads.session_writer import SessionWriter
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_server.stores import aopen_process_stores
from gc_server.webhook import status as task_status
from gc_server.webhook.recovery import RECOVERY_MESSAGE, arecover_once
from gc_server.webhook.surface import WebhookSurface
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="generic", thread="t")


def _settings(tmp_path):
    return make_settings(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.model": "scripted",
            "agent.max_steps": 3,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "logging.save_logs": False,
            "logging.console_trace": False,
            "paths.threads_data_path": str(tmp_path / "threads"),
            "storage": {
                "threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}
            },
        }
    )


def _install(monkeypatch):
    from gc_core.utils import connect_backend

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json=scripted_model.FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


async def _surface(tmp_path, stores, recovery=None):
    config = {"webhook": {"recovery": recovery}} if recovery else {}
    return WebhookSurface(
        ServerRuntime(
            base=_settings(tmp_path),
            config=ServerConfig.model_validate(config),
            stores=stores,
        )
    )


async def _crashed_turn(store, key):
    """Ход умер посреди работы: история записана, отметка хода стоит,
    аренды нет (истекла)."""
    dead = SessionWriter(store, key, owner="dead/1/deadbeef")
    await dead.aclaim()
    await dead.acommit(
        append_messages=[{"id": "u1", "role": "user", "content": "задача"}]
    )
    await dead.aside(set_runtime=RuntimeStatus(turn="running"))
    await dead.arelease()


async def _crashed_run(store, key=KEY):
    """Ход задачи вебхука умер посреди работы: статус задачи — running
    прежнего инстанса."""
    await _crashed_turn(store, key)
    await task_status.aset_status(
        Threads(store), key, status="running", run_id="dead:1", started_at=1.0
    )


async def _settled(*surfaces) -> None:
    for _ in range(300):
        tasks = [t for s in surfaces for t in s.registry.live_tasks()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("прогоны восстановления не закончились")


def _mentions(payload, text: str) -> bool:
    return any(
        text in str(m.get("content"))
        for m in payload.get("messages", [])
        if m.get("role") == "user"
    )


async def test_interrupted_task_is_continued(monkeypatch, tmp_path):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    statuses: list = []
    real = task_status.aset_status

    async def spy(threads, key, **fields):
        statuses.append(fields.get("status"))
        await real(threads, key, **fields)

    monkeypatch.setattr(task_status, "aset_status", spy)
    try:
        await _crashed_run(stores.threads)
        statuses.clear()
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    assert taken == [KEY]
    assert len(seen) == 1 and _mentions(seen[0], RECOVERY_MESSAGE)
    assert [s for s in statuses if s] == [
        "interrupted",
        "running",
        "completed",
    ]
    assert record.runtime.turn is None


async def test_live_foreign_lease_is_left_alone(monkeypatch, tmp_path):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        await SessionWriter(stores.threads, KEY, owner="alive").aclaim()
        taken = await arecover_once(surface, now=utc_now())
    finally:
        await stores.aclose()
    assert taken == [] and seen == []


async def test_undelivered_inbox_without_owner_is_played(
    monkeypatch, tmp_path
):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await task_status.asubmit(
            Threads(stores.threads),
            KEY,
            {"role": "user", "content": "никто не забрал"},
        )
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
    finally:
        await stores.aclose()
    assert taken == [KEY]
    assert _mentions(seen[0], "никто не забрал")
    assert not _mentions(seen[0], RECOVERY_MESSAGE)


async def test_two_instances_continue_the_task_once(monkeypatch, tmp_path):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    first = await _surface(tmp_path, stores)
    second = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        await asyncio.gather(
            arecover_once(first, now=utc_now()),
            arecover_once(second, now=utc_now()),
        )
        await _settled(first, second)
        history = await Threads(stores.threads).history(KEY)
    finally:
        await stores.aclose()
    assert len(seen) == 1
    assert (
        sum(RECOVERY_MESSAGE in str(e.data.get("content")) for e in history)
        == 1
    )


async def test_threads_of_other_surfaces_are_not_touched(
    monkeypatch, tmp_path
):
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        for system in ("a2a", "responses", "cli", "tui"):
            await _crashed_run(
                stores.threads, ThreadKey(system=system, thread="t")
            )
        taken = await arecover_once(surface, now=utc_now())
    finally:
        await stores.aclose()
    assert taken == [] and seen == []


async def test_threads_of_other_applications_are_not_touched(
    monkeypatch, tmp_path
):
    """Находка ревью: SDK-приложение на общем хранилище убито посреди хода
    (отметка хода стоит, аренда истекла) или оставило недоставленное во
    входящем — это не задача вебхука, какой бы system ни был: восстановление
    её не продолжает ходом с настройками и тулами вебхука."""
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        for system in ("sdk", "myapp"):
            await _crashed_turn(
                stores.threads, ThreadKey(system=system, thread="crash-1")
            )
        await Threads(stores.threads).submit(
            ThreadKey(system="myapp", thread="inbox"),
            {"role": "user", "content": "чужое"},
        )
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
    finally:
        await stores.aclose()
    assert taken == [] and seen == []


async def test_task_accepted_before_a_crash_is_recovered(
    monkeypatch, tmp_path
):
    """Инстанс принял задачу (сообщение во входящем) и умер до захвата
    треда: тред помечен задачей вебхука той же записью, что и сообщение, —
    восстановление другого инстанса его поднимает."""
    from gc_server.webhook.surface import make_user_message

    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    dying = await _surface(tmp_path, stores)

    async def crash(*_args, **_kwargs):
        raise RuntimeError("процесс умер")

    monkeypatch.setattr(dying, "_start_run", crash)
    survivor = await _surface(tmp_path, stores)
    try:
        with pytest.raises(RuntimeError):
            await dying.asubmit(
                KEY, make_user_message("принято"), op_id=None, trace_id=None
            )
        taken = await arecover_once(survivor, now=utc_now())
        await _settled(survivor)
    finally:
        await stores.aclose()
    assert taken == [KEY]
    assert _mentions(seen[0], "принято")


async def test_recovery_switch(tmp_path):
    stores = await aopen_process_stores(_settings(tmp_path))
    off = await _surface(tmp_path, stores, recovery={"enabled": False})
    on = await _surface(
        tmp_path, stores, recovery={"enabled": True, "interval_seconds": 60}
    )
    try:
        await off.astart()
        await on.astart()
        assert off.recovery_task is None
        assert on.recovery_task is not None
    finally:
        await off.astop()
        await on.astop()
        await stores.aclose()
    assert on.recovery_task is None


async def test_interruption_is_reported_by_callback(monkeypatch, tmp_path):
    _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    got: list = []
    surface.callbacks.config = surface.config.callbacks.model_copy(
        update={"url": "https://hooks.example/done"}
    )
    surface.config = surface.config.model_copy(
        update={"callbacks": surface.callbacks.config}
    )
    surface.callbacks.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: got.append(json.loads(r.content)) or httpx.Response(200)
        )
    )
    try:
        await _crashed_run(stores.threads)
        await arecover_once(surface, now=utc_now())
        await _settled(surface)
        await surface.callbacks.adrain()
    finally:
        await stores.aclose()
    assert [e["type"] for e in got] == ["task.interrupted", "task.completed"]


async def _failed_with_pending(store, key=KEY, *, upto):
    """Ход упал до доставки: во входящем — сообщение, статус финальный."""
    threads = Threads(store)
    await threads.submit(key, {"id": "u1", "role": "user", "content": "x"})
    await task_status.aset_status(
        threads, key, status="failed", run_id="dead:1", inbox_upto=upto
    )


async def test_failed_task_with_old_undelivered_is_not_retaken(
    monkeypatch, tmp_path
):
    """Находка ревью №4: упавшая задача со старым недоставленным
    сообщением не поднимается восстановлением по кругу; новое сообщение
    после финального статуса — поднимает."""
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _failed_with_pending(stores.threads, upto=1)
        assert await arecover_once(surface, now=utc_now()) == []
        await Threads(stores.threads).submit(
            KEY, {"id": "u2", "role": "user", "content": "новое"}
        )
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
    finally:
        await stores.aclose()
    assert taken == [KEY]
    assert len(seen) == 1


async def test_failed_run_records_its_inbox_boundary(monkeypatch, tmp_path):
    """Прогон упал — статус помнит, до какого входящего задача сдалась."""
    from gc_server.webhook.registry import ActiveRun

    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        threads = Threads(stores.threads)
        await threads.submit(KEY, {"id": "u1", "role": "user", "content": "x"})
        await task_status.aset_status(
            threads, KEY, status="running", run_id="r1"
        )
        run = ActiveRun(
            key="k",
            system=KEY.system,
            task_id=KEY.thread,
            executor=None,
            started_at=0.0,
            notes={"run_id": "r1"},
        )
        run.ending, run.error = "failed", RuntimeError("модель недоступна")
        await surface._amark_finished(run)
        status = task_status.status_of(await stores.threads.get_thread(KEY))
    finally:
        await stores.aclose()
    assert status["status"] == "failed" and status["inbox_upto"] == 1


async def test_crash_loop_ends_with_recovery_exhausted(monkeypatch, tmp_path):
    """Процесс падает посреди хода раз за разом: после трёх попыток
    восстановления подряд задача — failed с причиной, без нового хода."""
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        await task_status.aset_status(
            Threads(stores.threads), KEY, recovery_attempts=3
        )
        taken = await arecover_once(surface, now=utc_now())
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    status = task_status.status_of(record)
    assert taken == [] and seen == []
    assert status["status"] == "failed"
    assert status["error"] == "recovery_exhausted"


async def test_recovery_counts_its_attempts(monkeypatch, tmp_path):
    _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    attempts: list = []
    real = task_status.aset_status

    async def spy(threads, key, **fields):
        if "recovery_attempts" in fields:
            attempts.append(fields["recovery_attempts"])
        await real(threads, key, **fields)

    monkeypatch.setattr(task_status, "aset_status", spy)
    try:
        await _crashed_run(stores.threads)
        await arecover_once(surface, now=utc_now())
        await _settled(surface)
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    assert attempts[0] == 1  # попытка учтена до хода
    assert task_status.status_of(record)["recovery_attempts"] == 0


async def test_own_live_task_gets_no_interruption(monkeypatch, tmp_path):
    """Находка ревью №7: задачу ведёт (строит) этот же процесс — обход не
    пишет в неё «ход прерван» и не меняет статус."""
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        assert surface.registry.reserve(system=KEY.system, task_id=KEY.thread)
        taken = await arecover_once(surface, now=utc_now())
        record = await stores.threads.get_thread(KEY)
        inbox = await stores.threads.read_stream(KEY, "inbox")
    finally:
        await stores.aclose()
    assert taken == [] and seen == []
    assert inbox == []
    assert task_status.status_of(record)["status"] == "running"


async def test_server_stop_interrupts_instead_of_cancelling(
    monkeypatch, tmp_path
):
    """Остановка сервера — прерывание: статус остаётся running, отметка хода
    стоит, колбэка нет; после рестарта задачу продолжает восстановление."""
    from gc_core.utils import connect_backend

    entered = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(transport=httpx.MockTransport(hang)),
    )
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    events: list = []
    surface.notify = lambda key, status: events.append(status["status"])
    try:
        await surface.asubmit(
            KEY,
            {"id": "u1", "role": "user", "content": "долгая"},
            op_id=None,
            trace_id=None,
        )
        await asyncio.wait_for(entered.wait(), 10)
        await surface.astop()
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    assert task_status.status_of(record)["status"] == "running"
    assert record.runtime.turn == "running"
    assert events == []


async def test_cancel_of_a_crashed_task_finishes_it(monkeypatch, tmp_path):
    """Инстанс упал посреди хода, задачу отменили: отмена — сразу итог
    ``cancelled`` (вести ход некому), восстановление её не поднимает."""
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        assert await surface.acancel(KEY) == {"status": "cancelled"}
        taken = await arecover_once(surface, now=utc_now())
        await _settled(surface)
        record = await stores.threads.get_thread(KEY)
        status = await surface.astatus(KEY)
    finally:
        await stores.aclose()
    assert taken == []
    assert seen == []
    assert status["status"] == "cancelled"
    assert record.runtime.turn is None


async def _cancel_during_build_elsewhere(monkeypatch, tmp_path, *, done):
    """Инстанс A строит ход задачи (сборка контекста держится), отмена
    приходит на инстанс B. ``done`` — у задачи уже есть завершённый прогон."""
    from gc_server.webhook import surface as surface_module

    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    a = await _surface(tmp_path, stores)
    b = await _surface(tmp_path, stores)
    try:
        if done:
            await a.asubmit(KEY, {"text": "первое"}, op_id=None, trace_id=None)
            await _settled(a)
            assert (await a.astatus(KEY))["status"] == "completed"
            seen.clear()
        entered, gate = asyncio.Event(), asyncio.Event()
        real = surface_module.acreate_run_context

        async def slow_build(*args, **kwargs):
            entered.set()
            await gate.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(surface_module, "acreate_run_context", slow_build)
        submit = asyncio.create_task(
            a.asubmit(KEY, {"text": "второе"}, op_id=None, trace_id=None)
        )
        await asyncio.wait_for(entered.wait(), 5)
        reply = await b.acancel(KEY)
        gate.set()
        await submit
        await _settled(a, b)
        status = await b.astatus(KEY)
    finally:
        await stores.aclose()
    return reply, status, seen


async def test_cancel_on_another_instance_during_build_of_a_new_task(
    monkeypatch, tmp_path
):
    """Ревью, раунд 2 (R4): статуса нового прогона в хранилище ещё нет — но
    сообщение ждёт во входящем потоке: отмена записывается, ход её видит."""
    reply, status, seen = await _cancel_during_build_elsewhere(
        monkeypatch, tmp_path, done=False
    )
    assert reply == {"status": "cancelling"}
    assert status["status"] == "cancelled"
    assert seen == [], "отменённый ход дошёл до модели"


async def test_cancel_on_another_instance_during_build_after_a_finished_run(
    monkeypatch, tmp_path
):
    """Статус в хранилище — прошлого прогона (completed), новое сообщение ещё
    не доставлено: отмена касается его хода, а не отвечает старым итогом."""
    reply, status, seen = await _cancel_during_build_elsewhere(
        monkeypatch, tmp_path, done=True
    )
    assert reply == {"status": "cancelling"}
    assert status["status"] == "cancelled"
    assert seen == []


async def test_server_stops_do_not_exhaust_recovery(monkeypatch, tmp_path):
    """Ревью, раунд 2 (R5): четыре штатные остановки посреди одной долгой
    задачи (деплои) — не сбои хода: восстановление каждый раз продолжает
    её, а не сдаётся с ``recovery_exhausted``."""
    from gc_core.utils import connect_backend

    entered = asyncio.Event()
    hanging = [True]

    async def handler(request: httpx.Request) -> httpx.Response:
        if hanging[0]:
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=scripted_model.FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    stores = await aopen_process_stores(_settings(tmp_path))
    try:
        surface = await _surface(tmp_path, stores)
        await surface.asubmit(
            KEY,
            {"id": "u1", "role": "user", "content": "долгая"},
            op_id=None,
            trace_id=None,
        )
        for _ in range(4):
            await asyncio.wait_for(entered.wait(), 10)
            entered.clear()
            await surface.astop()
            surface = await _surface(tmp_path, stores)
            assert await arecover_once(surface, now=utc_now()) == [KEY]
        await asyncio.wait_for(entered.wait(), 10)
        hanging[0] = False
        await surface.astop()
        surface = await _surface(tmp_path, stores)
        await arecover_once(surface, now=utc_now())
        await _settled(surface)
        status = await surface.astatus(KEY)
    finally:
        await stores.aclose()
    assert status["status"] == "completed", status


async def test_cancel_while_the_run_writes_its_result(monkeypatch, tmp_path):
    """Ревью, раунд 2 (R13): прогон снят из реестра, но итог ещё пишется —
    отмена в этом окне не закрывает задачу «брошенной»: итог один, колбэк
    один."""
    _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    events: list = []
    surface.notify = lambda key, status: events.append(status["status"])
    entered, gate = asyncio.Event(), asyncio.Event()
    real = surface._amark_finished

    async def slow_finish(run):
        entered.set()
        await gate.wait()
        await real(run)

    surface._amark_finished = slow_finish
    try:
        await surface.asubmit(KEY, {"text": "x"}, op_id=None, trace_id=None)
        await asyncio.wait_for(entered.wait(), 10)
        reply = await surface.acancel(KEY)
        gate.set()
        await _settled(surface)
        status = await surface.astatus(KEY)
    finally:
        await stores.aclose()
    assert reply == {"status": "cancelling"}
    assert status["status"] == "completed"
    assert events == ["completed"]


async def test_cancelled_run_records_its_inbox_boundary(monkeypatch, tmp_path):
    """Ревью, раунд 2 (R14): отменённый прогон, как и упавший, помнит
    границу входящих — восстановление не собирает ход ради сообщения,
    которое отмена уже сняла (и второго колбэка ``cancelled`` нет)."""
    from gc_server.webhook.registry import ActiveRun

    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        threads = Threads(stores.threads)
        await threads.submit(KEY, {"id": "u1", "role": "user", "content": "x"})
        await task_status.aset_status(
            threads, KEY, status="running", run_id="r1"
        )
        run = ActiveRun(
            key="k",
            system=KEY.system,
            task_id=KEY.thread,
            executor=None,
            started_at=0.0,
            notes={"run_id": "r1"},
        )
        run.ending = "cancelled"
        await surface._amark_finished(run)
        status = task_status.status_of(await stores.threads.get_thread(KEY))
        taken = await arecover_once(surface, now=utc_now())
    finally:
        await stores.aclose()
    assert status["status"] == "cancelled" and status["inbox_upto"] == 1
    assert taken == [] and seen == []


async def test_cancel_whose_stop_does_not_settle_still_cancels(
    monkeypatch, tmp_path
):
    """Слияние develop: ``registry.stop`` ждёт, пока работа хода снята, с
    потолком. Не уложилось — отмена всё равно ложится в поток ``control``,
    ответ — «отменяется»: не ошибка и не успех."""
    from gc_core.threads.models import CONTROL
    from gc_core.user_stop import UserStop
    from gc_server.webhook.registry import ActiveRun

    monkeypatch.setattr("gc_core.user_stop._SETTLE_SEC", 0.05)
    _install(monkeypatch)
    gate = UserStop()
    gate._settled.clear()
    stopped: list = []

    class _Executor:
        user_stop = gate

        def stop_run(self) -> None:
            stopped.append(True)

    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        threads = Threads(stores.threads)
        await task_status.asubmit(
            threads, KEY, {"id": "u1", "role": "user", "content": "x"}
        )
        await task_status.aset_status(
            threads, KEY, status="running", run_id="r1"
        )
        surface.registry._runs["generic:t"] = ActiveRun(
            key="generic:t",
            system=KEY.system,
            task_id=KEY.thread,
            executor=_Executor(),
            started_at=0.0,
        )
        reply = await surface.acancel(KEY)
        control = await stores.threads.read_stream(KEY, CONTROL)
    finally:
        surface.registry._runs.clear()
        await stores.aclose()
    assert reply == {"status": "cancelling"}
    assert stopped == [True]
    assert [entry.data["type"] for entry in control] == ["cancel"]


async def test_task_failing_right_after_claim_is_not_recovered_again(
    monkeypatch, tmp_path
):
    """Ревью, раунд 4: ход, упавший сразу после захвата треда (проверка
    состояния треда), оставлял отметку хода и аренду — восстановление
    поднимало упавшую задачу каждый обход, без предела попыток."""
    seen = _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        await _crashed_run(stores.threads)
        # Тред прежней версии: устаревший ключ памяти — его отвергает
        # проверка состояния уже после захвата треда.
        writer = SessionWriter(stores.threads, KEY, owner="seed/1/legacy")
        await writer.aclaim()
        await writer.acommit(set_extra={"memory_files": ["x.md"]})
        await writer.aside(set_runtime=RuntimeStatus(turn="running"))
        await writer.arelease()
        first = await arecover_once(surface, now=utc_now())
        await _settled(surface)
        again = await arecover_once(surface, now=utc_now())
        await _settled(surface)
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    assert first == [KEY]
    assert again == []
    assert task_status.status_of(record)["status"] == "failed"
    assert record.runtime.turn != "running"
    assert seen == []


def _record_callbacks(surfaces) -> list:
    events: list = []
    for surface in surfaces:
        surface.notify = lambda key, status: events.append(
            (status["status"], status.get("error"))
        )
    return events


async def test_one_crash_is_one_attempt_across_instances(
    monkeypatch, tmp_path
):
    """Ревью, раунд 4: попытка писалась до захвата треда, и каждый
    инстанс, чей обход попал в окно сборки соседа, прибавлял свою — одно
    падение давало ``recovery_exhausted`` и повторный ``interrupted``."""
    from gc_server.webhook import surface as surface_mod

    _install(monkeypatch)
    gate = asyncio.Event()
    real_ctx = surface_mod.acreate_run_context

    async def slow_ctx(*args, **kwargs):
        await gate.wait()  # сборка контекста (MCP и т. п.) идёт долго
        return await real_ctx(*args, **kwargs)

    monkeypatch.setattr(surface_mod, "acreate_run_context", slow_ctx)
    stores = await aopen_process_stores(_settings(tmp_path))
    surfaces = [await _surface(tmp_path, stores) for _ in range(4)]
    events = _record_callbacks(surfaces)
    try:
        await _crashed_run(stores.threads)
        walks = []
        for surface in surfaces:
            walks.append(
                asyncio.create_task(arecover_once(surface, now=utc_now()))
            )
            await asyncio.sleep(0.2)
        gate.set()
        taken = await asyncio.gather(*walks)
        await _settled(*surfaces)
        record = await stores.threads.get_thread(KEY)
    finally:
        await stores.aclose()
    assert sum(len(keys) for keys in taken) == 1
    status = task_status.status_of(record)
    assert status["status"] == "completed", status
    assert events == [("interrupted", None), ("completed", None)]


async def test_instances_reading_one_snapshot_elect_one_recovery(
    monkeypatch, tmp_path
):
    """Два инстанса увидели тот же снимок бесхозной задачи: продолжает
    один, попытка — одна, ``interrupted`` — один."""
    from gc_server.webhook import recovery

    _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surfaces = [await _surface(tmp_path, stores) for _ in range(2)]
    events = _record_callbacks(surfaces)
    try:
        await _crashed_run(stores.threads)
        threads = Threads(stores.threads)
        record = await threads.get(KEY)
        started = await asyncio.gather(
            *(
                recovery._acontinue(surface, threads, record)
                for surface in surfaces
            )
        )
        await _settled(*surfaces)
        status = task_status.status_of(await threads.get(KEY))
    finally:
        await stores.aclose()
    assert sorted(started) == [False, True]
    assert status["status"] == "completed", status
    assert events == [("interrupted", None), ("completed", None)]


async def test_late_run_on_a_drained_thread_keeps_the_final_status(
    monkeypatch, tmp_path
):
    """Прогон взял тред, когда всё уже доставлено (его довёл другой
    инстанс): статус ``running`` он не пишет — иначе задача навсегда
    осталась бы «идущей» без хода и аренды."""
    _install(monkeypatch)
    stores = await aopen_process_stores(_settings(tmp_path))
    surface = await _surface(tmp_path, stores)
    try:
        threads = Threads(stores.threads)
        await surface.asubmit(KEY, {"text": "x"}, op_id=None, trace_id=None)
        await _settled(surface)
        assert task_status.status_of(await threads.get(KEY))["status"] == (
            "completed"
        )
        await surface._start_run(KEY.system, KEY.thread, None)
        await _settled(surface)
        record = await threads.get(KEY)
    finally:
        await stores.aclose()
    assert task_status.status_of(record)["status"] == "completed"


async def test_accepted_task_has_a_status_on_every_instance(
    monkeypatch, tmp_path
):
    """Ревью, раунд 4: задачу, принятую во входящий поток, но ещё без
    статуса прогона, знал только строящий её инстанс — остальные за
    балансировщиком отвечали 404. Теперь — ``queued``."""
    from gc_server.webhook import surface as surface_mod

    _install(monkeypatch)
    gate = asyncio.Event()
    real_ctx = surface_mod.acreate_run_context

    async def slow_ctx(*args, **kwargs):
        await gate.wait()
        return await real_ctx(*args, **kwargs)

    monkeypatch.setattr(surface_mod, "acreate_run_context", slow_ctx)
    stores = await aopen_process_stores(_settings(tmp_path))
    builder = await _surface(tmp_path, stores)
    other = await _surface(tmp_path, stores)
    try:
        submit = asyncio.create_task(
            builder.asubmit(KEY, {"text": "x"}, op_id=None, trace_id=None)
        )
        for _ in range(200):
            if builder.registry.building_since(
                system=KEY.system, task_id=KEY.thread
            ):
                break
            await asyncio.sleep(0.01)
        seen = await other.astatus(KEY)
        cancelled = await other.acancel(KEY)
        gate.set()
        await submit
        await _settled(builder, other)
    finally:
        await stores.aclose()
    assert seen is not None
    assert seen["status"] == "queued"
    assert seen["pending_messages"] == 1
    assert cancelled == {"status": "cancelling"}
