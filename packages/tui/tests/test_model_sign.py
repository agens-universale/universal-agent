from gc_tui.palette import ACCENT, BRAND, DIM, INK, RUN_COLOR
from gc_tui.services import model_sign as ms

SLOTS = {"light": "fast", "default": "main", "max": "main"}


def _colors(text):
    if text.spans:
        return [str(s.style) for s in text.spans]
    return [str(text.style)]


def test_levels_follow_explicit_slots_only():
    assert ms.levels_of("fast", SLOTS) == ("light",)
    assert ms.levels_of("main", SLOTS) == ("default", "max")
    assert ms.levels_of("other", SLOTS) == ()
    assert ms.levels_of("x", {"light": None, "default": None}) == ()


def test_dots_colors_per_level_free_and_auto():
    assert ms.dots("auto", SLOTS).plain == "●"
    assert _colors(ms.dots("auto", SLOTS)) == [BRAND]
    assert _colors(ms.dots("main", SLOTS)) == [ACCENT, RUN_COLOR]
    assert _colors(ms.dots("fast", SLOTS)) == [INK]
    assert _colors(ms.dots("other", SLOTS)) == [DIM]
    assert ms.dots("other", SLOTS).plain == "●"


def test_level_word_choice_shows_slot_model():
    assert _colors(ms.dots("max", SLOTS)) == [RUN_COLOR]
    sign = ms.sign("max", SLOTS, alias="main")
    assert sign.plain == "●● main"
    assert [str(sp.style) for sp in sign.spans[:2]] == [ACCENT, RUN_COLOR]
    bare = ms.sign("max", SLOTS)
    assert bare.plain == "● max"
    assert str(bare.spans[0].style) == RUN_COLOR
    assert ms.sign("fast", SLOTS).plain == "● fast"
    assert ms.sign("auto", SLOTS).plain == "● auto"


def test_parts_split_dots_and_name_with_styles():
    dots, name = ms.parts(ms.sign("main", SLOTS, name_style=DIM))
    assert (dots.plain, name.plain) == ("●●", "main")
    assert _colors(dots) == [ACCENT, RUN_COLOR]
    assert str(name.spans[0].style) == DIM
