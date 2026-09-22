"""Гейт: контракт хранилища тредов годен и держится.

Задача 7. Встраивающий должен уметь положить историю треда в базу, а не в
``data_*.jsonl``. Первый шаг — контракт (``ThreadStoreBase``) и две реализации:
файловая (перенос нынешнего поведения) и in-memory (доказательство годности).

Почему обе проверяются ОДНИМ набором тестов: разъезжаются реализации именно
там, где их проверяют по-разному. У ``TaskStore`` в TUI второй реализации нет
вовсе — и протокол за полгода отстал от факта на восемь методов.

Сам набор свойств переехал в ``gc_testkit.thread_store_conformance``, и это не
перекладывание файлов. Реализацию пишет ВСТРАИВАЮЩИЙ — у себя, поверх своей
базы. Проверить её нашей меркой он должен одной строкой
(``assert_conforms(factory)``), не переписывая наши тесты. Здесь остаётся то,
что к чужой реализации отношения не имеет: сквозная проводка через ядро и
AST-гейт на прямые вызовы файлового слоя.

История, из-за которой набор свойств вырос вдвое. Первая редакция описывала
методы, но не свойства — и две НАШИ СОБСТВЕННЫЕ реализации разошлись в четырёх
местах, оставаясь зелёными на всех 23 тестах: третий элемент
``load_thread_state`` (количество против номера ``seq``), пополнение
``known_keys``, шесть полей записи трассы против четырёх и ``id`` на чтении.
Каждое из четырёх теперь проверяется.
"""

from typing import Any

import pytest
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_types import SubagentTraceRecord, ThreadState
from gc_testkit.memory_thread_store import MemoryThreadStore
from gc_testkit.thread_store_conformance import (
    BEHAVIOUR_CHECKS,
    CONTRACT_METHODS,
    STRUCTURE_CHECKS,
    ThreadStoreContractError,
    assert_conforms,
)


@pytest.fixture(params=["file", "memory"])
def store_factory(request, tmp_path):
    """Фабрика хранилища: конформансу нужно свежее на каждую проверку."""
    if request.param == "file":

        def make() -> Any:
            s = FileThreadStore(str(tmp_path / request.node.name[:40]))
            s.ensure_ready()
            return s

        return make
    return MemoryThreadStore


@pytest.fixture
def store(store_factory) -> Any:
    return store_factory()


SYS, TID = "cli", "t-1"


def _msg(mid: str, text: str) -> dict:
    return {"id": mid, "role": "user", "content": text}


# --- сам контракт ----------------------------------------------------------


def test_both_pass_the_conformance_suite(store_factory):
    """Обе реализации проходят полный набор — тот же, что запустит чужая.

    Заменяет прежний ``isinstance``-тест, который был БЕСПОЛЕЗЕН: у класса,
    где все одиннадцать методов ``async def``, ``isinstance`` истинно, этот
    тест зелёный, а восемь поведенческих красные (проверено запуском).
    """
    passed = assert_conforms(store_factory)
    assert len(passed) == len(STRUCTURE_CHECKS) + len(BEHAVIOUR_CHECKS)


def test_our_implementations_share_the_base():
    """НАШИ реализации наследуют базу — ради общих инвариантов.

    Прежняя редакция требовала обратного: «никто ничего не наследует, чтобы
    чужому не пришлось». Половина верна и осталась в силе (см. тест ниже —
    чужое хранилище по-прежнему принимается без наследования), а вот вторая
    вышла боком. Инвариантам стало негде жить, и две НАШИ реализации разъехались
    ровно на них: файловая штамповала meta на каждом пути записи, in-memory —
    только в ``save_meta``. Обе были зелёными, потому что и конформанс проверял
    штампы лишь у ``save_meta``.
    """
    from gc_core.thread_store_base import ThreadStoreBase

    for cls in (FileThreadStore, MemoryThreadStore):
        assert issubclass(cls, ThreadStoreBase), (
            f"{cls.__name__} не наследует ThreadStoreBase — инварианты снова разъедутся по копиям"
        )


