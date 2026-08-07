"""Пресет-лист адреса на общем контроллере (`preset_list_controller`): минимальный
хост (Vertical с Input+якорем) — открытие, курсор, фильтр, выбор пресета, закрытие
без дублей (Task 6)."""
import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from config_modules.endpoint_presets import EndpointPreset

_HINT = "пресеты · печать — фильтр/свой адрес · Enter — подставить · Esc — свернуть"

_PRESETS = [
    EndpointPreset(name="OpenRouter", url="https://openrouter.ai/api/v1/chat/completions"),
    EndpointPreset(name="Cloud.ru", url="https://foundation-models.api.cloud.ru/v1/chat/completions"),
    EndpointPreset(name="GigaCode", url="https://litellm-proxy.sberai-rnd.ru/v1/chat/completions"),
]


class _Host(Vertical):
    def compose(self) -> ComposeResult:
        # Поле живёт в рамке-группе `#<input_id>-group` — контроллер монтирует
        # строки внутрь неё (единый язык форм 2026-08-03 §3.1). Без группы у
        # минимального хоста тест проверял бы уже несуществующий контракт.
        with Vertical(classes="field-group", id="host-url-group"):
            yield Input(id="host-url")
        yield Static("anchor", id="host-anchor")

    def on_mount(self) -> None:
        from tui.editors.preset_list import preset_list_controller
        self.ctrl = preset_list_controller(
            self, presets=list(_PRESETS), row_class="test-preset-row",
            input_id="host-url", hint=_HINT,
        )


def _harness(host):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield host
    return _App()


async def test_open_mounts_status_and_rows_with_cursor_on_first():
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        rows = list(host.query(".test-preset-row"))
        assert len(rows) == 3
        assert str(rows[0].render()).startswith("▸ OpenRouter")
        assert "https://openrouter.ai/api/v1/chat/completions" in str(rows[0].render())
        assert str(rows[1].render()).startswith("  Cloud.ru")
        status = str(host.query_one(".host-url-preset-hint").render())
        assert status == _HINT


async def test_move_advances_cursor_to_next_row():
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.move(1)
        await pilot.pause()
        rows = list(host.query(".test-preset-row"))
        assert str(rows[0].render()).startswith("  OpenRouter")
        assert str(rows[1].render()).startswith("▸ Cloud.ru")


async def test_pick_returns_selected_url_and_closes_list():
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.move(1)                     # -> Cloud.ru
        picked = host.ctrl.pick()
        await pilot.pause()
        # pick() отдаёт сам пресет — хост решает, подставлять ли .url
        # (пункт-наследование с .inherit подставлять нельзя)
        assert picked.url == "https://foundation-models.api.cloud.ru/v1/chat/completions"
        assert host.ctrl.is_open is False
        assert not list(host.query(".test-preset-row"))
        assert not list(host.query(".host-url-preset-hint"))


async def test_pick_with_no_presets_left_visible_returns_none():
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("zzz-no-match")
        await pilot.pause()
        assert host.ctrl.pick() is None
        assert host.ctrl.is_open is False


async def test_hint_stops_promising_a_pick_when_the_filter_matches_nothing():
    """Набран свой адрес — в списке не осталось строк, и `pick()` подставлять нечего.
    Подсказка под списком обязана это признать: «Enter — подставить» там прямая ложь,
    а строка эта висит на самом заметном месте — под полем адреса.

    Канон взят у соседнего списка (`FieldListController._refresh_rows` переключает
    `_MODEL_HINT`/`_MODEL_HINT_NO_MATCH` на каждый фильтр)."""
    from tui.editors.preset_list import PRESET_HINT_NO_MATCH
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert str(host.query_one(".host-url-preset-hint").render()) == _HINT
        host.ctrl.apply_filter("https://my-own-host.internal/v1")
        await pilot.pause()
        assert str(host.query_one(".host-url-preset-hint").render()) == PRESET_HINT_NO_MATCH
        # Вернулись к совпадениям — вернулась и подсказка хоста (она у каждого своя).
        host.ctrl.apply_filter("cloud")
        await pilot.pause()
        assert str(host.query_one(".host-url-preset-hint").render()) == _HINT


def test_preset_no_match_hint_and_the_wizard_footer_name_enter_alike():
    """Обе строки видны одновременно (хинт под списком, футер внизу) — разными словами
    одну клавишу они звать не могут."""
    from tui.editors.preset_list import PRESET_HINT_NO_MATCH
    from tui.screens.onboarding import _FOOTER_PRESET_LIST_NO_MATCH
    assert "Enter — оставить свой адрес" in PRESET_HINT_NO_MATCH
    assert "Enter — оставить свой адрес" in _FOOTER_PRESET_LIST_NO_MATCH


async def test_apply_filter_narrows_to_one_row_empty_filter_shows_all():
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.apply_filter("cloud")
        await pilot.pause()
        visible = [w for w in host.query(".test-preset-row") if w.display]
        assert len(visible) == 1
        assert "Cloud.ru" in str(visible[0].render())
        host.ctrl.apply_filter("")
        await pilot.pause()
        visible = [w for w in host.query(".test-preset-row") if w.display]
        assert len(visible) == 3


