"""TDD-тесты in-session-rename: Шаг 2 (R6 — авто-запись last_actual в
_on_user_text_submitted) + Шаг 3 (R5 — OSC = display_name(meta.tui)).

Проверяем wiring в TraceScreen: что send сеет/обновляет last_actual через стор,
порядок записи meta ДО обновления OSC, и что OSC читает display_name, а не
_resumed_prompt. Чистая логика display_name/normalize — в test_session_title.py.
"""
import pytest


# ---------------------------------------------------------------------------
# Шаг 3: _update_terminal_title = display_name(meta.tui) + статус (R5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_osc_uses_display_name_not_resumed_prompt(tmp_path, monkeypatch):
    """При активной задаче OSC = display_name(meta.tui) + статус, не _resumed_prompt."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = None  # чтобы get_task_meta шёл через реальный стор ниже
        # _resumed_prompt намеренно другой — OSC должен игнорировать его при активной задаче
        scr._resumed_prompt = "сырой последний промпт"
        monkeypatch.setattr(scr, "_session_status_word", lambda: "completed")

        # отдельный стор с meta {tui: {last_actual: "реализуй спеку X"}}
        store = _FakeStore({"tui": {"last_actual": "реализуй спеку X"}})
        monkeypatch.setattr(scr, "_store", store)

        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ✓ реализуй спеку X", calls
        assert "сырой" not in calls[-1]


@pytest.mark.asyncio
async def test_osc_title_priority_over_last_actual(tmp_path, monkeypatch):
    """Ручной title приоритетнее last_actual в OSC."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._resumed_prompt = "старый промпт"
        monkeypatch.setattr(scr, "_session_status_word", lambda: "running")
        store = _FakeStore({"tui": {"title": "parser bugfix", "last_actual": "реализуй спеку"}})
        monkeypatch.setattr(scr, "_store", store)

        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ▸ parser bugfix", calls


@pytest.mark.asyncio
async def test_osc_empty_meta_fallback_unnamed(tmp_path, monkeypatch):
    """Пустой meta.tui → OSC показывает «Без названия» (не _resumed_prompt)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._resumed_prompt = "что-то"
        monkeypatch.setattr(scr, "_session_status_word", lambda: "completed")
        store = _FakeStore({})
        monkeypatch.setattr(scr, "_store", store)

        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ✓ Без названия", calls


@pytest.mark.asyncio
async def test_osc_no_active_task_ignores_meta(tmp_path, monkeypatch):
    """Без активной задачи OSC = базовый бренд (display_name не участвует)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = None
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode", calls


# ---------------------------------------------------------------------------
# Шаг 2: _on_user_text_submitted — R6 (auto last_actual) + порядок meta→OSC
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seed_first_send_short_writes_last_actual(tmp_path, monkeypatch):
    """Первый send (короткий, last_actual пуст) → seed: last_actual записан."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: None, raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({})
        monkeypatch.setattr(scr, "_store", store)
        monkeypatch.setattr(scr, "_session_status_word", lambda: "running")

        scr._on_user_text_submitted("ты тут?")

        assert store.patches == [{"last_actual": "ты тут?"}], store.patches
        assert scr._resumed_prompt == "ты тут?"  # остаётся сырым


@pytest.mark.asyncio
async def test_seed_multiline_collapsed(tmp_path, monkeypatch):
    """Многострочный/пробельный send → в meta одна строка collapse, ≤ 200."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: None, raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({})
        monkeypatch.setattr(scr, "_store", store)

        scr._on_user_text_submitted("реализуй   спеку\n  по  шагам   ")

        assert store.patches == [{"last_actual": "реализуй спеку по шагам"}], store.patches
        assert len(store.patches[0]["last_actual"]) <= 200


@pytest.mark.asyncio
async def test_update_long_send_when_last_actual_set(tmp_path, monkeypatch):
    """len(norm) > 30 при заданном last_actual → обновлён."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: None, raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"last_actual": "ты тут?"}})
        monkeypatch.setattr(scr, "_store", store)

        scr._on_user_text_submitted("реализуй спеку переименования сессии полностью")

        assert store.patches == [{"last_actual": "реализуй спеку переименования сессии полностью"}], store.patches


@pytest.mark.asyncio
async def test_short_send_does_not_update_when_set(tmp_path, monkeypatch):
    """len(norm) ≤ 30 при заданном last_actual → НЕ тронут."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: None, raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"last_actual": "реализуй спеку переименования"}})
        monkeypatch.setattr(scr, "_store", store)

        scr._on_user_text_submitted("ок")

        assert store.patches == [], store.patches  # не писали
        assert store.base["tui"]["last_actual"] == "реализуй спеку переименования"


