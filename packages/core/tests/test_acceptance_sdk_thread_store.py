"""Приёмка SDK на чужом хранилище: агент работает, диска не касается.

Отдельно от ``test_acceptance_non_blocking``, потому что проверяет другое.
Там — что loop встраивающего остаётся живым; здесь — что встраивающий может
подсунуть СВОЁ хранилище и получить работающего агента, ни разу не тронувшего
файловую систему.

Почему это нужно отдельным гейтом. Приёмка переезда сегодня всегда идёт на
``FileThreadStore``: параметр ``thread_store`` не передаётся в ней ни разу, то
есть ни один её критерий чужого хранилища не измеряет. А ломается оно
характерно — МОЛЧА: из шестнадцати обращений ядра громко падают два, остальные
теряют запись без ошибки и без лога. Поэтому здесь проверяется не «прогон не
упал», а состав того, что доехало до хранилища.

Полный прогон, а не единичный вызов: модель заменена ``ScriptedBackend``, но
цикл и судья finish-gate — настоящие. Диспетчер тулов заглушен ЦЕЛИКОМ
(фикстура ``_stub_turn_finish``): и ``turn_finish``, и ``exec_sleep`` сразу
получают ``ok``, настоящий путь тула здесь не исполняется. Предмет файла —
хранилище тредов; путь тула меряет ``test_acceptance_non_blocking``, у него
заглушка делегирующая.
"""
import asyncio

import pytest

from gc_core.aio import AsyncAgent
from gc_testkit.memory_thread_store import MemoryThreadStore

from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    """Сплошная заглушка диспетчера тулов: у стенда нет tools_data.

    После правки develop «батч с turn_finish — сначала вся work, закрытие по
    output» ход ИСПОЛНЯЕТ финализатор и закрывается по факту ``ok``, а не по
    обнаружению вызова в трассе. Без tools_data это unknown tool, ход не
    закрывается и упирается в ``step_limit``.

    Сплошная, а не делегирующая: мгновенный ``ok`` получает ЛЮБОЙ тул, и
    ``exec_sleep`` из сценария не исполняется вовсе. Для этого файла так и
    надо — проверяется хранилище, а не тулы. Но и путь тула тесты файла не
    видят: блокировка в нём сюда не доходит (проверено подменой — 0,2 с сна в
    ``exec_sleep`` красят ``test_acceptance_non_blocking`` и не трогают
    здешний замер отзывчивости).
    """
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)


#: Ход 1 — вызов тула; сам ``exec_sleep`` не исполняется, заглушка выше
#: отвечает за него мгновенно. Ход 2 — финализатор. Ход 3 — судья.
TURNS = [
    assistant_tool_call("exec_sleep", {"seconds": 0.05}, call_id="c1"),
    assistant_tool_call("turn_finish", {
        "solution_type": "direct_answer", "task_report": "готово", "handoff": "{}",
    }, call_id="c2"),
    judge_accept(),
]


class AsyncMemoryThreadStore:
    """Асинхронный двойник — так выглядит хранилище на ``redis.asyncio``.

    Синхронных методов нет вовсе: до правок ядро такую реализацию вызвать не
    могло, а часть записей теряло молча.
    """

    def __init__(self) -> None:
        self.inner = MemoryThreadStore()
        self.awaited = 0

    def _make(name):  # noqa: N805
        async def method(self, *args, **kwargs):
            self.awaited += 1
            await asyncio.sleep(0)
            return getattr(self.inner, name)(*args, **kwargs)

        method.__name__ = f"a{name}"
        return method

    from gc_core.thread_store_base import CONTRACT_METHODS as _NAMES

    for _n in _NAMES:
        locals()[f"a{_n}"] = _make(_n)
    del _make, _n, _NAMES

    @property
    def calls(self) -> dict:
        return self.inner.calls


@pytest.fixture(params=["sync", "async"])
def store(request):
    """Обе формы чужого хранилища проходят одну приёмку."""
    return MemoryThreadStore() if request.param == "sync" else AsyncMemoryThreadStore()


def _inner(store):
    return store.inner if isinstance(store, AsyncMemoryThreadStore) else store


#: SDK пишет тред под своим ``system_id``, не под ``cli``.
SDK_SYS = "sdk"


