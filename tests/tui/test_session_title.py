"""tui/services/session_title: чистые функции имени сессии (title/last_actual).

display_name — единый источник имени для пикера и OSC:
  title (ручной) > last_actual (авто из промпта) > «Без названия».
normalize_prompt_title — collapse whitespace + truncate до LAST_ACTUAL_MAX,
порог обновления last_actual — LAST_ACTUAL_MIN по уже нормализованной строке.
"""
from tui.services.session_title import (
    LAST_ACTUAL_MAX,
    LAST_ACTUAL_MIN,
    display_name,
    normalize_prompt_title,
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
