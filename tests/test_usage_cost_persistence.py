"""Фактическая стоимость переживает сохранение и чтение meta треда."""

import threading
import types

import gc_core.agent_executor as ae
import gc_core.manual_compaction as mc
from gc_core.thread_store import load_thread_meta
from gc_core.thread_store_async import athread_store
from gc_core.thread_store_file import FileThreadStore
from gc_core.usage.account import CATEGORY_MAIN, MONEY_FIELD, UsageAccount
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def test_cost_survives_meta_write_and_read():
    acc = UsageAccount()
    acc.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=0.0058, billed=True, currency="$"
    )

    meta = {"token_usage": acc.to_meta()}
    restored = UsageAccount.from_meta(meta["token_usage"])

    assert restored.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 0.0058}
    assert restored.by_category[CATEGORY_MAIN]["billed_calls"] == 1
    assert restored.totals()[MONEY_FIELD] == {"$": 0.0058}


class _UsageState:
    # Прод строит состояние фабрикой: сбор данных о проекте — файловый
    # ввод-вывод, а конструктор ждать не умеет. Дабл повторяет форму.
    @classmethod
    async def acreate(cls, **kw):
        return cls(**kw)

    """State-заглушка с .usage (реальный конструктор State тяжёл/WMI-хэнг)."""

    def __init__(self, **kw):
        self.messages = []
        self.backend_api_mode = "responses"
        self.shared = kw.get("shared")
        self.thread_id = kw.get("thread_id")
        self.dialog_steps_offset = 0
        self.usage = UsageAccount()

    def init_user_intent_log(self, v):
        pass

    def init_current_user_intent(self, v):
        pass

    def init_user_turn_seq(self, v):
        pass

    def infer_user_turn_seq_if_missing(self):
        pass


class _Coord:
    """Координатор-заглушка: компакция — no-op, кумулятив не трогаем.

    Двойник нужен асинхронный: ``manual_compaction.acompact_thread`` зовёт
    ``aforce_compact_once``. Синхронное имя оставлено — на нём сидят прямые
    вызывающие вне цикла.
    """

    def __init__(self, **kw):
        self.state = kw["state"]

    async def aforce_compact_once(self, **kw):
        pass

    def force_compact_once(self, **kw):
        pass

    def on_summary_applied(self, ev):
        pass


def test_cost_survives_manual_compaction(monkeypatch, tmp_path):
    """Ручной /compact: фактическая цена и billed_calls доживают до записанной meta."""
    baseline = UsageAccount()
    baseline.add(
        CATEGORY_MAIN, 1000, 100, 0, cost=0.0058, billed=True, currency="$"
    )

    _store = FileThreadStore(str(tmp_path))

    async def _afrom_config(**kw):
        return types.SimpleNamespace(
            history_messages=[{"role": "user", "content": "hi", "id": "u1"}],
            meta={"token_usage": baseline.to_meta()},
            thread_id="t1",
            system_id="cli",
            threads_dir=str(tmp_path),
            # Ручная компакция пишет token_usage через хранилище сессии — и
            # ходит туда через асинхронный порт: тред может лежать в базе.
            store=_store,
            port=athread_store(_store),
        )

    monkeypatch.setattr(
        mc,
        "ThreadSession",
        types.SimpleNamespace(afrom_config=_afrom_config),
    )
    monkeypatch.setattr(mc, "repair_tool_call_pairing", lambda m, **kw: m)
    monkeypatch.setattr(mc, "RunSummaryCoordinator", _Coord)
    monkeypatch.setattr(mc, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(mc, "State", _UsageState)

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = fake_ctx(
        {
            "agent.model": "M",
            "agent.max_steps": 30,
            "agent.system_prompt_id": "x",
            "backend.api_mode": "chat_completions",
        },
        tools_spec=[],
        tools_description=[],
    )
    ex._lock = threading.Lock()
    ex._state = None
    ex._stop_pending = False

    assert run_coro(ex.acompact_thread("t1", system="cli")) is True

    saved = load_thread_meta(str(tmp_path), "cli", "t1") or {}
    persisted = UsageAccount.from_meta(saved.get("token_usage"))
    assert persisted.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 0.0058}
    assert persisted.by_category[CATEGORY_MAIN]["billed_calls"] == 1
