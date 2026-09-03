"""ArgPicker — чистая логика фильтра/навигации (без mount/app)."""
import pytest


_OPTS = [("txt", "вся сессия в читаемом виде"), ("json", "структурированные данные сессии")]


def test_empty_filter_returns_all():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="")
    assert [o[0] for o in p._visible_rows()] == ["txt", "json"]


def test_prefix_filter_matches_value():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="j")
    assert [o[0] for o in p._visible_rows()] == ["json"]
    assert p.selected_value() == "json"


def test_filter_case_insensitive():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="TX")
    assert [o[0] for o in p._visible_rows()] == ["txt"]


def test_no_match_returns_empty():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="zzz")
    assert p._visible_rows() == []
    assert p.selected_value() is None


def test_move_selection_wraps_around():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="")
    assert p.selected_value() == "txt"
    p.move_selection(1)
    assert p.selected_value() == "json"
    p.move_selection(1)
    assert p.selected_value() == "txt"


def test_descriptions_align_to_common_value_column():
    """Спека 2026-07-18 §4: описания начинаются в общей колонке — value
    паддируется до самого длинного value среди видимых опций + 3 пробела."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    opts = [("txt", "вся сессия"), ("log-json", "лог-файл треда")]
    p = ArgPicker(opts, initial_filter="")

    short_row = p._format_row(opts[0])
    long_row = p._format_row(opts[1])

    assert short_row.index("вся сессия") == long_row.index("лог-файл")
    assert long_row == " log-json   лог-файл треда"


def test_row_without_desc_stays_bare_value():
    """Опция без описания — просто value (как раньше), без паддинга."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker([("txt", "")], initial_filter="")
    assert p._format_row(("txt", "")) == " txt"


def test_two_tuple_row_still_plain_str_backward_compat():
    """2-tuple опции (/export и т.п.) продолжают рендериться простым `str` —
    3-й элемент (статус) не должен задеть другие пикеры (задача 10)."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker([("txt", "вся сессия")], initial_filter="")
    row = p._format_row(("txt", "вся сессия"))
    assert isinstance(row, str)
    assert row == " txt   вся сессия"


def test_three_tuple_row_renders_status_column_colored():
    """3-tuple опции (/profiles) — статус третьим элементом рендерится ЗЕЛЁНЫМ
    (OK), ровно как активный маркер в /settings (`_profile_row_text`)."""
    pytest.importorskip("textual")
    from tui import palette as pal
    from tui.widgets.arg_picker import ArgPicker

    opts = [
        ("без профиля", "работать чистым конфигом", ""),
        ("openrouter", "глобальный (как в конфиге)", ""),
        ("smoke", "глобальный", "● активен"),
    ]
    p = ArgPicker(opts, initial_filter="")

    none_row = p._format_row(opts[0])
    active_row = p._format_row(opts[2])

    # описание начинается в общей колонке, как у 2-tuple опций
    assert none_row.plain.index("работать") == active_row.plain.index("глобальный")
    # статус — самая правая часть строки, только у активной
    assert active_row.plain.rstrip().endswith("● активен")
    assert "активен" not in none_row.plain

    spans = [s for s in active_row.spans if active_row.plain[s.start:s.end] == "● активен"]
    assert spans and str(spans[0].style) == pal.OK


def test_three_tuple_status_visible_even_with_empty_description():
    """Ревью-фикс: пустое описание не должно молча ронять статус — 3-tuple с
    `desc=""` и непустым `status` всё равно рисует статус (контракт для
    будущих потребителей общего виджета, не только /profiles)."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker([("x", "", "● активен")], initial_filter="")
    row = p._format_row(("x", "", "● активен"))
    assert "● активен" in row.plain


def test_status_column_aligns_to_longest_visible_description():
    """Колонка статуса выровнена по самому длинному ОПИСАНИЮ среди видимых
    строк — тот же принцип, каким уже паддится value-колонка (_value_col_w)."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    short_opts = [("smoke", "глобальный", "● активен")]
    long_opts = [
        ("openrouter", "глобальный (как в конфиге)", ""),
        ("smoke", "глобальный", "● активен"),
    ]
    p_short = ArgPicker(short_opts, initial_filter="")
    p_long = ArgPicker(long_opts, initial_filter="")

    idx_short = p_short._format_row(short_opts[0]).plain.index("● активен")
    idx_long = p_long._format_row(long_opts[1]).plain.index("● активен")
    # более длинное описание в списке сдвигает статус правее — колонка
    # считается по самой длинной видимой строке, не по своей
    assert idx_long > idx_short


def test_set_filter_resets_selection_to_zero():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="")
    p.move_selection(1)
    assert p.selected_value() == "json"
    p.set_filter("")
    assert p.selected_value() == "txt"


def test_hint_provider_not_called_twice_for_the_same_selection():
    """`_build_footer` и `desired_height` спрашивали хинт КАЖДЫЙ по-своему —
    два полных вызова провайдера на одну и ту же подсветку (найдено на
    /profiles: провайдер внутри зовёт полную сборку конфига, ~6 мс). Один и тот
    же выбор обязан стоить один вызов, кто бы из двух его первым ни спросил."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    calls = []

    def hint(value):
        calls.append(value)
        return f"hint for {value}"

    p = ArgPicker(_OPTS, initial_filter="", hint_provider=hint)
    calls.clear()   # __init__ уже посчитал хинт один раз ради стартового desired_height
    p._build_footer()
    _ = p.desired_height
    assert calls == [], f"выбор не менялся, а провайдер дёрнут снова: {calls}"


def test_hint_provider_not_called_again_on_repeated_desired_height():
    """`desired_height` дёргает поллинг главного экрана каждые 250 мс, пока
    пикер просто открыт (`TraceScreen._poll_active` → `_sync_trace_max_height`).
    Без кэша это провайдер (полная сборка конфига для /profiles) 4 раза в
    секунду на UI-потоке — никто не нажимал ни одной клавиши."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    calls = []

    def hint(value):
        calls.append(value)
        return "hint"

    p = ArgPicker(_OPTS, initial_filter="", hint_provider=hint)
    calls.clear()   # __init__ уже посчитал хинт один раз ради стартового desired_height
    for _ in range(5):
        _ = p.desired_height
    assert calls == [], f"повторные обращения к desired_height дёрнули провайдер заново: {calls}"


def test_hint_provider_called_again_after_selection_changes():
    """Кэш — на ЗНАЧЕНИЕ выбора, не глобальный выключатель: смена подсветки
    (стрелка) обязана посчитать хинт для НОВОГО пункта, а не отдать текст
    прежнего."""
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    calls = []

    def hint(value):
        calls.append(value)
        return f"hint for {value}"

    p = ArgPicker(_OPTS, initial_filter="", hint_provider=hint)
    calls.clear()
    p.move_selection(1)
    assert p.selected_value() == "json"
    _ = p.desired_height
    p._build_footer()
    assert calls == ["json"], f"новый выбор не пересчитал хинт: {calls}"
