"""FieldListController — общий контроллер выпадающего списка под полем (Task 12).

Механика (раскрытие/сворачивание, фильтр по `display` без пересборки DOM, приоритет
↑/↓ над лентой хозяина, `Esc` с откатом фильтра, автоподскролл) — одна на всех; на
каждое различие, уехавшее в параметр (`rows_source`, `render_row`, `matches`,
`value_of`, `initial_index`, `can_open`, `on_pick`, `restore_on_close`), — по тесту.
"""

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

_ROW = "fl-row"
_HINT = "fl-hint"
_SCROLL = "fl-scroll"

_ITEMS = ("alpha", "beta", "gamma")


def _render(item, *, selected: bool) -> Text:
    t = Text()
    t.append("▸ " if selected else "  ")
    t.append(item)
    return t


class _Host(Vertical):
    """Минимальный хозяин: поле в рамке-группе `#fl-input-group` (единый язык форм
    §3.1 — строки списка монтируются ВНУТРЬ неё) плюс соседний виджет."""

    def __init__(self, **ctrl_kwargs) -> None:
        super().__init__()
        self._ctrl_kwargs = ctrl_kwargs

    def compose(self) -> ComposeResult:
        with Vertical(classes="field-group", id="fl-input-group"):
            yield Input(id="fl-input")
        yield Static("anchor", id="fl-anchor")

    def on_mount(self) -> None:
        from gc_tui.editors.field_list import FieldListController

        kwargs = dict(
            input_id="fl-input",
            row_class=_ROW,
            hint_class=_HINT,
            scroll_class=_SCROLL,
            rows_source=_ITEMS,
            render_row=_render,
            hint=lambda ok: "есть" if ok else "нет",
        )
        kwargs.update(self._ctrl_kwargs)
        self.ctrl = FieldListController(self, **kwargs)


def _harness(host):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield host

    return _App()


def _rows(host):
    return list(host.query(f".{_ROW}"))


def _shown(host):
    return [str(w.render()) for w in _rows(host) if w.display]


def _hint(host):
    return str(host.query_one(f".{_HINT}").render())


# --- механика (перенос тестов трёх контроллеров) ---


async def test_open_mounts_rows_and_hint_cursor_on_first():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.is_open is True
        assert _shown(host) == ["▸ alpha", "  beta", "  gamma"]
        assert _hint(host) == "есть"


async def test_filter_hides_rows_without_rebuilding_dom():
    """Фильтр гасит УЖЕ смонтированные строки через `display` — DOM не пересобирается."""
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        before = _rows(host)
        host.ctrl.apply_filter("bet")
        await pilot.pause()
        assert _shown(host) == ["▸ beta"]
        assert _rows(host) == before  # те же виджеты, не новые
        host.ctrl.apply_filter("")
        await pilot.pause()
        assert len(_shown(host)) == 3


async def test_hint_switches_when_filter_matches_nothing():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("zzz")
        await pilot.pause()
        assert host.ctrl.has_matches is False
        assert _hint(host) == "нет"


async def test_move_walks_rows_and_eats_the_key():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        assert host.ctrl.move(1) is False  # свёрнут — клавиша не наша
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.move(1) is True
        await pilot.pause()
        assert _shown(host)[1] == "▸ beta"
        host.ctrl.move(-1)
        await pilot.pause()
        assert _shown(host)[0] == "▸ alpha"


async def test_move_returns_key_when_filter_matches_nothing():
    """Ходить нечем — стрелки возвращаются ленте хозяина, иначе они «умирают»."""
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("zzz")
        await pilot.pause()
        assert host.ctrl.move(1) is False


async def test_close_removes_rows_but_keeps_the_hint_reopen_does_not_duplicate():
    """Свёртывание уносит строки списка, но НЕ строку-подсказку: её текст —
    функция состояния (см. `_sync_hint`), и виджет живёт, пока живо поле."""
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        assert not _rows(host)
        assert "показать список" in _hint_text(host)
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.open()  # повторный open() — без дублей строк
        await pilot.pause()
        assert len(_rows(host)) == 3


