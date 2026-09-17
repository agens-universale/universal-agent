"""tui/services/session_title: чистые функции имени сессии (title/task/last_actual).

display_name — единый источник имени для пикера и OSC:
  title (ручной) > task (из turn_finish) > last_actual (авто из промпта) > «Без названия».
normalize_prompt_title — collapse whitespace + truncate до LAST_ACTUAL_MAX,
порог обновления last_actual — LAST_ACTUAL_MIN по уже нормализованной строке.
"""
from gc_tui.services.session_title import (
    LAST_ACTUAL_MAX,
    LAST_ACTUAL_MIN,
    display_name,
    normalize_prompt_title,
    task_subtitle,
    task_title,
)


# ---- display_name -------------------------------------------------------

def test_display_none_meta_is_unnamed():
    assert display_name(None) == "Без названия"


def test_display_empty_meta_is_unnamed():
    assert display_name({}) == "Без названия"


def test_display_title_wins_over_last_actual():
    meta = {"title": "parser bugfix", "last_actual": "реализуй спеку по ренейму"}
    assert display_name(meta) == "parser bugfix"


def test_display_last_actual_when_title_empty():
    meta = {"title": "", "last_actual": "реализуй спеку по ренейму"}
    assert display_name(meta) == "реализуй спеку по ренейму"


def test_display_last_actual_when_title_missing():
    meta = {"last_actual": "реализуй спеку по ренейму"}
    assert display_name(meta) == "реализуй спеку по ренейму"


def test_display_title_whitespace_only_falls_back():
    assert display_name({"title": "   ", "last_actual": "fallback"}) == "fallback"


def test_display_title_stripped():
    assert display_name({"title": "  spaced title  "}) == "spaced title"


def test_display_last_actual_whitespace_only_falls_back_to_unnamed():
    assert display_name({"title": "", "last_actual": "   "}) == "Без названия"


def test_display_non_string_fields_ignored():
    assert display_name({"title": 0, "last_actual": None}) == "Без названия"


def test_display_task_wins_over_last_actual():
    meta = {"task": "Добавить поле task в turn_finish", "last_actual": "реализуй спеку"}
    assert display_name(meta) == "Добавить поле task в turn_finish"


def test_display_title_wins_over_task():
    meta = {
        "title": "parser bugfix",
        "task": "Добавить поле task",
        "last_actual": "реализуй спеку",
    }
    assert display_name(meta) == "parser bugfix"


def test_task_title_strips_and_truncates():
    assert task_title({"task": "  hello  "}) == "hello"
    assert task_title({"task": "   "}) == ""
    assert len(task_title({"task": "a" * (LAST_ACTUAL_MAX + 10)})) == LAST_ACTUAL_MAX


def test_task_subtitle_when_title_is_task():
    meta = {"task": "Закрыть баг", "last_actual": "посмотри логи"}
    assert task_subtitle(meta) == "посмотри логи"


def test_task_subtitle_empty_when_same_as_title():
    meta = {"task": "Закрыть баг", "last_actual": "Закрыть баг"}
    assert task_subtitle(meta) == ""


def test_task_subtitle_when_manual_title():
    meta = {"title": "parser", "task": "Закрыть баг", "last_actual": "посмотри логи"}
    assert task_subtitle(meta) == "Закрыть баг"


def test_task_subtitle_empty_without_task():
    assert task_subtitle({"last_actual": "привет"}) == ""


# ---- normalize_prompt_title --------------------------------------------

def test_normalize_collapses_whitespace():
    assert normalize_prompt_title("строка1\n строка2\t  три") == "строка1 строка2 три"


def test_normalize_truncates_to_max():
    text = "a" * (LAST_ACTUAL_MAX + 50)
    out = normalize_prompt_title(text)
    assert len(out) == LAST_ACTUAL_MAX
    assert out == "a" * LAST_ACTUAL_MAX


def test_normalize_empty_after_collapse():
    assert normalize_prompt_title("   \n\t  ") == ""


def test_normalize_keeps_short_text_unchanged():
    assert normalize_prompt_title("ты тут?") == "ты тут?"


def test_threshold_min_is_30():
    assert LAST_ACTUAL_MIN == 30
    assert LAST_ACTUAL_MAX == 200


# ---------------------------------------------------------------------------
# should_update_task: политика перезаписи meta.tui.task ходом (эвристика
# «имя обновляет только ход с реальной работой»).
# ---------------------------------------------------------------------------

def test_should_update_task_seeds_unconditionally_when_empty():
    from gc_tui.services.session_title import should_update_task
    assert should_update_task({}, turn_tool_calls=0) is True
    assert should_update_task(None, turn_tool_calls=0) is True
    assert should_update_task({"task": "   "}, turn_tool_calls=0) is True


def test_should_update_task_rejects_idle_turn_over_existing():
    from gc_tui.services.session_title import should_update_task
    meta = {"task": "починить парсер"}
    assert should_update_task(meta, turn_tool_calls=0) is False


def test_should_update_task_allows_working_turn_over_existing():
    from gc_tui.services.session_title import should_update_task
    meta = {"task": "починить парсер"}
    assert should_update_task(meta, turn_tool_calls=1) is True


def test_task_subtitle_skips_candidate_equal_to_manual_title():
    """«Ctrl+R, Enter» без правок: title == task → дубль не показываем,
    но отличный last_actual остаётся подзаголовком (и в поиске)."""
    from gc_tui.services.session_title import task_subtitle
    assert task_subtitle({"title": "Написать тесты", "task": "Написать тесты"}) == ""
    assert task_subtitle({"title": "Написать тесты", "task": "Написать тесты",
                          "last_actual": "напиши тесты к скрипту"}) == "напиши тесты к скрипту"