async def test_close_removes_everything_and_reopen_does_not_duplicate_rows():
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.close()
        await pilot.pause()
        assert not list(host.query(".test-preset-row"))
        assert not list(host.query(".host-url-preset-hint"))
        host.ctrl.open()
        await pilot.pause()
        host.ctrl.open()                       # повторный open() — без дублей строк
        await pilot.pause()
        rows = list(host.query(".test-preset-row"))
        assert len(rows) == 3


async def test_open_with_empty_field_shows_all_cursor_on_first():
    """Поле пустое (первичная настройка) → список раскрывается полностью,
    подсвечена первая строка — предложить все варианты (спека-отклонение №5)."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.is_open is True
        assert host.ctrl._idx == 0
        rows = list(host.query(".test-preset-row"))
        assert len(rows) == 3


async def test_open_with_preset_url_in_field_highlights_that_preset():
    """В поле — URL, совпадающий с одним из пресетов → список раскрывается,
    подсвечен ИМЕННО он: Enter вернёт тот же пресет и подстановка ничего не
    испортит (значение поля останется тем же самым)."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        cloud_url = "https://foundation-models.api.cloud.ru/v1/chat/completions"
        host.query_one("#host-url", Input).value = cloud_url
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.is_open is True
        assert host.ctrl._idx == 1                     # Cloud.ru — второй по счёту пресет
        picked = host.ctrl.pick()
        assert picked.url == cloud_url                 # подстановка — no-op, поле не меняется


async def test_initial_index_is_none_when_value_is_not_a_preset():
    """Свой адрес, которого нет в списке: подсвеченной строки нет вовсе.

    Иначе Enter по форсированно раскрытому списку (Alt+↓, спека 2026-08-06 §2.4)
    молча заменил бы рабочий адрес первым пресетом. `can_open` в обычном `open()`
    как раз и не пускает список поверх такого адреса (см. тест ниже) — раскрываем
    форсированно (`forced=True`), как это будет делать Alt+↓."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        own_url = "https://my-own-server.example/v1/chat/completions"
        host.query_one("#host-url", Input).value = own_url
        host.ctrl.open(forced=True)
        await pilot.pause()
        assert host.ctrl.is_open is True
        assert host.ctrl.selected_item is None


async def test_hint_calls_it_no_match_when_forced_open_has_no_highlight():
    """Находка A финального ревью 2026-08-06: `has_matches` (строки видны) и
    `has_selection` (что-то подсвечено) — разные вопросы именно в этом состоянии
    (форсированное раскрытие Alt+↓ поверх своего адреса, спека §2.4). Раньше хинт
    читал `has_matches`, и здесь ВСЕ строки видны — хинт врал «Enter — подставить»,
    хотя `pick()` вернёт None (список просто свернётся, ничего не сохранив)."""
    from tui.editors.preset_list import PRESET_HINT_NO_MATCH
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        own_url = "https://my-own-server.example/v1/chat/completions"
        host.query_one("#host-url", Input).value = own_url
        host.ctrl.open(forced=True)
        await pilot.pause()
        assert host.ctrl.has_matches is True            # предусловие: строки все видны
        assert host.ctrl.selected_item is None            # но ни одна не подсвечена
        hint = str(host.query_one(".host-url-preset-hint").render())
        assert hint == PRESET_HINT_NO_MATCH


async def test_initial_index_matches_the_preset_with_that_url():
    """В поле — URL одного из пресетов → подсвечена ИМЕННО эта строка."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        cloud_url = "https://foundation-models.api.cloud.ru/v1/chat/completions"
        host.query_one("#host-url", Input).value = cloud_url
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.selected_item is not None
        assert host.ctrl.selected_item.url == cloud_url


async def test_initial_index_highlights_first_row_when_field_is_empty():
    """Пустое поле (первичная настройка) — по-прежнему подсвечена первая строка:
    §2.4 меняет только случай «значение есть, но чужое», не пустое поле."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.selected_item is not None
        assert host.ctrl.selected_item.name == "OpenRouter"


async def test_open_with_custom_address_in_field_does_not_open():
    """В поле — свой рабочий адрес, которого нет в списке пресетов → список НЕ
    раскрывается (ловушка, спека-отклонение №5): иначе ↑/↓ уходили бы в список
    вместо формы, а Enter молча затёр бы адрес первым пресетом."""
    host = _Host()
    app = _harness(host)
    async with app.run_test() as pilot:
        await pilot.pause()
        own_url = "https://my-own-server.example/v1/chat/completions"
        host.query_one("#host-url", Input).value = own_url
        host.ctrl.open()
        await pilot.pause()
        assert host.ctrl.is_open is False
        assert not list(host.query(".test-preset-row"))
        assert not list(host.query(".host-url-preset-hint"))
        # ↑/↓ формы (move() контроллера — no-op при закрытом списке, форма ведёт себя как без пресетов)
        host.ctrl.move(1)
        assert host.ctrl.is_open is False
        # Enter (pick()) на закрытом списке ничего не возвращает — поле не трогается
        assert host.ctrl.pick() is None
        assert host.query_one("#host-url", Input).value == own_url
