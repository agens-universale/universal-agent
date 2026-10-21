"""Active-scope user input: prose → executing child, else parent (spec)."""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.states import SharedStateData, State
from gc_testkit.develop_files import read_subagent_trace
from gc_core.tools.subagents.active_scope import (
    activate_child_input,
    active_parent_call_id,
    is_child_input_active,
    try_enqueue_user_message,
)
from gc_core.execution.progress import QueueSubagentProgressSink
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit.settings import fake_ctx

# Уборщика глобали больше нет: скоуп живёт в контексте прогона и не может
# пережить тест — переживать негде.


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System\n<specific_role>x</specific_role>",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_config(**overrides):
    cfg = {
        "agent.use_local_subagents": True,
        "backend.api_mode": "chat_completions",
        "paths.skills_data_path": "",
        "agent.model": "test-model",
        "agent.system_prompt_id": "test-prompt",
        "agent.subagent_max_steps": 30,
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(cfg=None, **extra):
    ns = fake_ctx(
        cfg or _make_config(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
        subagent_progress_sink=None,
        threads_dir=None,
        system_id=None,
        thread_id=None,
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


class TestActiveScopeRegistry:
    def test_inactive_try_enqueue_false(self):
        ctx = SimpleNamespace()
        assert (
            try_enqueue_user_message(ctx, {"role": "user", "content": "hi"})
            is False
        )
        assert is_child_input_active(ctx) is False
        assert active_parent_call_id(ctx) is None

    def test_activate_enqueues_and_drains(self):
        ctx = SimpleNamespace()
        with activate_child_input(ctx, parent_call_id="call_sub_1") as scope:
            assert is_child_input_active(ctx) is True
            assert active_parent_call_id(ctx) == "call_sub_1"
            assert (
                try_enqueue_user_message(
                    ctx, {"role": "user", "content": "steer"}
                )
                is True
            )
            assert scope.drain() == [{"role": "user", "content": "steer"}]
            assert scope.drain() == []
        assert is_child_input_active(ctx) is False

    def test_on_enqueue_callback(self):
        seen: list = []
        ctx = SimpleNamespace()
        with activate_child_input(ctx, parent_call_id="p1") as scope:
            scope.set_on_enqueue(lambda m: seen.append(m["content"]))
            scope.enqueue({"role": "user", "content": "x"})
        assert seen == ["x"]


class TestAgentExecutorRoutesToChild:
    def test_add_message_goes_to_child_when_active(self):
        # Активатор берём У ТОГО ЖЕ МОДУЛЯ, что и исполнитель.
        #
        # Конфтесты подменяют `sys.modules["gc_core.tools"]` заглушкой и
        # возвращают обратно; при неудачном порядке сборки
        # `subagents.active_scope` оказывается загружен ДВАЖДЫ. Тогда у теста
        # и у `agent_executor` разные `ContextVar`: исполнитель пишет в один,
        # тест читает другой, и сообщение «теряется» на ровном месте. Причём
        # устареть может ЛЮБАЯ из сторон — зависит от порядка сборки.
        #
        # Поэтому берём активатор из ОКРУЖЕНИЯ той функции, которую держит
        # исполнитель. Не через `sys.modules`: выброшенный модуль оттуда уже
        # удалён, и поиск по имени падает с `KeyError`, — а `__globals__`
        # живут вместе с самой функцией.
        import gc_core.agent_executor as _ae
        from gc_core.agent_executor import AgentExecutor

        _activate = _ae.try_enqueue_user_message.__globals__[
            "activate_child_input"
        ]

        ctx = MagicMock()
        # У MagicMock любой атрибут «есть», поэтому пустой скоуп задаём явно:
        # иначе адресат окажется мок-объектом, а не None.
        ctx.child_input_scope = None
        ex = AgentExecutor(ctx)
        with _activate(ctx, parent_call_id="call_sub_1") as scope:
            ex.add_message({"role": "user", "content": "to child", "id": "m1"})
            assert scope.drain()[0]["content"] == "to child"
            with ex._lock:
                assert ex._pending_messages == []

    def test_add_message_goes_to_parent_when_no_child(self):
        from gc_core.agent_executor import AgentExecutor

        ctx = MagicMock()
        ctx.child_input_scope = None
        ex = AgentExecutor(ctx)
        ex.add_message({"role": "user", "content": "to parent"})
        with ex._lock:
            assert len(ex._pending_messages) == 1
            assert ex._pending_messages[0]["content"] == "to parent"


class TestRunnerWiresPending:
    def test_passes_get_pending_and_is_active_during_run(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured = {}
        barrier = threading.Event()
        release = threading.Event()

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured["get_pending"] = kwargs.get("get_pending_messages")
            captured["active_during"] = is_child_input_active(ctx)
            captured["parent_call_id"] = active_parent_call_id(ctx)
            barrier.set()
            release.wait(timeout=2)
            pending = kwargs["get_pending_messages"]()
            captured["drained"] = pending
            return "done"

        runner = SubagentRunner(ctx, orch)
        runner._parent_call_id = "call_parent_9"

        def _run():
            with patch.object(
                runner,
                "_build_child_ctx",
                return_value=SimpleNamespace(
                    run_id="run_c1",
                    parent_call_id="call_parent_9",
                    subagent_progress_sink=None,
                    logger=None,
                ),
            ):
                # Цель потока: loop'а здесь нет, поэтому свой — как у точки
                # входа `main`. Синхронной формы у прогона субагента больше нет.
                asyncio.run(
                    runner.arun(
                        "subagent_worker",
                        {"task": "test"},
                        fake_run_agent,
                        parent_call_id="call_parent_9",
                    )
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert barrier.wait(timeout=2)
        assert captured["active_during"] is True
        assert captured["parent_call_id"] == "call_parent_9"
        assert callable(captured["get_pending"])
        try_enqueue_user_message(
            ctx, {"role": "user", "content": "look at auth", "id": "m_steer"}
        )
        release.set()
        t.join(timeout=2)
        assert captured["drained"][0]["content"] == "look at auth"
        assert is_child_input_active(ctx) is False

    def test_drained_message_images_are_moved_into_the_thread(self, tmp_path):
        """Реплика ребёнку посреди его работы — такой же приём из очереди, как у
        родителя: картинки из кармана процесса переезжают в тред (адрес — родителя),
        иначе после выхода из TUI карман снесён и в треде остаётся мёртвый путь."""
        import io

        from PIL import Image

        from gc_core.attachments.model import ORIGIN_USER, user_label
        from gc_core.attachments.store import ImageStore, amake_attachment

        store = ImageStore.for_threads_dir(str(tmp_path))
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
        staged = asyncio.run(
            amake_attachment(
                store,
                buf.getvalue(),
                origin=ORIGIN_USER,
                label=user_label(1),
            )
        )
        orch = _make_orchestrator_state()
        cfg = _make_config()
        ctx = _make_ctx(cfg)
        captured = {}
        barrier = threading.Event()
        release = threading.Event()

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            barrier.set()
            release.wait(timeout=2)
            captured["drained"] = kwargs["get_pending_messages"]()
            return "done"

        runner = SubagentRunner(ctx, orch, cfg)
        child_ctx = SimpleNamespace(
            run_id="run_c1",
            parent_call_id="call_parent_9",
            subagent_progress_sink=None,
            logger=None,
            image_store=store,
            system_id="cli",
            thread_id="t" * 32,
        )

        def _run():
            with patch.object(
                runner, "_build_child_ctx", return_value=child_ctx
            ):
                asyncio.run(
                    runner.arun(
                        "subagent_worker",
                        {"task": "test"},
                        fake_run_agent,
                        parent_call_id="call_parent_9",
                    )
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert barrier.wait(timeout=2)
        try_enqueue_user_message(
            ctx,
            {
                "role": "user",
                "content": "[Изображение #1] вот",
                "attachments": [staged.to_dict()],
            },
        )
        release.set()
        t.join(timeout=2)
        [att] = captured["drained"][0]["attachments"]
        assert att["path"] != staged.path
        assert not att["path"].startswith(store.staging_dir)
        assert os.path.isfile(att["path"])

    def test_enqueue_emits_progress_and_sidecar_user(self, tmp_path):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        sink = QueueSubagentProgressSink()
        ctx = _make_ctx(cfg, subagent_progress_sink=sink)
        barrier = threading.Event()
        release = threading.Event()

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            barrier.set()
            release.wait(timeout=2)
            return "done"

        child_ctx = SimpleNamespace(
            run_id="run_c1",
            parent_call_id="call_sub_steer",
            subagent_progress_sink=sink,
            logger=None,
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id="task-steer",
        )
        runner = SubagentRunner(ctx, orch)

        def _run():
            with patch.object(
                runner, "_build_child_ctx", return_value=child_ctx
            ):
                asyncio.run(
                    runner.arun(
                        "subagent_worker",
                        {"task": "test"},
                        fake_run_agent,
                        parent_call_id="call_sub_steer",
                    )
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert barrier.wait(timeout=5)
        try_enqueue_user_message(
            ctx, {"role": "user", "content": "check auth next", "id": "m_u1"}
        )
        # technical must enqueue but not emit nested user row
        try_enqueue_user_message(
            ctx,
            {
                "role": "user",
                "content": wrap_technical_user_content("attachment meta"),
            },
        )
        events = sink.drain()
        user_evs = [e for e in events if e.kind == "user_message"]
        assert len(user_evs) == 1
        assert user_evs[0].prose == "check auth next"
        assert user_evs[0].parent_call_id == "call_sub_steer"

        release.set()
        t.join(timeout=5)

        # Запись трассы уехала на loop прогона (см. `SubagentRunner`: колбэк
        # enqueue приходит из чужого потока и своей синхронной формы у операции
        # больше нет), поэтому смотрим её ПОСЛЕ того, как прогон закончился.
        # Здесь `fake_run_agent` держит loop в `release.wait`, то есть до
        # `release.set()` очередь loop'а не двигалась вовсе; в проде на его
        # месте `arun_agent`, который отдаёт управление на каждом await.
        rows = read_subagent_trace(str(tmp_path), "cli", "task-steer")
        user_rows = [r for r in rows if r.get("kind") == "user"]
        assert len(user_rows) == 1
        assert user_rows[0]["payload"]["content"] == "check auth next"


class TestParentPendingUnchangedWithoutChild:
    def test_enqueued_images_reach_progress_and_sidecar_from_the_thread(
        self, tmp_path
    ):
        """Картинки реплики ребёнку — как у ответа на его вопрос: строка в ленте
        вживую (событие — путь кармана, он живёт до выхода) и после перезапуска
        (запись ленты субагента — путь в треде). Копия в тред — в цикле ребёнка, не в
        потоке, поставившем реплику в очередь (UI TUI) (смоук и ревью 2026-10-01)."""
        import io

        from PIL import Image

        from gc_core.attachments.model import ORIGIN_USER, user_label
        from gc_core.attachments.store import ImageStore, amake_attachment

        store = ImageStore.for_threads_dir(str(tmp_path))
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), "blue").save(buf, format="PNG")
        staged = asyncio.run(
            amake_attachment(
                store,
                buf.getvalue(),
                origin=ORIGIN_USER,
                label=user_label(1),
            )
        )
        orch = _make_orchestrator_state()
        cfg = _make_config()
        sink = QueueSubagentProgressSink()
        ctx = _make_ctx(cfg, subagent_progress_sink=sink)
        barrier = threading.Event()
        release = threading.Event()

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            barrier.set()
            release.wait(timeout=2)
            return "done"

        child_ctx = SimpleNamespace(
            run_id="run_c1",
            parent_call_id="call_sub_img",
            subagent_progress_sink=sink,
            logger=None,
            threads_dir=str(tmp_path),
            image_store=store,
            system_id="cli",
            thread_id="task-img",
        )
        runner = SubagentRunner(ctx, orch, cfg)

        def _run():
            with patch.object(
                runner, "_build_child_ctx", return_value=child_ctx
            ):
                asyncio.run(
                    runner.arun(
                        "subagent_worker",
                        {"task": "test"},
                        fake_run_agent,
                        parent_call_id="call_sub_img",
                    )
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert barrier.wait(timeout=5)
        try_enqueue_user_message(
            ctx,
            {
                "role": "user",
                "content": "[Изображение #1] что тут?",
                "id": "m_img",
                "attachments": [staged.to_dict()],
            },
        )
        [event] = [e for e in sink.drain() if e.kind == "user_message"]
        assert event.attachments == [staged.to_dict()]
        # поток постановки в очередь файлов не пишет
        assert not [
            f for f in os.listdir(store.root) if f.startswith("cli_task-img_")
        ]

        release.set()
        t.join(timeout=5)
        [row] = [
            r
            for r in read_subagent_trace(str(tmp_path), "cli", "task-img")
            if r.get("kind") == "user"
        ]
        [kept] = row["payload"]["attachments"]
        assert kept["id"] == staged.id
        assert not kept["path"].startswith(store.staging_dir)
        assert os.path.isfile(kept["path"])

    def test_technical_only_parent_path(self):
        """Regression: no active child → parent pending as before."""
        from gc_core.agent_executor import AgentExecutor

        ctx = MagicMock()
        # Скоуп задаём явно: у MagicMock «есть» любой атрибут, и без этой
        # строки адресатом окажется мок-объект, а не отсутствие субагента.
        ctx.child_input_scope = None
        ex = AgentExecutor(ctx)
        msg = {"role": "user", "content": "hello"}
        ex.add_message(msg)
        with ex._lock:
            assert ex._pending_messages == [msg]
