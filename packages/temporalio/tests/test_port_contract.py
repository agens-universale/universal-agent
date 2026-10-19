"""Защита от забывчивости: новый ввод-вывод ядра не проходит мимо Temporal.

Ядро находит порты и данные контекста через ``getattr(ctx, "<имя>", None)``
или ``port(ctx, "<имя>")`` и, не найдя порта, делает работу на месте
(``port`` в durable-исполнении вместо этого роняет ход). Под Temporal «на месте» — код
workflow: файл, сеть или процесс там либо падают, либо тихо выключают
функцию. Поэтому каждое такое имя ядра здесь — с решением для Temporal.
Новое имя роняет тест: привяжите порт в ``context.py`` (или в транспорте
субагентов) либо впишите имя сюда с причиной, почему он не нужен.

Тесты без Temporal CLI: идут в общем прогоне всегда.
"""

import ast
import inspect
from pathlib import Path

import gc_core
from gc_core.context_preparation import RunContextBindings
from gc_core.execution.ports import AgentOperations
from gc_core_temporalio import context as temporal_context
from gc_core_temporalio.operations import WorkflowOperations
from live_support import worker_kwargs

CORE = Path(gc_core.__file__).parent
PACKAGE = Path(temporal_context.__file__).parent

#: Порт привязан в Temporal: workflow вызывает activity (или свою реализацию).
BOUND = "порт: привязан"
#: Данные или флаги контекста: значения из подготовки хода, без ввода-вывода.
DATA = "данные контекста"
#: Работает на воркере: читает его activity, в коде workflow не нужен.
WORKER = "на воркере"
#: Хук фронтенда (TUI, CLI, сервер): в Temporal его нет, ядро обходится.
FRONTEND = "хук фронтенда"

#: Имена, которые ядро достаёт из контекста через ``getattr``/``hasattr``.
CORE_CONTEXT_ATTRIBUTES = {
    "active_state": DATA
    + ": ссылка на State текущего workflow-хода, без копии состояния",
    "agent_store": BOUND
    + " (ExternalAgentStore: чтение ресурсов агента — только в activity)",
    "ask_human": FRONTEND + ": вопрос человеку — human_request",
    "attach_state": DATA
    + ": привязка State к RunContext в памяти, без ввода-вывода",
    "auth_connector": WORKER + ": токен модели — activity модели",
    "auto_available": DATA
    + ": вычисляется assemble_run_context из настроек и router",
    "auxiliary_llm": DATA,
    "child_input_scope": BOUND + " (child_transport)",
    "collect_garbage": BOUND + " (no-op)",
    "confirm_request": FRONTEND + ": подтверждение — human_request",
    "ctx": DATA,
    "download_publish": FRONTEND + ": публикация загрузки TUI",
    "durable_execution": DATA,
    "execution_patch": BOUND + " (workflow.patched: прежняя ветка при replay)",
    "environment": WORKER + ": окружение Harbor",
    "guard_probe": BOUND,
    "human_request": BOUND + " (updates/signals workflow)",
    "image_store": WORKER + ": байты картинок читает activity модели",
    "intent_scope": DATA,
    "is_cli_mode": DATA,
    "is_tui": DATA,
    "lock_factory": BOUND + " (nullcontext)",
    "logger": BOUND + " (workflow.logger)",
    "logging_path": DATA,
    "mcp_cleanup": BOUND,
    "mcp_session_owner": WORKER + ": живые MCP-сессии — WorkerResourceHost",
    "model_router": BOUND + " (маршрут по имени, model_routing)",
    "model_transport_configured": DATA,
    "on_messages_evicted": FRONTEND + ": хук TUI",
    "on_stream_delta": FRONTEND + ": потокового предпросмотра нет",
    "on_summary_applied": FRONTEND + ": хук TUI",
    "on_trace_note": FRONTEND + ": хук TUI",
    "operations": BOUND,
    "os_backend": BOUND + " (ExternalResources: прямой доступ — отказ)",
    "parent_call_id": DATA,
    "permission_store": DATA,
    "project_gc_dir": DATA,
    "project_memory_io": BOUND,
    "prompt_loader": BOUND,
    "prompt_resource": BOUND,
    "provider_currency": DATA,
    "routing_contract_v2": DATA
    + ": результат execution_patch при assemble_run_context, без ввода-вывода",
    "subagent_independent_auto": DATA
    + ": версия дочернего контракта из записанного входа",
    "routing_params": DATA
    + ": типизированные параметры two_level из настроек",
    "run_id": DATA,
    "runtime": DATA,
    "settings": DATA,
    "skills_loader": BOUND,
    "skills_registry": BOUND + " (ExternalResources: прямой доступ — отказ)",
    "state_preparer": BOUND,
    "subagent_checkpoint": BOUND + " (child_workflow)",
    "subagent_definitions": BOUND + " (child_preparation)",
    "subagent_diagnostics": DATA,
    "subagent_executor": BOUND + " (child_transport)",
    "subagent_input_scope_factory": BOUND + " (child_workflow)",
    "subagent_preparation": BOUND + " (child_workflow)",
    "subagent_progress_sink": FRONTEND + ": прогресс субагента в TUI",
    "subagent_tool_name": DATA,
    "summary_trace_path": DATA,
    "summary_trace_sink": BOUND,
    "system_id": DATA,
    "thread_id": DATA,
    "thread_store": BOUND + " (TemporalThreadStore)",
    "threads_dir": DATA,
    "tool_effect": BOUND,
    "tools_cache": WORKER + ": discovery инструментов на воркере",
    "tools_data": BOUND + " (метаданные воркера)",
    "tui_confirm": FRONTEND + ": подтверждение — human_request",
    "turn_id": DATA,
    "workspace": DATA,
}