async def test_sdk_run_writes_only_to_the_given_store(tmp_path, monkeypatch, store):
    """Главный гейт: полный прогон агента не создаёт ни одного файла треда.

    ``tmp_path`` — рабочий каталог прогона, и после него там не должно
    появиться ни ``data_*.jsonl``, ни ``meta_*.json``, ни архивов компакции.
    Краснеет в тот день, когда кто-то допишет прямой ``open()`` мимо контракта.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))

    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        answer = await agent.run("сделай что-нибудь", thread_id="t-sdk")

    assert answer, "агент не ответил"

    thread_files = [
        p for p in tmp_path.rglob("*")
        if p.is_file() and (
            p.name.startswith(("data_", "meta_")) or "__pre_summary__" in p.name
        )
    ]
    assert thread_files == [], (
        f"прогон создал файлы треда вопреки чужому хранилищу: {thread_files}"
    )


async def test_sdk_run_persists_the_whole_thread(tmp_path, monkeypatch, store):
    """История и meta доехали ЦЕЛИКОМ, а не частично.

    Частичная запись — характерная поломка этого шва: громко падают два
    обращения из шестнадцати, остальные молчат. Поэтому проверяется состав.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))

    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        await agent.run("сделай что-нибудь", thread_id="t-sdk")

    inner = _inner(store)
    messages = inner.load_messages(SDK_SYS, "t-sdk")
    roles = [m.get("role") or m.get("type") for m in messages]

    assert "user" in roles, "пользовательское сообщение не сохранилось"
    assert any(r == "function_call" for r in roles), "вызов тула не сохранился"
    assert any(r == "function_call_output" for r in roles), "результат тула не сохранился"
    assert all(m.get("id") for m in messages), "у сообщения нет id"

    meta = inner.load_meta(SDK_SYS, "t-sdk")
    assert meta.get("messages_count") == len(messages), (
        f"messages_count={meta.get('messages_count')} против {len(messages)} сообщений"
    )
    assert meta.get("backend_api_mode"), "backend_api_mode не записан"
    assert isinstance(meta.get("token_usage"), dict), "token_usage не записан"


async def test_runtime_flags_are_cleared_after_the_run(tmp_path, monkeypatch, store):
    """Секции ``runtime`` не остаётся: индикаторы сняты, пустая секция убрана.

    Свойство контракта («снятие последнего флага убирает секцию целиком»)
    проверяется здесь на СКВОЗНОМ прогоне, а не на прямом вызове: именно так
    оно и ломалось — флаги молча не доезжали до хранилища.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))

    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        await agent.run("сделай что-нибудь", thread_id="t-sdk")

    runtime = _inner(store).load_meta(SDK_SYS, "t-sdk").get("runtime", {})
    for flag in ("summarizing", "finalizing", "finish_gate_checking"):
        assert not runtime.get(flag), f"индикатор {flag} остался взведённым"


async def test_second_run_continues_the_same_thread(tmp_path, monkeypatch, store):
    """Второй прогон дочитывает историю из хранилища и продолжает её.

    Проверяет чтение — ``load_thread_state`` и ``max_seq``. Ошибка «номер
    против количества» проявляется именно тут: при неверном ``max_seq`` второй
    прогон переиспользует номера, и хвостовой читатель TUI перестраивает трейс.
    """
    cfg = parity_config(tmp_path, monkeypatch)

    install(monkeypatch, ScriptedBackend(list(TURNS)))
    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        await agent.run("первый", thread_id="t-sdk")
    after_first = len(_inner(store).load_messages(SDK_SYS, "t-sdk"))

    install(monkeypatch, ScriptedBackend(list(TURNS)))
    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        await agent.run("второй", thread_id="t-sdk")

    messages = _inner(store).load_messages(SDK_SYS, "t-sdk")
    assert len(messages) > after_first, "второй прогон не дописал историю"
    assert len({m["id"] for m in messages}) == len(messages), "id задвоились"

    texts = [str(m.get("content") or "") for m in messages]
    assert any("первый" in t for t in texts), "история первого прогона потеряна"
    assert any("второй" in t for t in texts), "второй прогон не записан"


async def test_native_async_store_is_awaited_not_adapted(tmp_path, monkeypatch):
    """Асинхронное хранилище ждётся нативно, а не заворачивается в адаптер.

    Иначе выигрыш мнимый: адаптер зовёт синхронно, и сетевой round-trip
    морозит цикл — замерено, что по лагу он неотличим от прямой блокировки.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    store = AsyncMemoryThreadStore()

    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        await agent.run("сделай что-нибудь", thread_id="t-sdk")

    assert store.awaited > 0, "ни одна корутина хранилища не была дождана"
    assert store.inner.load_messages(SDK_SYS, "t-sdk"), "история не сохранилась"