def test_base_refuses_an_incomplete_implementation():
    """Неполная реализация не создастся вовсе — в этом и смысл ABC.

    У протокола она обнаруживалась поздно: молчали все, пока агент не доходил
    до проводки. Здесь отказ приходит из конструктора и называет, чего нет.
    """
    from gc_core.thread_store_base import ThreadStoreBase

    class HalfStore(ThreadStoreBase):
        def load_messages(self, system_id, thread_id):
            return []

    with pytest.raises(TypeError) as excinfo:
        HalfStore()

    assert "save_meta" in str(excinfo.value), excinfo.value


def test_a_foreign_store_without_our_base_is_still_accepted():
    """Наследование — короткий путь, а не пропуск на входе.

    Чужой объект (из DI-контейнера, обёртка, реализация, не желающая
    импортировать наш класс) обязан работать, совпав по именам и сигнатурам.
    Если однажды кто-то поставит здесь ``isinstance``, этот тест покраснеет.
    """
    from gc_core.thread_store_async import athread_store
    from gc_core.thread_store_base import ThreadStoreBase

    store = MemoryThreadStore()

    class Foreign:
        """Ничего не наследует, всё делегирует — законное прокси-хранилище."""

        def __getattr__(self, name):
            return getattr(store, name)

    foreign = Foreign()
    assert not isinstance(foreign, ThreadStoreBase)

    port = athread_store(foreign)  # не должно бросить
    assert port is not None


def test_naming_alone_does_not_make_a_store():
    """Совпасть по ИМЕНАМ мало: корутины под синхронными именами — отказ.

    Проверка при проводке структурная, то есть смотрит на наличие методов.
    Реализация, у которой все одиннадцать объявлены ``async def`` под
    синхронными именами, по именам проходит — и была бы принята молча.
    Молчание тут дорого стоит: синхронный вызывающий отбросил бы корутину без
    ошибки, и часть записей треда потерялась бы.

    Раньше та же мысль формулировалась через ``isinstance`` к ``Protocol``
    (``runtime_checkable`` сверяет только наличие атрибутов). Протокол снят —
    контракт объявлен один раз, в ABC, — а проверяемое свойство осталось тем
    же и проверяется прямо: конформанс обязан отказать и назвать причину.
    """

    class AllAsync:
        pass

    for name in CONTRACT_METHODS:

        async def method(self, *args, **kwargs):
            return None

        setattr(AllAsync, name, method)

    with pytest.raises(ThreadStoreContractError, match="async def"):
        assert_conforms(AllAsync)


def test_a_prefixed_names_are_rejected_with_a_hint():
    """Имена ``aload_meta`` — частая ошибка; отказ обязан её назвать.

    Так называет методы тот, кто прочитал документацию про async и решил, что
    ядро ждёт их. Без подсказки он получит ``AttributeError`` глубоко внутри
    чтения треда, где причина уже не видна.
    """

    class ANamed:
        pass

    for name in CONTRACT_METHODS:

        async def method(self, *args, **kwargs):
            return None

        setattr(ANamed, f"a{name}", method)

    with pytest.raises(ThreadStoreContractError) as err:
        assert_conforms(ANamed)
    assert "AsyncThreadStore" in str(err.value), "отказ не подсказал асинхронный контракт"


# --- типы контракта --------------------------------------------------------


def test_thread_state_is_named_but_still_unpacks(store):
    """``ThreadState`` — именованный кортеж: старая распаковка работает.

    Имена нужны из-за третьего элемента: он ``int``, и подмену «номер seq» на
    «количество записей» не поймает ни одна проверка типов. Наши реализации
    разошлись именно здесь.
    """
    store.append_messages(SYS, TID, [_msg("m1", "раз"), _msg("m2", "два")])

    state = store.load_thread_state(SYS, TID)
    assert isinstance(state, ThreadState)

    messages, keys, max_seq = state  # позиционная распаковка — как раньше
    assert messages == state.messages
    assert keys == state.known_keys
    assert max_seq == state.max_seq == 2


