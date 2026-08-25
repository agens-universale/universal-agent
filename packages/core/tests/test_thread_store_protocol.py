"""Гейт: контракт хранилища тредов годен и держится.

Задача 7. Встраивающий должен уметь положить историю треда в базу, а не в
``data_*.jsonl``. Первый шаг — контракт (``ThreadStore``) и две реализации:
файловая (перенос нынешнего поведения) и in-memory (доказательство годности).

Почему обе проверяются ОДНИМ набором тестов: разъезжаются реализации именно
там, где их проверяют по-разному. У ``TaskStore`` в TUI второй реализации нет
вовсе — и протокол за полгода отстал от факта на восемь методов.

Здесь же закреплены свойства, которые чужой реализации придётся соблюдать:
идемпотентность дописывания, пустота вместо ошибки на отсутствующем треде,
точечность ``patch_runtime_flag``.
"""
from typing import Any

import pytest

from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_protocol import ThreadStore
from gc_testkit.memory_thread_store import MemoryThreadStore


@pytest.fixture(params=["file", "memory"])
def store(request, tmp_path) -> Any:
    """Один набор проверок на обе реализации."""
    if request.param == "file":
        s = FileThreadStore(str(tmp_path))
        s.ensure_ready()
        return s
    return MemoryThreadStore()


SYS, TID = "cli", "t-1"


def _msg(mid: str, text: str) -> dict:
    return {"id": mid, "role": "user", "content": text}


# --- сам контракт ----------------------------------------------------------

def test_both_satisfy_the_protocol(store):
    """Структурная типизация: наследовать ничего не нужно, достаточно совпасть."""
    assert isinstance(store, ThreadStore)


def test_implementations_inherit_nothing():
    """Реализации не наследуют наш базовый класс — иначе чужой пришлось бы.

    Ровно та беда, из-за которой ``FsBackend`` (ABC с 31 методом) нельзя
    развивать: новый метод ломает каждую внешнюю реализацию.
    """
    for cls in (FileThreadStore, MemoryThreadStore):
        assert cls.__mro__[1:] == (object,), (
            f"{cls.__name__} от кого-то наследуется: {cls.__mro__[1:]}"
        )


# --- поведение, обязательное для любой реализации --------------------------

def test_missing_thread_reads_empty_not_raises(store):
    """Нет треда — пустота, а не исключение."""
    assert store.load_messages(SYS, "no-such-thread") == []
    assert store.load_meta(SYS, "no-such-thread") == {}


def test_append_then_read_roundtrip(store):
    written = store.append_messages(SYS, TID, [_msg("m1", "раз"), _msg("m2", "два")])
    assert written == 2
    got = store.load_messages(SYS, TID)
    assert [m["content"] for m in got] == ["раз", "два"]


def test_append_is_idempotent(store):
    """Батч персистится ДВАЖДЫ — сразу после тулов и на выходе цикла.

    Повтор не должен задваивать записи, иначе история треда растёт вдвое на
    каждом ходу. Это свойство ядра, а не файлов, поэтому и в контракте.
    """
    batch = [_msg("m1", "раз"), _msg("m2", "два")]
    assert store.append_messages(SYS, TID, batch) == 2
    assert store.append_messages(SYS, TID, batch) == 0
    assert len(store.load_messages(SYS, TID)) == 2


def test_meta_roundtrip(store):
    store.save_meta(SYS, TID, {"token_usage": {"total": 7}})
    assert store.load_meta(SYS, TID)["token_usage"]["total"] == 7


def test_patch_runtime_flag_is_pointwise(store):
    """Флаг рантайма не должен затирать остальной meta.

    Сюда пишут ВО ВРЕМЯ работы (``summarizing``), параллельно с самим прогоном,
    и read-modify-write целого meta потерял бы то, что записал прогон.
    """
    store.save_meta(SYS, TID, {"token_usage": {"total": 7}, "runtime": {}})
    store.patch_runtime_flag(SYS, TID, "summarizing", active=True)

    meta = store.load_meta(SYS, TID)
    assert meta["runtime"].get("summarizing") is True
    assert meta["token_usage"]["total"] == 7, "патч флага затёр чужие секции meta"

    store.patch_runtime_flag(SYS, TID, "summarizing", active=False)
    # Секции может не быть вовсе: снятие последнего флага убирает её целиком.
    assert not store.load_meta(SYS, TID).get("runtime", {}).get("summarizing")
    assert "runtime" not in store.load_meta(SYS, TID), (
        "пустая секция runtime осталась — реализации разошлись по форме meta"
    )


def test_discard_message(store):
    store.append_messages(SYS, TID, [_msg("m1", "раз"), _msg("m2", "два")])
    assert store.discard_message(SYS, TID, "m1") is True
    assert [m["id"] for m in store.load_messages(SYS, TID)] == ["m2"]
    assert store.discard_message(SYS, TID, "no-such-thread") is False


def test_reset_clears_the_thread(store):
    store.append_messages(SYS, TID, [_msg("m1", "раз")])
    store.save_meta(SYS, TID, {"x": 1})
    store.reset(SYS, TID)
    assert store.load_messages(SYS, TID) == []
    assert store.load_meta(SYS, TID) == {}