async def test_pick_returns_value_and_closes():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.move(1)
        assert host.ctrl.pick() == "beta"
        await pilot.pause()
        assert host.ctrl.is_open is False
        assert not _rows(host)


async def test_pick_without_matches_returns_none():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("zzz")
        await pilot.pause()
        assert host.ctrl.pick() is None
        assert host.ctrl.is_open is False


async def test_typing_reopens_a_collapsed_list_keeping_the_baseline():
    """После Esc список свёрнут, а курсор остался в поле: печать раскрывает его заново
    и НЕ переписывает «значение до фильтра» — иначе следующий Esc вернул бы набранное."""
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        field = host.query_one("#fl-input", Input)
        field.value = "gamma"
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        host.ctrl.reopen_for_typing("bet")
        await pilot.pause()
        assert host.ctrl.is_open is True
        assert _shown(host) == ["▸ beta"]
        host.ctrl.close()
        await pilot.pause()
        assert field.value == "gamma"


async def test_reload_opens_a_collapsed_list():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        assert host.ctrl.reload() is True
        await pilot.pause()
        assert host.ctrl.is_open is True
        assert (
            host.ctrl.reload() is False
        )  # раскрытому статическому списку нечего перечитывать


async def test_selected_row_is_scrolled_into_view(monkeypatch):
    seen = []
    original = Static.scroll_visible
    monkeypatch.setattr(
        Static,
        "scroll_visible",
        lambda self, **kw: seen.append(self) or original(self, **kw),
    )
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("gamma")
        await pilot.pause()
        assert any(str(w.render()).endswith("gamma") for w in seen)


# --- различия, уехавшие в параметры ---


async def test_rows_source_may_be_a_callable_read_on_every_open():
    """Источник строк — параметр: список или функция, которую перечитывают на раскрытии."""
    box = ["one"]
    host = _Host(rows_source=lambda: list(box))
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert _shown(host) == ["▸ one"]
        host.ctrl.close()
        await pilot.pause()
        box.append("two")
        host.ctrl.open()
        await pilot.pause()
        assert _shown(host) == ["▸ one", "  two"]


async def test_render_row_draws_the_columns_of_its_host():
    """Рендер строки — параметр: у моделей колонки и метки, у языка код и название."""

    def render(item, *, selected):
        return Text(("> " if selected else "· ") + item.upper())

    host = _Host(render_row=render)
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert _shown(host) == ["> ALPHA", "· BETA", "· GAMMA"]


async def test_matches_rule_is_a_parameter():
    """Правило фильтра — параметр: язык ищет и по коду, и по названию."""
    host = _Host(matches=lambda item, q: item.endswith(q))
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("ma")
        await pilot.pause()
        assert _shown(host) == ["▸ gamma"]


async def test_value_of_lets_pick_return_the_item_itself():
    """Что отдаёт `pick()` — параметр: у пресетов это объект, а не строка: подставлять
    ли его .url, решает хозяин."""
    host = _Host(value_of=lambda item: {"id": item})
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.pick() == {"id": "alpha"}


async def test_initial_index_highlights_the_current_value():
    """Подсветка на входе — параметр: у языка и моделей это ДЕЙСТВУЮЩЕЕ значение, а не
    первая строка (Enter сразу после входа не имеет права подменить чужое)."""

    def initial(items, value):
        return next((i for i, it in enumerate(items) if it == value), None)

    host = _Host(initial_index=initial)
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.query_one("#fl-input", Input).value = "gamma"
        host.ctrl.open()
        await pilot.pause()
        assert _shown(host) == ["  alpha", "  beta", "▸ gamma"]
        assert host.ctrl.pick() == "gamma"