@pytest.mark.asyncio
async def test_whitespace_only_send_skips(tmp_path, monkeypatch):
    """Чисто-пробельный send → normalize пуст → last_actual не тронут."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: None, raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"last_actual": "старое"}})
        monkeypatch.setattr(scr, "_store", store)

        scr._on_user_text_submitted("   \n\t  ")

        assert store.patches == [], store.patches


@pytest.mark.asyncio
async def test_no_active_task_skips_last_actual(tmp_path, monkeypatch):
    """Без активной задачи last_actual не пишется (но _resumed_prompt ставится)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: None, raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = None
        store = _FakeStore({})
        monkeypatch.setattr(scr, "_store", store)

        scr._on_user_text_submitted("реализуй спеку переименования сессии полностью")

        assert store.patches == [], store.patches
        assert scr._resumed_prompt == "реализуй спеку переименования сессии полностью"


@pytest.mark.asyncio
async def test_meta_written_before_osc(tmp_path, monkeypatch):
    """Порядок R6: запись meta → затем _update_terminal_title (OSC видит свежее имя)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    osc_calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: osc_calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({})
        monkeypatch.setattr(scr, "_store", store)
        monkeypatch.setattr(scr, "_session_status_word", lambda: "running")

        order = []

        real_update = store.update_tui_meta

        def track_update(tid, patch, **k):
            order.append(("meta", patch["last_actual"]))
            return real_update(tid, patch, **k)

        def track_osc(text, **k):
            order.append(("osc", text))
            osc_calls.append(text)

        monkeypatch.setattr(store, "update_tui_meta", track_update, raising=False)
        monkeypatch.setattr(main_mod, "set_terminal_title", track_osc, raising=False)

        scr._on_user_text_submitted("реализуй спеку переименования сессии полностью")

        # первая запись — meta, вторая — OSC
        assert order[0][0] == "meta", order
        assert order[1][0] == "osc", order
        assert "реализуй спеку" in order[1][1]


# ---------------------------------------------------------------------------
# helper: лёгкий fake store, имитирующий read-merge-write update_tui_meta
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, meta: dict):
        self.base = meta
        self.patches: list[dict] = []

    def get_task_meta(self, task_id, system=None):
        return self.base

    def update_tui_meta(self, task_id, patch, system=None):
        self.patches.append(dict(patch))
        tui = dict(self.base.get("tui") or {})
        tui.update(patch)
        self.base["tui"] = tui


# ---------------------------------------------------------------------------
# Шаг 4: _rename_active_session (R2) + _on_rename_result (R3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rename_active_session_opens_with_display_name(tmp_path, monkeypatch):
    """R2: Ctrl+R → RenameScreen(current=display_name(meta.tui)) + _on_rename_result."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.rename import RenameScreen

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"last_actual": "реализуй спеку"}})
        monkeypatch.setattr(scr, "_store", store)

        pushed = {}

        def fake_push(screen, callback=None, **k):
            pushed["screen"] = screen
            pushed["cb"] = callback

        monkeypatch.setattr(scr.app, "push_screen", fake_push, raising=False)

        scr._rename_active_session()

        assert isinstance(pushed["screen"], RenameScreen), pushed
        assert pushed["screen"]._current == "реализуй спеку", pushed["screen"]._current
        assert pushed["cb"] is not None


@pytest.mark.asyncio
async def test_rename_active_session_title_priority(tmp_path, monkeypatch):
    """R2: при заданном ручном title current=title (приоритет над last_actual)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.rename import RenameScreen

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"title": "parser bugfix", "last_actual": "реализуй спеку"}})
        monkeypatch.setattr(scr, "_store", store)

        pushed = {}

        def fake_push(screen, callback=None, **k):
            pushed["screen"] = screen

        monkeypatch.setattr(scr.app, "push_screen", fake_push, raising=False)

        scr._rename_active_session()

        assert pushed["screen"]._current == "parser bugfix", pushed["screen"]._current


@pytest.mark.asyncio
async def test_rename_active_session_no_active_task_noop(tmp_path, monkeypatch):
    """R2: без активной задачи → push_screen не вызван."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = None
        store = _FakeStore({})
        monkeypatch.setattr(scr, "_store", store)

        pushed = []
        monkeypatch.setattr(scr.app, "push_screen", lambda *a, **k: pushed.append(k), raising=False)

        scr._rename_active_session()

        assert pushed == [], pushed


@pytest.mark.asyncio
async def test_rename_active_session_no_store_noop(tmp_path, monkeypatch):
    """R2: без стора → push_screen не вызван (гейт _active_task_id + _store)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = None

        pushed = []
        monkeypatch.setattr(scr.app, "push_screen", lambda *a, **k: pushed.append(k), raising=False)

        scr._rename_active_session()

        assert pushed == [], pushed


@pytest.mark.asyncio
async def test_rename_result_writes_title(tmp_path, monkeypatch):
    """R3: value=\"имя\" → update_tui_meta(tid,{\"title\":\"имя\"}) + OSC обновлён."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    osc_calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: osc_calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"last_actual": "реализуй спеку"}})
        monkeypatch.setattr(scr, "_store", store)
        monkeypatch.setattr(scr, "_session_status_word", lambda: "running")

        scr._on_rename_result("parser bugfix")

        assert store.patches == [{"title": "parser bugfix"}], store.patches
        # OSC обновлён: title приоритетнее last_actual
        assert any("parser bugfix" in t for t in osc_calls), osc_calls