#: Поля ``RunContextBindings``, которые workflow не привязывает, и почему.
UNBOUND_BINDINGS = {
    "auth_connector": "токен модели берёт activity на воркере",
    "environment": "окружение Harbor есть только у воркера",
    "tools_cache": "discovery инструментов — на воркере",
    "on_stream_delta": "потокового предпросмотра в Temporal нет",
    "on_trace_note": "хук TUI",
    "on_messages_evicted": "хук TUI",
}

_CONTEXT_NAMES = {
    "ctx",
    "_ctx",
    "context",
    "run_ctx",
    "parent_ctx",
    "child_ctx",
    "run_context",
}


def _base(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _core_context_attributes():
    found = {}
    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id
                in ("getattr", "hasattr", "port", "runtime_port")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and _base(node.args[0]) in _CONTEXT_NAMES
            ):
                continue
            found.setdefault(node.args[1].value, str(path.relative_to(CORE)))
    return found


def test_every_optional_context_attribute_of_core_has_a_temporal_decision():
    found = _core_context_attributes()
    new = {
        name: path
        for name, path in found.items()
        if name not in CORE_CONTEXT_ATTRIBUTES
    }
    assert not new, (
        "Ядро обращается к новым атрибутам контекста. Под Temporal без порта "
        "работа пойдёт в код workflow. Привяжите порт (gc_core_temporalio/"
        "context.py) или впишите имя в CORE_CONTEXT_ATTRIBUTES с причиной: "
        f"{new}"
    )
    stale = sorted(set(CORE_CONTEXT_ATTRIBUTES) - set(found))
    assert not stale, f"Ядро больше не обращается к этим именам: {stale}"


def _bound_in_context():
    tree = ast.parse(Path(temporal_context.__file__).read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RunContextBindings"
        ):
            return {keyword.arg for keyword in node.keywords}
    raise AssertionError("RunContextBindings(...) не найден в context.py")


def test_every_binding_is_bound_by_the_workflow_or_explained():
    fields = set(inspect.signature(RunContextBindings).parameters)
    bound = _bound_in_context()
    missing = fields - bound - set(UNBOUND_BINDINGS)
    assert not missing, (
        "Новые поля RunContextBindings не привязаны в Temporal "
        f"(context.py) и не объяснены в UNBOUND_BINDINGS: {sorted(missing)}"
    )
    assert not set(UNBOUND_BINDINGS) & bound, (
        "поле привязано — уберите из UNBOUND_BINDINGS"
    )
    assert set(UNBOUND_BINDINGS) <= fields


