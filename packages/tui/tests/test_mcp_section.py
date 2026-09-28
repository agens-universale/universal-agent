import pytest

pytest.importorskip("textual")

from gc_tui.services.mcp_manager import McpRow, McpState


class FakeManager:
    def __init__(self, rows, states=None):
        self._rows = rows
        self._states = states or {}
        self.calls = []

    def rows(self):
        return list(self._rows)

    def state(self, name):
        return self._states.get(name, McpState(False, None, None))

    def wire(self, name):
        self.calls.append(("wire", name))
        return True

    def unwire(self, name):
        self.calls.append(("unwire", name))
        return True

    def install(self, name):
        self.calls.append(("install", name))
        return 0

    def remove(self, name):
        self.calls.append(("remove", name))
        return 0


def _rows():
    return [
        McpRow("office", wired=True, manual=False),
        McpRow("playwright", wired=False, manual=False),
        McpRow("corp-jira", wired=False, manual=True),
    ]


async def test_section_renders_rows_and_manual_block():
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=FakeManager(_rows()))
        await app.mount(ed)
        await pilot.pause()
        from textual.widgets import Static

        blob = " ".join(str(w.render()) for w in ed.query(Static))
        assert "[●] office" in blob  # подключён — закрашенный чекбокс
        assert "[ ] playwright" in blob  # не подключён — пустой
        assert "corp-jira" in blob and "вручную в config.yml" in blob
        assert "▸" in blob  # курсор на первой управляемой строке


async def test_section_frame_hugs_content():
    """Регресс: McpSectionEditor — Vertical (дефолт height:1fr) растягивал рамку на
    весь экран (шов CSS-правила height:auto — новый редактор не был добавлен).
    height:auto → высота ≈ число строк, не заполнение экрана (~24)."""
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=FakeManager(_rows()))
        await app.mount(ed)
        await pilot.pause()
        # контент — 2 управляемых + разделитель действий + 2 действия + разделитель + 1 ручная
        # + воздух set-spaced между строками ≈ 12 строк, НЕ весь экран
        assert ed.size.height <= 14


async def test_section_worker_fills_statuses():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    states = {
        "office": McpState(True, "0.3.1", None),
        "playwright": McpState(False, None, None),
    }
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=FakeManager(_rows(), states))
        await app.mount(ed)
        await wait_until(
            pilot,
            lambda: "office" in ed._states and "playwright" in ed._states,
            "states loaded",
        )
        from textual.widgets import Static

        blob = " ".join(str(w.render()) for w in ed.query(Static))
        assert "подключён · v0.3.1" in blob
        assert "не установлен" in blob


async def test_toggle_unwires_connected():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("office"), "loaded")
        ed.toggle()  # курсор на office (wired) → unwire
        assert ("unwire", "office") in mgr.calls


async def test_toggle_wires_installed_disabled():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(True, "0.2.0", None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("computer"), "loaded")
        ed.toggle()  # installed, не wired → wire
        assert ("wire", "computer") in mgr.calls


async def test_toggle_noop_when_not_installed():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(False, None, None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("computer"), "loaded")
        hint = ed.toggle()  # не установлен → no-op + хинт про Enter
        assert mgr.calls == []  # ни wire, ни unwire
        assert (
            hint is not None and "Enter" in hint
        )  # подсказка, как установить


def test_toggle_noop_before_status_arrives():
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(True, "0.2.0", None)})
    # Не монтируем → воркер статусов не запускается, статус детерминированно не пришёл
    ed = McpSectionEditor(manager=mgr)
    assert not ed.status_ready("computer")
    hint = ed.toggle()  # st is None → no-op молча (без хинта)
    assert mgr.calls == []
    assert hint is None  # статус ещё грузится — молчим