async def test_initial_index_may_leave_nothing_highlighted():
    """Своего значения в списке нет — подсвеченной строки нет вовсе, и Enter уходит
    про введённое вручную."""

    def initial(items, value):
        return next((i for i, it in enumerate(items) if it == value), None)

    host = _Host(initial_index=initial)
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.query_one("#fl-input", Input).value = "своё"
        host.ctrl.open()
        await pilot.pause()
        assert _shown(host) == ["  alpha", "  beta", "  gamma"]
        assert host.ctrl.pick() is None


async def test_can_open_forbids_opening_over_a_foreign_value():
    """Правило «можно ли раскрывать» — параметр: пресет-лист адреса не раскрывается
    поверх чужого адреса, иначе Enter молча затёр бы рабочее значение."""
    host = _Host(can_open=lambda value: value in ("", *_ITEMS))
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        field = host.query_one("#fl-input", Input)
        field.value = "https://my-own-host/v1"
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.is_open is False
        assert not _rows(host)
        assert host.ctrl.move(1) is False
        assert host.ctrl.pick() is None
        assert field.value == "https://my-own-host/v1"


async def test_on_pick_is_what_enter_does():
    """Действие по Enter — параметр: подстановка в поле идёт через него."""
    picked = []
    host = _Host(on_pick=picked.append)
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.move(1)
        host.ctrl.pick()
        assert picked == ["beta"]


async def test_restore_on_close_off_keeps_the_typed_text():
    """Откат фильтра по Esc — параметр: в поле адреса печать это не только фильтр, но и
    свой адрес, и стирать набранное нельзя."""
    host = _Host(restore_on_close=False)
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        field = host.query_one("#fl-input", Input)
        host.ctrl.open()
        await pilot.pause()
        field.value = "своё"
        host.ctrl.apply_filter("своё")
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        assert field.value == "своё"


async def test_restore_on_close_rolls_the_filter_back():
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        field = host.query_one("#fl-input", Input)
        field.value = "gamma"
        host.ctrl.open()
        await pilot.pause()
        field.value = "bet"
        host.ctrl.apply_filter("bet")
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        assert field.value == "gamma"


async def test_pick_becomes_the_new_baseline():
    """Enter фиксирует поле: следующий Esc обязан вернуть ВЫБРАННОЕ, а не то, с чем
    входили, — иначе он молча откатил бы только что сохранённое."""
    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        field = host.query_one("#fl-input", Input)
        field.value = "alpha"
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.move(1)
        assert host.ctrl.pick() == "beta"
        await pilot.pause()
        field.value = "beta"  # так поступает хозяин/`on_pick`
        host.ctrl.reopen_for_typing("ga")
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        assert field.value == "beta"


async def test_closed_hint_tells_how_to_reopen():
    """Свёрнутый список называет способ себя вызвать — ДОСЛОВНО `LIST_CLOSED_HINT`.

    Гоняется боевым путём (`close()`), а не ручным показом: `show_closed_hint` и
    `hide_closed_hint` удалены вместе с каноном 2026-08-17 — строка под полем
    постоянна, и единственное, что с ней происходит, это смена текста."""
    from gc_tui.editors.field_list import LIST_CLOSED_HINT

    host = _Host()
    async with _harness(host).run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        assert _hint(host) == LIST_CLOSED_HINT


def _hint_text(host) -> str:
    """Текст живой строки-подсказки поля («» — строки нет)."""
    live = [w for w in host.query(f".{_HINT}") if not w._pruning]
    return str(live[-1].render()) if live else ""


async def test_close_keeps_the_hint_and_switches_its_text():
    """Строка-подсказка переживает свёртывание: её текст — функция состояния списка.

    Раньше `close()` сносил виджет, и каждый путь закрытия обязан был помнить о
    ремонте — три пути на пять точек. Забыли бы одну (а забыли две: Enter и фильтр
    в ноль) — строка пропадала, и форма дёргалась на строку вверх."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.is_open
        assert "показать список" not in _hint_text(host)

        host.ctrl.close()
        await pilot.pause()

        assert not host.ctrl.is_open
        assert "показать список" in _hint_text(host)