def test_workflow_operations_cover_the_core_operations_protocol():
    required = {
        name
        for name, value in vars(AgentOperations).items()
        if callable(value) and not name.startswith("_")
    }
    missing = {
        name
        for name in required
        if not callable(getattr(WorkflowOperations, name, None))
    }
    assert not missing, f"WorkflowOperations не реализует: {sorted(missing)}"


def _activity_names_called_by_workflow_code():
    names = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("universal_agent.")
                and node.value.count(".") >= 2
                and " " not in node.value
            ):
                names.add(node.value)
    return names


def test_every_activity_called_by_the_workflow_is_registered(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    from gc_core_temporalio.provider import WorkerConfig
    from gc_core_temporalio.worker import build_runtime
    from temporalio import activity

    config = WorkerConfig.model_validate(
        {
            "state_dir": str(tmp_path),
            "workflow_queue": "q",
            "model_queue": "q",
            "resource_queue": "q",
            "profiles": {"p": {"workspace": str(tmp_path)}},
        }
    )
    provider, native, threads = build_runtime(config)
    registered = {
        activity._Definition.must_from_callable(function).name
        for options in [worker_kwargs(config, native, threads)]
        for function in options.get("activities", [])
    }
    called = _activity_names_called_by_workflow_code()
    assert called, "не нашёл вызовов activities — тест устарел"
    assert called <= registered, (
        f"не зарегистрированы: {sorted(called - registered)}"
    )


def test_custom_thread_store_must_declare_its_paths(tmp_path):
    """Своё хранилище без protected_paths() — отказ, а не тихо незащищённые
    данные; с объявлением — его пути под защитой."""
    import pytest
    from gc_core_temporalio.worker import store_paths

    class Undeclared:
        pass

    class Declared:
        def protected_paths(self):
            return [str(tmp_path / "s3-cache")]

    with pytest.raises(ValueError, match="protected_paths"):
        store_paths(Undeclared())
    assert store_paths(Declared()) == [str(tmp_path / "s3-cache")]


def test_every_bundled_thread_store_is_accepted_by_the_worker(tmp_path):
    """Хранилища из коробки, которые документация называет для воркера, —
    воркер их принимает (Postgres раньше не объявлял protected_paths и
    валил старт). Подключение к базе не нужно."""
    from gc_core.threads.file_store import FileThreadStore
    from gc_core_temporalio.worker import store_paths
    from gc_stores.postgres import PostgresThreadStore
    from gc_stores.sqlite import SQLiteThreadStore

    assert store_paths(FileThreadStore(tmp_path / "files"))
    assert store_paths(SQLiteThreadStore(tmp_path / "t.db"))
    assert (
        store_paths(PostgresThreadStore("postgresql://u@db.invalid/x")) == []
    )


async def test_ask_refuses_to_answer_a_dropped_message(monkeypatch):
    """Сообщение, отброшенное остановкой хода: ask не выдаёт ответ прошлого
    хода за ответ на него."""
    import pytest
    from gc_core_temporalio import AgentSession, MessageNotProcessed

    session = AgentSession(None, agent="a", thread_id="t", task_queue="q")

    async def send(_text, **_options):
        return "m1"

    async def wait(message_id, **_options):
        return {"message_id": message_id, "delivered": False, "answer": "old"}

    monkeypatch.setattr(session, "send", send)
    monkeypatch.setattr(session, "wait", wait)
    with pytest.raises(MessageNotProcessed, match="m1"):
        await session.ask("hello")


def test_plugin_installs_its_sandbox_unless_the_app_set_its_own(caplog):
    """Умолчание Worker (SandboxedWorkflowRunner с умолчаниями SDK) —
    заменяется песочницей плагина; свой runner приложения — сохраняется с
    предупреждением."""
    from gc_core_temporalio.plugin import _keep_user_runner
    from temporalio.worker import UnsandboxedWorkflowRunner
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    default = SandboxedWorkflowRunner()
    ours = _keep_user_runner(default)
    assert ours is not default and isinstance(ours, SandboxedWorkflowRunner)
    assert ours.restrictions != default.restrictions
    custom = UnsandboxedWorkflowRunner()
    assert _keep_user_runner(custom) is custom
    assert "не применена" in caplog.text