async def test_loop_stays_responsive_with_a_slow_async_store(tmp_path, monkeypatch):
    """Медленное АСИНХРОННОЕ хранилище не морозит цикл.

    Что меряется: loop свободен всю жизнь агента — сборку, прогон и
    закрытие, — пока ядро ждёт хранилище с RTT 5 мс. То есть путь хранилища
    и собственные куски ядра между его обращениями. Путь тула НЕ меряется:
    диспетчер заглушен целиком (фикстура ``_stub_turn_finish``), и блокировка
    в настоящем ``exec_sleep`` этот тест не красит — её ловит
    ``test_acceptance_non_blocking``.

    Ради этого и заводился нативный путь. Закон ниже снят тем же прогретым
    пульсом на ходах этого файла, но с настоящим ``exec_sleep`` вместо
    сплошной заглушки: у синхронного, блокирующего хранилища 25 обращений за
    прогон ложатся в два куска без уступки — 11 и 14 обращений. При RTT 5 мс
    это разрывы около 85 и 100 мс; потолок в 150 мс их пропускает, отвергает
    правило «не больше одного пропуска». При 10 мс пробит и потолок — около
    185 мс.

    Здесь RTT = 5 мс, и нативно ожидаемое хранилище держит разрывы у шага
    пульса: худший 12 мс, пропусков ноль. Проверено подменой: ``time.sleep``
    вместо ``await asyncio.sleep`` в этом хранилище сливает весь прогон в
    один разрыв около 180 мс (тулы здесь заглушены, других уступок нет) —
    тест красный.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))

    class SlowAsyncStore(AsyncMemoryThreadStore):
        def _make(name):  # noqa: N805
            async def method(self, *args, **kwargs):
                self.awaited += 1
                await asyncio.sleep(0.005)
                return getattr(self.inner, name)(*args, **kwargs)
            return method

        from gc_core.thread_store_base import CONTRACT_METHODS as _NAMES

        for _n in _NAMES:
            locals()[f"a{_n}"] = _make(_n)
        del _make, _n, _NAMES

    from gc_testkit import LoopPulse, assert_loop_was_free, quiet_gc_for_loop_timing

    store = SlowAsyncStore()
    # Сборщик глушится на окне: тест судит НАТИВНОСТЬ ожидания хранилища, а
    # пауза поколения 2 на куче набора (180–220 мс) кратно больше бюджета.
    # Пульс прогрет и тикает ДО сборки агента: иначе синхронный кусок сборки
    # до её первой уступки в замер не попадал бы.
    with quiet_gc_for_loop_timing():
        async with LoopPulse() as pulse:
            async with await AsyncAgent.create(cfg, thread_store=store) as agent:
                await agent.run("сделай что-нибудь", thread_id="t-sdk")

    assert store.awaited >= 10, f"хранилище звали всего {store.awaited} раз"
    assert_loop_was_free(pulse.gaps, tick=pulse.step,
                         label="хранилище с RTT 5 мс: ")


async def test_broken_store_fails_loudly_at_wiring(tmp_path, monkeypatch):
    """Негодное хранилище отвергается В МОМЕНТ ПРОВОДКИ, а не в глубине прогона.

    Прежде такой объект принимался молча, а падал на 630-й строке чужого файла
    с ``TypeError: cannot unpack non-iterable coroutine object`` — сообщение,
    по которому причину не найти.
    """
    from gc_core.thread_store_async import ThreadStoreContractError

    cfg = parity_config(tmp_path, monkeypatch)

    class NotAStore:
        pass

    with pytest.raises(ThreadStoreContractError) as err:
        await AsyncAgent.create(cfg, thread_store=NotAStore())

    text = str(err.value)
    assert "NotAStore" in text, "отказ не назвал класс встраивающего"
    assert "AsyncThreadStore" in text, "отказ не подсказал асинхронный контракт"
