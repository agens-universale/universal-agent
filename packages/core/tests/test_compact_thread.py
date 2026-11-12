"""compact_thread: грузит тред + форс-сжатие через координатор."""

from gc_core import agent_executor as ae
from gc_core import manual_compaction as mc
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey, Usage
from gc_core.threads.session_writer import SessionWriter
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx

KEY = ThreadKey(system="cli", thread="t1")


def _seed(tmp_path, content: str, usage: dict | None = None) -> None:
    """Тред на диске: одно сообщение пользователя и кумулятив расхода."""

    async def seed():
        writer = SessionWriter(
            FileThreadStore(str(tmp_path)), KEY, owner="seed"
        )
        await writer.acommit(
            append_messages=[{"role": "user", "content": content, "id": "u1"}],
            set_usage=Usage.model_validate(usage or {}),
        )
        await writer.arelease()

    run_coro(seed())


def _executor(tmp_path):
    import threading

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = fake_ctx(
        {
            "agent.model": "M",
            "agent.max_steps": 30,
            "agent.system_prompt_id": "x",
            "backend.api_mode": "chat_completions",
            "paths.threads_data_path": str(tmp_path),
        },
        tools_spec=[],
        tools_description=[],
    )
    ex._lock = threading.Lock()
    ex._state = None
    ex._stop_pending = False
    return ex


def test_compact_thread_calls_force_compact(monkeypatch, tmp_path):
    captured = {}

    _seed(tmp_path, "привет")

    class _Coord:
        def __init__(self, **kw):
            captured["coord_state"] = kw["state"]

        async def aforce_compact_once(self, **kw):
            captured["forced"] = True

        def on_summary_applied(self, ev):
            pass

    class _FakeState:
        # Мокаем State, чтобы НЕ дёргать реальный конструктор. Прежняя причина
        # («он зовёт get_project_data → WMI-хэнг на Windows») ушла: сбор данных
        # о проекте переехал в `acreate`, конструктор в файловую систему не
        # ходит. Мок остаётся ради перехвата сообщений.
        @classmethod
        async def acreate(cls, **kw):
            return cls(**kw)

        def __init__(self, **kw):
            self.messages = []
            self.backend_api_mode = "responses"
            self.shared = kw.get("shared")
            self.thread_id = kw.get("thread_id")
            self.dialog_steps_offset = 0

        def init_user_intent_log(self, v):
            pass

        def init_current_user_intent(self, v):
            pass

        def init_user_turn_seq(self, v):
            pass

        def infer_user_turn_seq_if_missing(self):
            pass

    monkeypatch.setattr(mc, "RunSummaryCoordinator", _Coord)
    monkeypatch.setattr(mc, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(mc, "State", _FakeState)

    ex = _executor(tmp_path)
    ok = run_coro(ex.acompact_thread("t1", system="cli"))
    assert ok is True
    assert captured.get("forced") is True
    # история треда загружена в state, по которому идёт сжатие
    assert any(
        m.get("content") == "привет" for m in captured["coord_state"].messages
    )


def _make_compact_executor(
    monkeypatch, tmp_path, *, baseline_usage, coord_cls, state_cls
):
    # Логика compact_thread живёт в manual_compaction (mc) — патчим ЕГО namespace,
    # а не agent_executor (тот лишь делегирует).
    _seed(tmp_path, "hi", baseline_usage)
    monkeypatch.setattr(mc, "RunSummaryCoordinator", coord_cls)
    monkeypatch.setattr(mc, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(mc, "State", state_cls)
    return _executor(tmp_path)


class _UsageState:
    @classmethod
    async def acreate(cls, **kw):
        return cls(**kw)

    """State-заглушка с .usage (реальный конструктор State тяжёл/WMI-хэнг)."""

    def __init__(self, **kw):
        from gc_core.usage.account import UsageAccount

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


def test_compact_thread_persists_summary_usage(monkeypatch, tmp_path):
    """Ручной /compact: summary-токены должны попасть в персистентный token_usage
    поверх гидрированного кумулятива (регресс: терялись целиком)."""
    from gc_core.usage.account import UsageAccount

    baseline = UsageAccount()
    baseline.add("main", 600000, 6000)
    captured = {}

    class _Coord:
        def __init__(self, **kw):
            self.state = kw["state"]

        async def aforce_compact_once(self, **kw):
            # имитируем summary LLM-вызов внутри компакции
            self.state.usage.add("summary", 30000, 500)

        def on_summary_applied(self, ev):
            pass

    ex = _make_compact_executor(
        monkeypatch,
        tmp_path,
        baseline_usage=baseline.to_meta(),
        coord_cls=_Coord,
        state_cls=_UsageState,
    )
    assert run_coro(ex.acompact_thread("t1", system="cli")) is True

    # Читаем РЕАЛЬНО сохранённый расход треда — именно его гидрирует run()
    # (регресс: писали в секцию TUI, run() её не видит).
    record = run_coro(FileThreadStore(str(tmp_path)).get_thread(KEY))
    persisted = UsageAccount.from_meta(record.usage.model_dump())
    assert (
        persisted.by_category["main"]["input"] == 600000
    )  # кумулятив сохранён
    assert (
        persisted.by_category["summary"]["input"] == 30000
    )  # summary добавлен
    assert persisted.by_category["summary"]["output"] == 500


def test_compact_thread_updates_live_state_for_footer(monkeypatch, tmp_path):
    """После /compact live-state (что читает футер) должен отражать summary."""

    class _Coord:
        def __init__(self, **kw):
            self.state = kw["state"]

        async def aforce_compact_once(self, **kw):
            self.state.usage.add("summary", 30000, 500)

        def on_summary_applied(self, ev):
            pass

    ex = _make_compact_executor(
        monkeypatch,
        tmp_path,
        baseline_usage={},
        coord_cls=_Coord,
        state_cls=_UsageState,
    )
    live = _UsageState(thread_id="t1")
    ex._state = live

    run_coro(ex.acompact_thread("t1", system="cli"))
    assert ex._state.usage.by_category["summary"]["input"] == 30000


def test_compact_thread_noop_in_responses(monkeypatch, tmp_path):
    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = fake_ctx(
        {
            "agent.model": "M",
            "agent.max_steps": 30,
            "agent.system_prompt_id": "x",
            "backend.api_mode": "responses",
        },
        tools_spec=[],
        tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    ex._state = None
    assert run_coro(ex.acompact_thread("t1", system="cli")) is False