def test_subagent_trace_roundtrip(store):
    store.append_subagent_trace(
        SYS, TID,
        parent_call_id="call-1", child_run_id="run-1",
        kind="call", payload={"name": "fs_read_file"},
    )
    trace = store.read_subagent_trace(SYS, TID)
    assert len(trace) == 1
    assert trace[0]["kind"] == "call"


# --- то, ради чего всё затевалось ------------------------------------------

def test_memory_store_touches_no_disk(tmp_path, monkeypatch):
    """In-memory реализация не создаёт ни одного файла.

    Это и есть гейт, отсутствие которого сгноило ``TaskStore``: он краснеет
    ровно в тот день, когда кто-то допишет прямой ``open()`` мимо контракта.
    """
    monkeypatch.chdir(tmp_path)
    store = MemoryThreadStore()

    store.append_messages(SYS, TID, [_msg("m1", "раз")])
    store.save_meta(SYS, TID, {"x": 1})
    store.patch_runtime_flag(SYS, TID, "summarizing", active=True)
    store.append_subagent_trace(
        SYS, TID, parent_call_id="c", child_run_id="r", kind="call", payload={},
    )
    store.load_messages(SYS, TID)
    store.read_subagent_trace(SYS, TID)

    created = list(tmp_path.rglob("*"))
    assert created == [], f"in-memory store создал файлы: {created}"
    assert store.calls["append_messages"] == 1, "обращения шли мимо store"


# --- сквозная проводка -----------------------------------------------------

def test_store_reaches_the_context_and_session(tmp_path):
    """Хранилище доезжает от вызывающего до ``ThreadSession``.

    Проверяем всю цепочку разом: ``create_run_context(thread_store=…)`` →
    ``ctx.thread_store`` → ``ThreadSession.from_config(store=…)``. Разрыв в
    любом звене означал бы, что параметр принят и молча проигнорирован — самый
    неприятный вид поломки, потому что выглядит как работающая фича.
    """
    from gc_core.run_context import create_run_context
    from gc_core.thread_store import ThreadSession

    store = MemoryThreadStore()
    ctx = create_run_context(
        {"stop_tools": [], "backend_api_mode": "chat_completions", "save_logs": False},
        thread_store=store,
    )
    assert ctx.thread_store is store, "контекст не донёс хранилище"

    session = ThreadSession.from_config(
        config_dict=ctx.config_dict, system_id=SYS, thread_id=TID,
        store=getattr(ctx, "thread_store", None),
    )
    assert session.store is store, "сессия собралась со своим хранилищем"
    assert store.calls.get("load_thread_state"), "сессия читала мимо хранилища"


def test_session_writes_go_through_the_store(tmp_path, monkeypatch):
    """Запись сессии идёт в хранилище, а не на диск.

    Гейт, ради которого задача и делалась: с in-memory хранилищем прогон не
    создаёт НИ ОДНОГО файла. Он краснеет в тот день, когда кто-то допишет
    прямой ``open()`` мимо контракта — именно этого не хватало ``TaskStore``.
    """
    monkeypatch.chdir(tmp_path)
    from gc_core.thread_store import ThreadSession

    store = MemoryThreadStore()
    session = ThreadSession.from_config(
        config_dict={"threads_dump_dir": str(tmp_path / "threads")},
        system_id=SYS, thread_id=TID, store=store,
    )
    session.persist_batch([_msg("m1", "раз")], previous_response_id=None)

    assert store.calls.get("append_messages"), "запись прошла мимо хранилища"
    assert store.load_messages(SYS, TID), "сообщение не сохранилось"
    created = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created == [], f"сессия создала файлы вопреки in-memory хранилищу: {created}"


def test_no_direct_file_calls_left_in_core():
    """В ядре не осталось прямых вызовов файлового слоя мимо хранилища.

    Главный гейт задачи. До него девять вызовов в четырёх файлах
    (``agent_executor``, ``run_summary_coordinator``, ``manual_compaction``,
    ``subagents/sidecar``) работали с тредом, у которого store УЖЕ есть, но
    звали модульные функции по старой памяти.

    Встраивающий с базой получил бы расщепление: история и meta от сессии — в
    базу, а вот эти девять записей — в файлы. Молча, без ошибки. Хуже, чем
    явный отказ.

    Считаем ТОЛЬКО голые имена: ``store.update_runtime_meta(...)`` — это уже
    метод хранилища, а не модульная функция, и различие здесь всё.
    """
    import ast

    from gc_testkit.paths import REPO

    module_fns = {
        "load_thread_messages", "load_thread_meta", "save_thread_meta",
        "append_new_thread_messages", "patch_runtime_flag", "update_runtime_meta",
        "update_tui_meta", "discard_message_by_id", "reset_thread_files",
        "delete_thread", "append_subagent_trace_record", "read_subagent_trace",
    }
    core = REPO / "packages" / "core" / "src" / "gc_core"
    # Сам слой и его реализации зовут модульные функции законно — это их работа.
    own_files = {"thread_store.py", "thread_store_file.py", "thread_store_protocol.py"}
    offenders = []
    for path in core.rglob("*.py"):
        if path.name in own_files:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in module_fns
            ):
                offenders.append(
                    f"{path.relative_to(core)}:{node.lineno} {node.func.id}"
                )

    assert offenders == [], (
        "прямые вызовы файлового слоя мимо ThreadStore:\n  "
        + "\n  ".join(sorted(offenders))
        + "\nу встраивающего с базой эти записи молча уйдут на диск"
    )
