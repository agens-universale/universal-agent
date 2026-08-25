"""compact_thread: грузит тред + форс-сжатие через координатор."""
import types

from gc_core.thread_store_file import FileThreadStore

from gc_core import agent_executor as ae
from gc_core import manual_compaction as mc


def test_compact_thread_calls_force_compact(monkeypatch, tmp_path):
    captured = {}

    class _TS:
        history_messages = [{"role": "user", "content": "привет", "id": "u1"}]
        meta = {}
        thread_id = "t1"
        system_id = "cli"
        threads_dir = str(tmp_path)

    class _Coord:
        def __init__(self, **kw):
            captured["coord_state"] = kw["state"]
        async def aforce_compact_once(self, **kw):
            captured["forced"] = True
        def on_summary_applied(self, ev):
            pass

    class _FakeState:
        # Мокаем State, чтобы НЕ дёргать реальный конструктор (он зовёт
        # get_project_data → WMI-хэнг на Windows). Capture messages.
        def __init__(self, **kw):
            self.messages = []
            self.backend_api_mode = "responses"
            self.shared = kw.get("shared")
            self.thread_id = kw.get("thread_id")
            self.dialog_steps_offset = 0
        def init_user_intent_log(self, v): pass
        def init_current_user_intent(self, v): pass
        def init_user_turn_seq(self, v): pass
        def infer_user_turn_seq_if_missing(self): pass

    monkeypatch.setattr(mc, "ThreadSession", types.SimpleNamespace(from_config=lambda **kw: _TS()))
    monkeypatch.setattr(mc, "repair_tool_call_pairing", lambda m, **kw: m)
    monkeypatch.setattr(mc, "RunSummaryCoordinator", _Coord)
    monkeypatch.setattr(mc, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(mc, "State", _FakeState)

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "chat_completions"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    ex._state = None
    ex._stop_pending = False

    ok = ex.compact_thread("t1", system="cli")
    assert ok is True
    assert captured.get("forced") is True
    # история треда загружена в state, по которому идёт сжатие
    assert any(m.get("content") == "привет" for m in captured["coord_state"].messages)


def _make_compact_executor(monkeypatch, tmp_path, *, baseline_meta, coord_cls, state_cls):
    import threading
    # Логика compact_thread живёт в manual_compaction (mc) — патчим ЕГО namespace,
    # а не agent_executor (тот лишь делегирует).
    monkeypatch.setattr(
        mc, "ThreadSession",
        types.SimpleNamespace(from_config=lambda **kw: types.SimpleNamespace(
            history_messages=[{"role": "user", "content": "hi", "id": "u1"}],
            meta=baseline_meta, thread_id="t1", system_id="cli",
            threads_dir=str(tmp_path),
            # Хранилище: ручная компакция пишет token_usage через него, а не
            # через модульные функции — иначе у встраивающего с базой эта
            # запись уходила бы на диск мимо остального треда.
            store=FileThreadStore(str(tmp_path)),
        )),
    )
    monkeypatch.setattr(mc, "repair_tool_call_pairing", lambda m, **kw: m)
    monkeypatch.setattr(mc, "RunSummaryCoordinator", coord_cls)
    monkeypatch.setattr(mc, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(mc, "State", state_cls)

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "chat_completions"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = threading.Lock()
    ex._state = None
    ex._stop_pending = False
    return ex


class _UsageState:
    """State-заглушка с .usage (реальный конструктор State тяжёл/WMI-хэнг)."""
    def __init__(self, **kw):
        from gc_core.usage.account import UsageAccount
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


def test_compact_thread_persists_summary_usage(monkeypatch, tmp_path):
    """Ручной /compact: summary-токены должны попасть в персистентный token_usage
    поверх гидрированного кумулятива (регресс: терялись целиком)."""
    from gc_core.usage.account import UsageAccount
    baseline = UsageAccount(); baseline.add("main", 600000, 6000)
    captured = {}

    class _Coord:
        def __init__(self, **kw): self.state = kw["state"]
        async def aforce_compact_once(self, **kw):
            # имитируем summary LLM-вызов внутри компакции
            self.state.usage.add("summary", 30000, 500)
        def on_summary_applied(self, ev): pass

    ex = _make_compact_executor(
        monkeypatch, tmp_path,
        baseline_meta={"token_usage": baseline.to_meta()},
        coord_cls=_Coord, state_cls=_UsageState,
    )
    assert ex.compact_thread("t1", system="cli") is True

    # Читаем РЕАЛЬНО сохранённую meta и проверяем TOP-LEVEL token_usage — именно
    # его гидрирует run() (регресс: писали в meta["tui"], run() его не видит).
    from gc_core.thread_store import load_thread_meta
    saved = load_thread_meta(str(tmp_path), "cli", "t1") or {}
    persisted = UsageAccount.from_meta(saved.get("token_usage"))
    assert persisted.by_category["main"]["input"] == 600000     # кумулятив сохранён
    assert persisted.by_category["summary"]["input"] == 30000   # summary добавлен
    assert persisted.by_category["summary"]["output"] == 500


def test_compact_thread_updates_live_state_for_footer(monkeypatch, tmp_path):
    """После /compact live-state (что читает футер) должен отражать summary."""
    from gc_core.usage.account import UsageAccount

    class _Coord:
        def __init__(self, **kw): self.state = kw["state"]
        async def aforce_compact_once(self, **kw):
            self.state.usage.add("summary", 30000, 500)
        def on_summary_applied(self, ev): pass

    ex = _make_compact_executor(
        monkeypatch, tmp_path, baseline_meta={"token_usage": {}},
        coord_cls=_Coord, state_cls=_UsageState,
    )
    live = _UsageState(thread_id="t1")
    ex._state = live

    ex.compact_thread("t1", system="cli")
    assert ex._state.usage.by_category["summary"]["input"] == 30000


def test_compact_thread_noop_in_responses(monkeypatch, tmp_path):
    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "responses"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    ex._state = None
    assert ex.compact_thread("t1", system="cli") is False
