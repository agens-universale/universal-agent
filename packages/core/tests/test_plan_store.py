"""План режима plan в состоянии сессии: форма блока, сохранение, восстановление."""

from datetime import UTC, datetime

from gc_core.execution.environment import execution_environment
from gc_core.execution.state import SharedStateData
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    serialize_shared_block,
)
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.plan_store import (
    PLAN_STATUS_LABELS,
    PLAN_STATUSES,
    new_plan_block,
    normalize_plan_block,
    now_stamp,
)


def _restore(block):
    return ThreadMetaMapper.shared_from_meta(
        {"state_schema_version": STATE_SCHEMA_VERSION, "shared": block}
    )


def test_session_without_a_plan_is_saved_exactly_as_before():
    block = serialize_shared_block(SharedStateData())
    assert "plan" not in block and "mode_announced" not in block
    restored = _restore(block)
    assert restored.plan is None and restored.mode_announced is None


def test_plan_survives_save_and_restore():
    plan = new_plan_block("  1. сделать\n2. проверить  ")
    assert plan["text"] == "1. сделать\n2. проверить"
    assert plan["status"] == "proposed" and plan["approved_mode"] is None
    shared = SharedStateData(plan=plan, mode_announced="plan")
    restored = _restore(serialize_shared_block(shared))
    assert restored.plan == plan
    assert restored.mode_announced == "plan"


def test_garbage_in_meta_does_not_break_loading():
    for raw in (
        None,
        "x",
        {},
        {"text": "", "status": "proposed"},
        {"text": "t", "status": "нет такого"},
    ):
        assert normalize_plan_block(raw) is None
    assert _restore({"plan": {"text": "t", "status": "мусор"}}).plan is None


def test_every_status_has_a_label():
    assert set(PLAN_STATUS_LABELS) == PLAN_STATUSES


def test_labels_and_accessors_for_frontends():
    """Фронтенды получают готовые подписи и признаки: статусы и поля блока
    плана — дело ядра (в исходниках TUI имена статусов запрещены сторожем)."""
    from gc_core.plan_store import (
        plan_accepted_mode,
        plan_awaits_decision,
        plan_status_label,
    )

    plan = new_plan_block("1. шаг")
    assert plan_status_label(plan) == "предложен"
    assert plan_awaits_decision(plan) is True
    assert plan_accepted_mode(plan) is None

    plan["status"] = "reworking"
    assert plan_status_label(plan) == "в доработке"
    assert plan_awaits_decision(plan) is True

    plan.update(status="approved", approved_mode="manual")
    assert plan_status_label(plan) == "утверждён"
    assert plan_awaits_decision(plan) is False
    assert plan_accepted_mode(plan) == "manual"

    for missing in (None, {}, "мусор"):
        assert plan_status_label(missing) == ""
        assert plan_awaits_decision(missing) is False
        assert plan_accepted_mode(missing) is None


async def _seed(store, shared):
    from gc_core.threads.session_port import SessionPort

    port = SessionPort(store)
    await port.asave_meta(
        "cli", "t1", {"shared": serialize_shared_block(shared)}
    )
    await port.arelease_lease("cli", "t1")


async def test_clearing_the_plan_in_session_meta(tmp_path):
    """`/plan clear`: план уходит из сессии, модели взводится одноразовое
    сообщение. Сессия без плана не трогается вовсе."""
    from gc_core.threads.facade import Threads
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import ThreadKey

    store = FileThreadStore(str(tmp_path))
    threads, key = Threads(store), ThreadKey(system="cli", thread="t1")
    await _seed(
        store,
        SharedStateData(plan=new_plan_block("1. шаг"), permission_mode="plan"),
    )
    assert await threads.clear_plan(key) is True
    saved = Threads.legacy_meta(await threads.get(key))["shared"]
    assert "plan" not in saved
    assert saved["plan_removed_notice"] is True
    assert saved["permission_mode"] == "plan"  # остальное состояние цело
    restored = _restore(saved)
    assert restored.plan is None and restored.plan_removed_notice is True

    assert await threads.clear_plan(key) is False  # плана уже нет


async def test_clearing_the_plan_also_removes_its_file_for_the_human(
    tmp_path,
):
    """Файл плана для человека не должен пережить `/plan clear`: иначе снятый
    план выглядит действующим планом сессии."""
    import os

    from gc_core.thread_store import plan_file_path
    from gc_core.threads.facade import Threads
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import ThreadKey

    store = FileThreadStore(str(tmp_path))
    threads = Threads(store)
    await _seed(store, SharedStateData(plan=new_plan_block("1. шаг")))
    path = plan_file_path(str(tmp_path), "cli", "t1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("1. шаг")
    assert await threads.clear_plan(ThreadKey(system="cli", thread="t1"))
    assert not os.path.exists(path)
    none = ThreadKey(system="cli", thread="t-none")
    assert await threads.clear_plan(none) is False


def test_removed_notice_is_written_only_when_armed():
    assert "plan_removed_notice" not in serialize_shared_block(
        SharedStateData()
    )


def test_plan_time_comes_from_the_execution_environment():
    """Под Temporal код плана идёт в workflow: часы — из окружения
    исполнения, прямое чтение часов песочница запрещает."""
    fixed = datetime(2026, 10, 16, 12, 30, 45, 123456, tzinfo=UTC)
    with execution_environment(
        new_id=lambda prefix: prefix, now=lambda: fixed
    ):
        assert now_stamp() == "2026-10-16T12:30:45+00:00"
        block = new_plan_block("1. шаг")
    assert block["updated_at"] == "2026-10-16T12:30:45+00:00"