async def test_enter_installs_not_installed():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("playwright", wired=False, manual=False)]
    mgr = FakeManager(rows, {"playwright": McpState(False, None, None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        assert ed.begin_enter() is True
        assert ed.awaiting_confirm is True
        assert mgr.calls == []  # confirm показан, ничего ещё не поставлено
        result = ed.confirm_pending()
        assert ("install", "playwright") in mgr.calls
        assert result == [("playwright", True)]
        assert ed.awaiting_confirm is False


async def test_ctrl_d_confirm_then_remove():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(True, "0.2.0", None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("computer"), "loaded")
        assert ed.request_remove() is True
        assert ed.awaiting_confirm is True
        from textual.widgets import Static

        blob = " ".join(str(w.render()) for w in ed.query(Static))
        assert "Удалить пакет computer" in blob and "Enter — удалить" in blob
        assert ed.confirm_pending() == []
        assert ("remove", "computer") in mgr.calls
        assert ed.awaiting_confirm is False


async def test_ctrl_d_on_not_installed_is_noop():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("playwright", wired=False, manual=False)]
    mgr = FakeManager(rows, {"playwright": McpState(False, None, None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        assert ed.request_remove() is False
        assert ed.awaiting_confirm is False


def test_ctrl_d_noop_before_status_arrives():
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(True, "0.2.0", None)})
    # Не монтируем → воркер статусов не запускается, статус детерминированно не пришёл
    ed = McpSectionEditor(manager=mgr)
    assert not ed.status_ready("computer")
    assert (
        ed.request_remove() is False
    )  # st is None → no-op, даже installed в manager
    assert ed.awaiting_confirm is False
    assert mgr.calls == []


async def test_ctrl_d_esc_cancels_without_remove():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(True, "0.2.0", None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("computer"), "loaded")
        assert ed.request_remove() is True
        ed.cancel_pending()
        await pilot.pause()
        assert ed.awaiting_confirm is False
        assert mgr.calls == []
        from textual.widgets import Static

        blob = " ".join(str(w.render()) for w in ed.query(Static))
        assert "Удалить пакет" not in blob


async def test_action_rows_visibility():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        ed.refresh_action_rows()
        from textual.widgets import Static

        blob = " ".join(str(w.render()) for w in ed.query(Static))
        assert "Установить все недостающие" in blob  # playwright не установлен
        assert "playwright" in blob


async def test_move_reaches_action_rows():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    # office wired + update available → ⟳; playwright не установлен → ⤓
    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", "0.4.0"),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot,
            lambda: (
                ed.status_ready("office") and ed.status_ready("playwright")
            ),
            "loaded",
        )
        assert ed._action_kinds() == ["missing", "update"]
        assert ed.current_action is None  # старт на управляемой строке
        ed.move(1)  # office → playwright
        assert ed.current_action is None
        ed.move(1)  # playwright → ⤓ (missing)
        assert ed.current_action == "missing"
        assert ed.current_row is None  # на строке-действии row нет
        ed.move(1)  # ⤓ → ⟳ (update)
        assert ed.current_action == "update"
        assert ed.current_row is None


async def test_selected_class_follows_cursor():
    """Регресс: строка курсора не подсвечивалась фоном — _refresh_rows обновлял только
    текст, не класс `selected` (см. .mcp-row-managed.selected в styles.tcss)."""
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        rows = list(ed.query(".mcp-row-managed"))
        assert rows[0].has_class("selected")
        assert not rows[1].has_class("selected")
        ed.move(1)  # office → playwright
        assert not rows[0].has_class("selected")
        assert rows[1].has_class("selected")


async def test_selected_class_follows_action_cursor():
    """Курсор на строке-действии — тоже с классом `selected` (то же правило CSS,
    .mcp-action-row.selected); управляемая строка при этом теряет класс."""
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", "0.4.0"),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot,
            lambda: (
                ed.status_ready("office") and ed.status_ready("playwright")
            ),
            "loaded",
        )
        ed.move(1)  # office → playwright
        ed.move(1)  # playwright → ⤓ (missing)
        assert ed.current_action == "missing"
        rows = list(ed.query(".mcp-row-managed"))
        assert not any(w.has_class("selected") for w in rows)
        actions = list(ed.query(".mcp-action-row"))
        assert actions[0].has_class("selected")
        assert not actions[1].has_class("selected")


async def test_action_rows_updated_in_place_on_move():
    """Минор: move() обновляет строки-действия НА МЕСТЕ (те же виджеты), не пересоздаёт
    remove+mount — иначе при частых move возможно накопление (async-remove)."""
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", "0.4.0"),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot,
            lambda: (
                ed.status_ready("office") and ed.status_ready("playwright")
            ),
            "loaded",
        )
        assert ed._action_kinds() == ["missing", "update"]
        before = list(ed.query(".mcp-action-row"))
        assert len(before) == 2
        ed.move(1)
        ed.move(1)
        ed.move(1)
        ed.move(1)  # прогон курсора через строки-действия
        after = list(ed.query(".mcp-action-row"))
        assert before == after  # те же объекты — обновлены на месте


