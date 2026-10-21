"""Шаг 2: thread identity на child ctx + gate sidecar без sink."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    child_progress_enabled,
)
from gc_core.tools.subagents.sidecar import (
    bind_thread_identity,
    clear_thread_identity,
    resolve_sidecar_identity,
)


class TestResolveSidecarIdentityGate:
    def test_enabled_with_identity_and_parent_call_id_without_sink(self):
        ctx = SimpleNamespace(
            parent_call_id="call_sub_1",
            run_id="run_c1",
            threads_dir="/tmp/threads",
            system_id="cli",
            thread_id="task-1",
            subagent_progress_sink=None,
        )
        ident = resolve_sidecar_identity(ctx)
        assert ident is not None
        assert ident.threads_dir == "/tmp/threads"
        assert ident.system_id == "cli"
        assert ident.thread_id == "task-1"
        assert ident.parent_call_id == "call_sub_1"
        assert ident.child_run_id == "run_c1"
        assert not child_progress_enabled(ctx)

    def test_enabled_even_when_sink_present(self):
        sink = QueueSubagentProgressSink()
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id="t1",
            subagent_progress_sink=sink,
        )
        assert resolve_sidecar_identity(ctx) is not None
        assert child_progress_enabled(ctx)

    def test_disabled_without_parent_call_id(self):
        ctx = SimpleNamespace(
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id="t1",
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_enabled_without_threads_dir(self):
        """Каталога может не быть — у хранилища в базе его нет вовсе.

        Прежняя редакция требовала обратного, и требование было дефектом:
        у стороннего store ``threads_dir`` — пустая строка, гейт её отвергал,
        и трасса субагентов не писалась в нефайловое хранилище НИКОГДА. Все
        семь вызовов записи молча возвращали ``None``. Адрес треда — это
        ``(system_id, thread_id)``; каталог нужен лишь файловому запасному
        пути и требуется там.
        """
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            system_id="cli",
            thread_id="t1",
        )

        ident = resolve_sidecar_identity(ctx)

        assert ident is not None, "гейт снова требует каталог, которого нет"
        assert ident.threads_dir == ""
        assert (ident.system_id, ident.thread_id) == ("cli", "t1")

    def test_disabled_without_thread_id(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id=None,
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_disabled_on_invalid_thread_id(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id="bad id!",
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_sanitizes_system_id(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="CLI",
            thread_id="t1",
        )
        ident = resolve_sidecar_identity(ctx)
        assert ident is not None
        assert ident.system_id == "cli"


class TestBindClearThreadIdentity:
    def test_bind_and_clear(self):
        ctx = SimpleNamespace()
        bind_thread_identity(
            ctx, threads_dir="/threads", system_id="cli", thread_id="abc"
        )
        assert ctx.threads_dir == "/threads"
        assert ctx.system_id == "cli"
        assert ctx.thread_id == "abc"
        clear_thread_identity(ctx)
        assert ctx.threads_dir is None
        assert ctx.system_id is None
        assert ctx.thread_id is None


class TestChildCtxCopiesThreadIdentity:
    @pytest.mark.asyncio
    async def test_build_child_ctx_copies_identity_without_sink(
        self, tmp_path
    ):
        from gc_core.states import SharedStateData, State
        from gc_core.tools.subagents.runner import SubagentRunner
        from gc_testkit.helpers.tool_ctx import local_ctx

        state = State(
            verbose=False,
            model="m",
            tools_spec=[],
            tools_descr=[],
            skills_list=[],
            max_steps=10,
            system_prompt_id="p",
            use_server_subagents=False,
            chat_completions_system_prompt="s",
            shared=SharedStateData(),
        )
        state.backend_api_mode = "chat_completions"

        cfg = {
            "logging.save_logs": True,
            "paths.logs_dump_dir": str(tmp_path),
            "agent.use_local_subagents": True,
            "backend.api_mode": "chat_completions",
            "paths.skills_data_path": "",
            "agent.model": "m",
            "agent.system_prompt_id": "p",
            "chat_completions": {
                "auxiliary_llm": {"max_tokens": 128, "completion_kwargs": {}},
            },
        }
        parent = local_ctx(cfg)
        parent.run_id = "p1"
        parent.auth_connector = None
        parent.logger = None
        parent.logging_path = ""
        parent.summary_trace_path = ""
        parent.tools_data = {}
        parent.tools_spec = []
        parent.tools_description = []
        parent.subagent_progress_sink = None
        bind_thread_identity(
            parent,
            threads_dir=str(tmp_path / "threads"),
            system_id="cli",
            thread_id="task-9",
        )
        runner = SubagentRunner(parent, state, cfg)
        runner._parent_call_id = "call_sub"
        child = await runner._build_child_ctx(tool_name="subagent_worker")

        assert child.threads_dir == str(tmp_path / "threads")
        assert child.system_id == "cli"
        assert child.thread_id == "task-9"
        assert child.parent_call_id == "call_sub"
        assert getattr(child, "subagent_progress_sink", None) is None
        ident = resolve_sidecar_identity(child)
        assert ident is not None
        assert ident.parent_call_id == "call_sub"
        assert not child_progress_enabled(child)


# --- N5: адрес треда не зависит от каталога ---------------------------------


async def _child_ctx_of_parent_with(tmp_path, *, threads_dir: str):
    """Настоящий `_build_child_ctx` над родителем с заданным каталогом тредов."""
    from gc_core.states import SharedStateData, State
    from gc_core.tools.subagents.runner import SubagentRunner
    from gc_testkit.helpers.tool_ctx import local_ctx

    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="p",
        use_server_subagents=False,
        chat_completions_system_prompt="s",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"

    cfg = {
        "logging.save_logs": True,
        "paths.logs_dump_dir": str(tmp_path),
        "agent.use_local_subagents": True,
        "backend.api_mode": "chat_completions",
        "paths.skills_data_path": "",
        "agent.model": "m",
        "agent.system_prompt_id": "p",
        "chat_completions": {
            "auxiliary_llm": {"max_tokens": 128, "completion_kwargs": {}},
        },
    }
    parent = local_ctx(cfg)
    parent.run_id = "p1"
    parent.auth_connector = None
    parent.logger = None
    parent.logging_path = ""
    parent.summary_trace_path = ""
    parent.tools_data = {}
    parent.tools_spec = []
    parent.tools_description = []
    parent.subagent_progress_sink = None
    bind_thread_identity(
        parent, threads_dir=threads_dir, system_id="cli", thread_id="task-9"
    )
    runner = SubagentRunner(parent, state, cfg)
    runner._parent_call_id = "call_sub"
    return await runner._build_child_ctx(tool_name="subagent_worker")


async def test_child_keeps_the_thread_address_without_a_directory(tmp_path):
    """У встраивающего с базой каталога нет — адрес треда обязан уцелеть.

    Гейт `resolve_sidecar_identity` пустой каталог уже принимает, но до него
    дело не доходило: `_build_child_ctx` требовал непустой `threads_dir`, иначе
    звал `clear_thread_identity`. Ребёнок оставался без адреса, и все записи
    трассы молча возвращали `None` — при полностью работающих субагентах.
    """
    child = await _child_ctx_of_parent_with(tmp_path, threads_dir="")

    assert (child.system_id, child.thread_id) == ("cli", "task-9"), (
        f"адрес треда стёрт из-за пустого каталога: {(child.system_id, child.thread_id)}"
    )
    assert resolve_sidecar_identity(child) is not None, (
        "identity не собралась — трасса субагента не будет записана"
    )


async def test_child_still_loses_the_address_without_a_thread(tmp_path):
    """Обратная половина: нет самого адреса — личность снимается, как и раньше."""
    child = await _child_ctx_of_parent_with(
        tmp_path, threads_dir=str(tmp_path)
    )
    child.thread_id = None

    assert resolve_sidecar_identity(child) is None


async def test_no_store_and_no_directory_writes_nowhere(tmp_path, monkeypatch):
    """Ни хранилища, ни каталога — тихий no-op, а не запись в текущий каталог.

    `FileThreadStore("")` уводит запись в путь ОТНОСИТЕЛЬНО cwd процесса: та же
    молчаливая запись мимо контракта, от которой уже уходили в компакции.
    """
    from gc_core.tools.subagents.sidecar import apersist_subagent_trace

    monkeypatch.chdir(tmp_path)
    ctx = SimpleNamespace(
        parent_call_id="call-1",
        run_id="child-1",
        system_id="cli",
        thread_id="t1",
        thread_store=None,
        threads_dir="",
    )

    written = await apersist_subagent_trace(
        ctx, kind="child_start", payload={"x": 1}
    )

    assert written is None, "запись состоялась, хотя писать некуда"
    assert list(tmp_path.iterdir()) == [], (
        f"трасса ушла в текущий каталог процесса: {list(tmp_path.iterdir())}"
    )


# --- H7: трасса доезжает до нефайлового хранилища --------------------------


async def test_trace_reaches_a_store_without_files():
    """Сквозная проверка: запись доходит до стороннего хранилища.

    Гейт требовал непустой `threads_dir`, а у хранилища в базе или в памяти
    его нет. Identity не собиралась, и все семь вызовов записи молча
    возвращали `None`: субагенты работали, трасса была пуста. Прежние тесты
    этого не видели, потому что подставляли `threads_dir=str(tmp_path)`.

    Здесь каталога нет намеренно — как у встраивающего с базой.
    """
    from types import SimpleNamespace

    from gc_core.threads.facade import Threads
    from gc_core.threads.models import ThreadKey
    from gc_core.tools.subagents.sidecar import apersist_subagent_trace
    from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

    store = MemoryThreadStore()
    ctx = SimpleNamespace(
        parent_call_id="call-1",
        run_id="child-1",
        system_id="cli",
        thread_id="t1",
        thread_store=store,
        threads_dir=None,  # хранилище без файлов
    )

    written = await apersist_subagent_trace(
        ctx, kind="child_start", payload={"x": 1}
    )

    assert written is not None, (
        "запись не состоялась — гейт снова требует каталог"
    )
    events = await Threads(store).subagent_events(
        ThreadKey(system="cli", thread="t1")
    )
    trace = [entry.data for entry in events]
    assert len(trace) == 1, trace
    assert trace[0]["parent_call_id"] == "call-1", trace[0]
