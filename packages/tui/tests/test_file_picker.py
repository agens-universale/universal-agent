"""FilePicker — overlay список путей, set_entries (внешний листинг), навигация."""
import pytest

from gc_tui.widgets.file_picker import FilePicker


def test_selected_entry_default_first():
    p = FilePicker(["agents/", "a.py", "b.py"])
    assert p.selected_entry() == "agents/"


def test_move_selection_wraps():
    p = FilePicker(["a", "b", "c"])
    p.move_selection(1)
    assert p.selected_entry() == "b"
    p.move_selection(-1)
    p.move_selection(-1)
    assert p.selected_entry() == "c"  # wrap


def test_set_entries_resets_selection():
    p = FilePicker(["a", "b"])
    p.move_selection(1)
    p.set_entries(["x", "y", "z"])
    assert p.selected_entry() == "x"


def test_empty_entries_selected_none():
    p = FilePicker([])
    assert p.selected_entry() is None


def test_truncated_note_adds_two_rows_to_desired_height():
    """Строка-подсказка + её margin-top: честная высота растёт на 2
    (недосчёт выдавил бы нижнюю рамку пикера за экран)."""
    assert FilePicker(["a"]).desired_height == 1 + 2 + 2
    assert FilePicker(["a"], truncated=True).desired_height == 1 + 2 + 2 + 2


def test_set_entries_updates_truncated_flag():
    p = FilePicker(["a"], truncated=True)
    p.set_entries(["b"])
    assert p._truncated is False
    assert p.desired_height == 1 + 2 + 2


def test_empty_truncated_picker_height_counts_note():
    """Пусто + обрезанный индекс: строка «нет совпадений»(1) + рамка(2) +
    клавиши(2) + подсказка(2)."""
    assert FilePicker([], truncated=True).desired_height == 1 + 2 + 2 + 2


def test_searching_picker_shows_search_text_instead_of_no_match():
    assert FilePicker([], searching=True)._empty_text() == "ищу по проекту…"
    assert FilePicker([])._empty_text() == "(нет совпадений)"
    p = FilePicker([], searching=True)
    p.set_entries(["a"])
    assert p._searching is False


@pytest.mark.asyncio
async def test_entries_set_before_mount_completes_are_shown(tmp_path):
    """Результат поиска может прийти раньше, чем пикер смонтировался (порог
    «ищу по проекту…»): строки и честная высота должны соответствовать новым
    данным, а не тем, с которыми пикер создавали."""
    pytest.importorskip("textual")
    from textual.app import App

    app = App()
    async with app.run_test() as pilot:
        picker = FilePicker([], searching=True)
        pilot.app.mount(picker)                        # монтирование не ждём
        picker.set_entries(["a.py", "b.py", "c.py"])
        await pilot.pause()
        assert len(picker.query(".palette-row")) == 3
        assert picker.styles.min_height.value == picker.desired_height
        assert picker._searching is False


@pytest.mark.asyncio
async def test_entries_set_after_compose_before_mount_are_shown():
    """Данные приходят ПОСЛЕ compose, но ДО конца монтирования (результат поиска
    догнал пикер на пороге «ищу по проекту…»): строки обязаны перестроиться сами,
    без следующего нажатия — иначе на экране остаётся «ищу…» при готовой выдаче."""
    pytest.importorskip("textual")
    from textual.app import App

    class _LateData(FilePicker):
        def compose(self):
            yield from super().compose()
            self.set_entries(["a.py", "b.py", "c.py"])

    app = App()
    async with app.run_test() as pilot:
        picker = _LateData([], searching=True)
        await pilot.app.mount(picker)
        await pilot.pause()      # отложенный _rebuild (call_after_refresh из on_mount)
        await pilot.pause()      # его remove_children/mount_all — асинхронные, ещё один цикл
        assert len(picker.query(".palette-row")) == 3
        assert picker._stale is False
        assert picker.styles.min_height.value == picker.desired_height