@pytest.mark.asyncio
async def test_rename_result_empty_resets_title(tmp_path, monkeypatch):
    """R3: value=\"\" → title=\"\" (сброс к авто-имени); OSC возвращается к last_actual."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    osc_calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: osc_calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"title": "старое имя", "last_actual": "реализуй спеку"}})
        monkeypatch.setattr(scr, "_store", store)
        monkeypatch.setattr(scr, "_session_status_word", lambda: "running")

        scr._on_rename_result("")

        assert store.patches == [{"title": ""}], store.patches
        # OSC вернулся к last_actual (title сброшен)
        assert any("реализуй спеку" in t for t in osc_calls), osc_calls
        assert not any("старое имя" in t for t in osc_calls), osc_calls


@pytest.mark.asyncio
async def test_rename_result_none_noop(tmp_path, monkeypatch):
    """R3: value=None (Esc) → ничего не пишется, OSC не тронут дополнительно."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    osc_calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: osc_calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        store = _FakeStore({"tui": {"last_actual": "реализуй спеку"}})
        monkeypatch.setattr(scr, "_store", store)

        scr._on_rename_result(None)

        assert store.patches == [], store.patches


# ---------------------------------------------------------------------------
# helper: лёгкий fake key event для on_key (ctrl+r алиасы — синтетика)
# ---------------------------------------------------------------------------

class _FakeKey:
    def __init__(self, key: str):
        self.key = key
        self.stopped = False
        self.prevented = False

    def stop(self) -> None:
        self.stopped = True

    def prevent_default(self) -> None:
        self.prevented = True


# ---------------------------------------------------------------------------
# Шаг 5: R1 — алиасы Ctrl+R в TraceScreen.on_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1_ctrl_r_renames_active_session(tmp_path, monkeypatch):
    """R1: ctrl+r при активной задаче+сторе → _rename_active_session + event.stop."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        monkeypatch.setattr(scr, "_store", _FakeStore({"tui": {"last_actual": "реализуй спеку"}}))

        called = []
        monkeypatch.setattr(scr, "_rename_active_session", lambda: called.append(1))

        ev = _FakeKey("ctrl+r")
        scr.on_key(ev)

        assert called == [1], called
        assert ev.stopped is True
        assert ev.prevented is True


@pytest.mark.asyncio
async def test_r1_ctrl_capital_r_renames_active_session(tmp_path, monkeypatch):
    """R1: ctrl+R — алиас регистра."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        monkeypatch.setattr(scr, "_store", _FakeStore({"tui": {"last_actual": "x"}}))
        called = []
        monkeypatch.setattr(scr, "_rename_active_session", lambda: called.append(1))
        ev = _FakeKey("ctrl+R")
        scr.on_key(ev)
        assert called == [1], called
        assert ev.stopped is True


@pytest.mark.asyncio
async def test_r1_ctrl_cyrillic_k_lower_renames(tmp_path, monkeypatch):
    """R1: ctrl+к (русская раскладка, физ. кнопка R) → rename."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        monkeypatch.setattr(scr, "_store", _FakeStore({"tui": {"last_actual": "x"}}))
        called = []
        monkeypatch.setattr(scr, "_rename_active_session", lambda: called.append(1))
        ev = _FakeKey("ctrl+к")
        scr.on_key(ev)
        assert called == [1], called
        assert ev.stopped is True


@pytest.mark.asyncio
async def test_r1_ctrl_cyrillic_k_upper_renames(tmp_path, monkeypatch):
    """R1: ctrl+К (русская раскладка, Shift+R) → rename."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        monkeypatch.setattr(scr, "_store", _FakeStore({"tui": {"last_actual": "x"}}))
        called = []
        monkeypatch.setattr(scr, "_rename_active_session", lambda: called.append(1))
        ev = _FakeKey("ctrl+К")
        scr.on_key(ev)
        assert called == [1], called
        assert ev.stopped is True


@pytest.mark.asyncio
async def test_r1_ctrl_r_no_active_task_noop(tmp_path, monkeypatch):
    """R1: ctrl+r без активной задачи → rename не вызван, event не остановлен."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = None
        monkeypatch.setattr(scr, "_store", _FakeStore({"tui": {}}))
        called = []
        monkeypatch.setattr(scr, "_rename_active_session", lambda: called.append(1))
        ev = _FakeKey("ctrl+r")
        scr.on_key(ev)
        assert called == [], called
        assert ev.stopped is False


@pytest.mark.asyncio
async def test_r1_ctrl_r_no_store_noop(tmp_path, monkeypatch):
    """R1: ctrl+r без стора → rename не вызван, event не остановлен."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        monkeypatch.setattr(scr, "_store", None)
        called = []
        monkeypatch.setattr(scr, "_rename_active_session", lambda: called.append(1))
        ev = _FakeKey("ctrl+r")
        scr.on_key(ev)
        assert called == [], called
        assert ev.stopped is False
