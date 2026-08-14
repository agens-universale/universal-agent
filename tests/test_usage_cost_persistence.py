"""Фактическая стоимость переживает сохранение и чтение meta треда."""
import threading
import types

import agent_executor as ae
import manual_compaction as mc
from thread_store import load_thread_meta
from usage.account import CATEGORY_MAIN, MONEY_FIELD, UsageAccount


def test_cost_survives_meta_write_and_read():
    acc = UsageAccount()
    acc.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.0058, billed=True, currency="$")

    meta = {"token_usage": acc.to_meta()}
    restored = UsageAccount.from_meta(meta["token_usage"])

    assert restored.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 0.0058}
    assert restored.by_category[CATEGORY_MAIN]["billed_calls"] == 1
    assert restored.totals()[MONEY_FIELD] == {"$": 0.0058}


class _UsageState:
    """State-заглушка с .usage (реальный конструктор State тяжёл/WMI-хэнг)."""

    def __init__(self, **kw):
        self.messages = []
        self.backend_api_mode = "responses"
        self.shared = kw.get("shared")
        self.thread_id = kw.get("thread_id")
        self.dialog_steps_offset = 0
        self.usage = UsageAccount()

    def init_user_intent_log(self, v): pass
    def init_current_user_intent(self, v): pass
    def init_user_turn_seq(self, v): pass
    def infer_user_turn_seq_if_missing(self): pass


class _Coord:
    """Координатор-заглушка: force_compact_once — no-op, кумулятив не трогаем."""

    def __init__(self, **kw):
        self.state = kw["state"]

    def force_compact_once(self, **kw):
        pass

    def on_summary_applied(self, ev):
        pass


def test_cost_survives_manual_compaction(monkeypatch, tmp_path):
    """Ручной /compact: фактическая цена и billed_calls доживают до записанной meta."""
    baseline = UsageAccount()
    baseline.add(CATEGORY_MAIN, 1000, 100, 0, cost=0.0058, billed=True, currency="$")

    monkeypatch.setattr(
        mc, "ThreadSession",
        types.SimpleNamespace(from_config=lambda **kw: types.SimpleNamespace(
            history_messages=[{"role": "user", "content": "hi", "id": "u1"}],
            meta={"token_usage": baseline.to_meta()}, thread_id="t1", system_id="cli",
            threads_dir=str(tmp_path),
        )),
    )
    monkeypatch.setattr(mc, "repair_tool_call_pairing", lambda m, **kw: m)
    monkeypatch.setattr(mc, "RunSummaryCoordinator", _Coord)
    monkeypatch.setattr(mc, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(mc, "State", _UsageState)

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "chat_completions"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = threading.Lock()
    ex._state = None
    ex._stop_pending = False

    assert ex.compact_thread("t1", system="cli") is True

    saved = load_thread_meta(str(tmp_path), "cli", "t1") or {}
    persisted = UsageAccount.from_meta(saved.get("token_usage"))
    assert persisted.by_category[CATEGORY_MAIN][MONEY_FIELD] == {"$": 0.0058}
    assert persisted.by_category[CATEGORY_MAIN]["billed_calls"] == 1