async def test_run_action_installs_missing():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        ed.move(1)  # office → playwright
        ed.move(1)  # → ⤓ (единственная строка-действие)
        assert ed.current_action == "missing"
        assert ed.begin_enter() is True
        assert ed.awaiting_confirm is True
        assert mgr.calls == []
        res = ed.confirm_pending()
        assert ("install", "playwright") in mgr.calls  # недостающий поставлен
        assert res == [("playwright", True)]


async def test_run_action_updates_all():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [
        McpRow("office", wired=True, manual=False),
        McpRow("ddg", wired=True, manual=False),
    ]
    mgr = FakeManager(
        rows,
        {
            "office": McpState(True, "0.3.1", "0.4.0"),
            "ddg": McpState(True, "0.1.0", "0.2.0"),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot,
            lambda: ed.status_ready("office") and ed.status_ready("ddg"),
            "loaded",
        )
        assert ed._action_kinds() == [
            "update"
        ]  # оба updatable, недостающих нет
        ed.move(1)
        ed.move(1)  # office → ddg → ⟳
        assert ed.current_action == "update"
        assert ed.begin_enter() is True
        assert mgr.calls == []
        res = ed.confirm_pending()
        assert ("install", "office") in mgr.calls and (
            "install",
            "ddg",
        ) in mgr.calls
        assert res == [("office", True), ("ddg", True)]


async def test_update_all_row_shows_for_connected_without_updates():
    """A/§4: ↻ «Обновить все подключённые» видна, когда есть подключённые, даже без
    обнаруженных обновлений (source-рецепты, update=None) — идемпотентная пересборка;
    счётчик «N обновлений» не показывается, когда их нет."""
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow
    from textual.widgets import Static

    rows = [
        McpRow("office", wired=True, manual=False),
        McpRow("ddg", wired=True, manual=False),
    ]
    mgr = FakeManager(
        rows,
        {
            "office": McpState(True, "0.3.1", None),  # update=None
            "ddg": McpState(True, "0.1.0", None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot,
            lambda: ed.status_ready("office") and ed.status_ready("ddg"),
            "loaded",
        )
        assert ed._action_kinds() == ["update"]  # подключённые есть → ↻ видна
        blob = " ".join(str(w.render()) for w in ed.query(Static))
        assert "Обновить все подключённые" in blob
        assert "обновлени" not in blob  # счётчик обновлений скрыт (их нет)
        ed.move(1)
        ed.move(1)  # office → ddg → ↻
        assert ed.current_action == "update"
        assert ed.begin_enter() is True
        ed.confirm_pending()
        assert ("install", "office") in mgr.calls and (
            "install",
            "ddg",
        ) in mgr.calls


async def test_ctrl_u_updates_current():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("office"), "loaded")
        assert ed.begin_update() is True  # курсор на office (wired) → confirm
        assert ed.awaiting_confirm is True
        assert mgr.calls == []
        res = ed.confirm_pending()
        assert ("install", "office") in mgr.calls
        assert res == [("office", True)]
        assert ed.awaiting_confirm is False


async def test_ctrl_u_noop_on_not_installed_and_action_row():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        ed.move(1)  # office → playwright (не установлен)
        assert ed.begin_update() is False  # не установлен и не wired → no-op
        assert ed.awaiting_confirm is False
        assert mgr.calls == []
        ed.move(1)  # → ⤓ (строка-действие)
        assert ed.current_action == "missing"
        assert ed.begin_update() is False  # на строке-действии → no-op
        assert ed.awaiting_confirm is False
        assert mgr.calls == []


async def test_reload_keeps_cursor_after_action_row_op():
    """После операции со строки-действия _idx не выходит за границу управляемых."""
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(
            pilot, lambda: ed.status_ready("playwright"), "loaded"
        )
        ed.move(1)
        ed.move(1)  # курсор на ⤓ (строка-действие)
        assert ed.current_action == "missing"
        ed._reload_rows()  # эмулируем ресинк после операции
        managed = [r for r in mgr.rows() if not r.manual]
        assert 0 <= ed._idx < len(managed)  # _idx валиден, не за границей


async def test_confirm_gate_blocks_other_actions():
    """Пока висит confirm-баннер, повторный begin_enter/begin_update/toggle/request_remove
    не исполняются и не сбрасывают уже открытый confirm."""
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor

    mgr = FakeManager(
        _rows(),
        {
            "office": McpState(True, "0.3.1", None),
            "playwright": McpState(False, None, None),
        },
    )
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("office"), "loaded")
        assert ed.begin_update() is True  # office → confirm обновления
        assert ed.awaiting_confirm is True
        assert ed.toggle() is None
        assert ed.begin_enter() is False
        assert ed.begin_update() is False
        assert ed.request_remove() is False
        assert ed.awaiting_confirm is True  # confirm всё ещё висит
        assert mgr.calls == []  # ничего не исполнилось
        res = ed.confirm_pending()
        assert ("install", "office") in mgr.calls
        assert res == [("office", True)]


async def test_mcp_row_shows_description(monkeypatch):
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow, McpState

    class _Mgr:
        def rows(self):
            return [
                McpRow(
                    "office",
                    wired=False,
                    manual=False,
                    description="чтение/запись Word · Excel · PDF",
                )
            ]

        def state(self, n):
            return McpState(installed=False, version=None, update=None)

    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=_Mgr())
        await app.mount(ed)
        await pilot.pause()
        body = " ".join(str(w.render()) for w in ed.query(".mcp-row-managed"))
        assert "чтение/запись Word" in body