def test_trace_record_has_all_six_fields(store):
    """Запись трассы — замкнутая форма, шесть полей."""
    record = store.append_subagent_trace(
        SYS,
        TID,
        parent_call_id="call-1",
        child_run_id="run-1",
        kind="call",
        payload={"name": "fs_read_file"},
    )
    assert set(record) == set(SubagentTraceRecord.__annotations__)
    assert record["seq"] == 1 and isinstance(record["ts"], str)


# --- сквозная проводка -----------------------------------------------------


async def test_store_reaches_the_context_and_session(tmp_path):
    """Хранилище доезжает от вызывающего до ``ThreadSession``.

    Проверяем всю цепочку разом: ``acreate_run_context(thread_store=…)`` →
    ``ctx.thread_store`` → ``ThreadSession.afrom_config(store=…)``. Разрыв в
    любом звене означал бы, что параметр принят и молча проигнорирован — самый
    неприятный вид поломки, потому что выглядит как работающая фича.
    """
    from gc_core.run_context import acreate_run_context
    from gc_core.thread_store import ThreadSession

    store = MemoryThreadStore()
    ctx = await acreate_run_context(
        {"stop_tools": [], "backend_api_mode": "chat_completions", "save_logs": False},
        thread_store=store,
    )
    assert ctx.thread_store is store, "контекст не донёс хранилище"

    session = await ThreadSession.afrom_config(
        config_dict=ctx.config_dict,
        system_id=SYS,
        thread_id=TID,
        store=getattr(ctx, "thread_store", None),
    )
    assert session.store is store, "сессия собралась со своим хранилищем"
    assert store.calls.get("load_thread_state"), "сессия читала мимо хранилища"


async def test_session_writes_go_through_the_store(tmp_path, monkeypatch):
    """Запись сессии идёт в хранилище, а не на диск.

    Гейт, ради которого задача и делалась: с in-memory хранилищем прогон не
    создаёт НИ ОДНОГО файла. Он краснеет в тот день, когда кто-то допишет
    прямой ``open()`` мимо контракта — именно этого не хватало ``TaskStore``.
    """
    monkeypatch.chdir(tmp_path)
    from gc_core.thread_store import ThreadSession

    store = MemoryThreadStore()
    session = await ThreadSession.afrom_config(
        config_dict={"threads_dump_dir": str(tmp_path / "threads")},
        system_id=SYS,
        thread_id=TID,
        store=store,
    )
    await session.apersist_batch([_msg("m1", "раз")], previous_response_id=None)

    assert store.calls.get("append_messages"), "запись прошла мимо хранилища"
    assert store.load_messages(SYS, TID), "сообщение не сохранилось"
    created = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created == [], f"сессия создала файлы вопреки in-memory хранилищу: {created}"


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
        SYS,
        TID,
        parent_call_id="c",
        child_run_id="r",
        kind="call",
        payload={},
    )
    store.load_messages(SYS, TID)
    store.read_subagent_trace(SYS, TID)

    created = list(tmp_path.rglob("*"))
    assert created == [], f"in-memory store создал файлы: {created}"
    assert store.calls["append_messages"] == 1, "обращения шли мимо store"


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
        "load_thread_messages",
        "load_thread_meta",
        "save_thread_meta",
        "append_new_thread_messages",
        "patch_runtime_flag",
        "update_runtime_meta",
        "update_tui_meta",
        "discard_message_by_id",
        "reset_thread_files",
        "delete_thread",
        "append_subagent_trace_record",
        "read_subagent_trace",
    }
    core = REPO / "packages" / "core" / "src" / "gc_core"
    # Сам слой и его реализации зовут модульные функции законно — это их работа.
    own_files = {"thread_store.py", "thread_store_file.py", "thread_store_types.py"}
    offenders = []
    for path in core.rglob("*.py"):
        if path.name in own_files:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in module_fns:
                offenders.append(f"{path.relative_to(core)}:{node.lineno} {node.func.id}")

    assert offenders == [], (
        "прямые вызовы файлового слоя мимо хранилища:\n  "
        + "\n  ".join(sorted(offenders))
        + "\nу встраивающего с базой эти записи молча уйдут на диск"
    )