async def test_mcp_descriptions_align_across_statuses():
    """Описания стартуют в ОДНОЙ колонке при разной длине статусов (включая длинный
    «установлен, отключён · v0.1.0») — динамическая колонка по самому широкому статусу."""
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow, McpState

    rows = [
        McpRow(
            "computer", wired=True, manual=False, description="DESCA"
        ),  # подключён · v0.1.0
        McpRow(
            "office", wired=False, manual=False, description="DESCB"
        ),  # не установлен
        McpRow(
            "science", wired=False, manual=False, description="DESCC"
        ),  # установлен, отключён · v0.1.0
    ]
    states = {
        "computer": McpState(installed=True, version="0.1.0", update=None),
        "office": McpState(installed=False, version=None, update=None),
        "science": McpState(installed=True, version="0.1.0", update=None),
    }

    class _Mgr:
        def rows(self):
            return rows

        def state(self, n):
            return states[n]

    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=_Mgr())
        ed._states = states
        cols = [
            ed._row_text(r, i).plain.index("DESC" + "ABC"[i])
            for i, r in enumerate(rows)
        ]
        assert len(set(cols)) == 1, f"описания не выровнены: {cols}"


async def test_enter_noop_when_already_installed():
    from gc_testkit.tui_support import wait_until
    from gc_tui import app as app_mod
    from gc_tui.editors.mcp_section import McpSectionEditor
    from gc_tui.services.mcp_manager import McpRow

    rows = [McpRow("computer", wired=False, manual=False)]
    mgr = FakeManager(rows, {"computer": McpState(True, "0.2.0", None)})
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        ed = McpSectionEditor(manager=mgr)
        await app.mount(ed)
        await wait_until(pilot, lambda: ed.status_ready("computer"), "loaded")
        assert ed.begin_enter() is False
        assert ed.awaiting_confirm is False
        assert mgr.calls == []
