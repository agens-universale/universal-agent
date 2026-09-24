"""Токены палитры для диффов правок агента (DIFF_ADD_BG/DIFF_DEL_BG/…):
присутствие в css_variables(), валидность hex, различимость на фоне выделения
и внутри своей пары фон/акцент. Метрика различимости — переиспользуется из
tests/tui/test_palette_sync.py (не изобретаем свою)."""
from __future__ import annotations

import re

from test_palette_sync import _distance, _luminance

from gc_tui import palette


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Тот же порог, что использует test_selection_distinguishable_from_focus для
# различимости двух тёмных поверхностных цветов.
_DISTANCE_THRESHOLD = 40

_DIFF_TOKENS = {
    "diff-add-bg": "DIFF_ADD_BG",
    "diff-del-bg": "DIFF_DEL_BG",
    "diff-add-hl": "DIFF_ADD_HL",
    "diff-del-hl": "DIFF_DEL_HL",
    "diff-gutter": "DIFF_GUTTER",
    "diff-add-gutter": "DIFF_ADD_GUTTER",
    "diff-del-gutter": "DIFF_DEL_GUTTER",
}

_GUTTER_BY_KIND = ("DIFF_ADD_GUTTER", "DIFF_DEL_GUTTER")


def test_diff_tokens_present_in_css_variables():
    variables = palette.css_variables()
    missing = sorted(name for name in _DIFF_TOKENS if name not in variables)
    assert not missing, f"нет в css_variables(): {missing}"


def test_diff_tokens_are_valid_hex():
    variables = palette.css_variables()
    for kebab_name in _DIFF_TOKENS:
        value = variables[kebab_name]
        assert _HEX_RE.match(value), f"${kebab_name}={value!r} — не hex-цвет"


def test_diff_row_backgrounds_distinguishable_from_selection():
    """Фон изменённой/удалённой строки не должен сливаться с выделением мышью."""
    for token in (palette.DIFF_ADD_BG, palette.DIFF_DEL_BG):
        assert _distance(token, palette.SCREEN_SELECTION_BACKGROUND) >= _DISTANCE_THRESHOLD


def test_diff_intraline_accents_brighter_than_row_backgrounds():
    """Intraline-акцент рисуется поверх фона строки — должен заметно выделяться."""
    assert _distance(palette.DIFF_ADD_HL, palette.DIFF_ADD_BG) >= _DISTANCE_THRESHOLD
    assert _distance(palette.DIFF_DEL_HL, palette.DIFF_DEL_BG) >= _DISTANCE_THRESHOLD


def test_diff_add_and_del_backgrounds_distinguishable():
    """Самая важная пара: фон добавленной строки не должен сливаться с фоном
    удалённой — иначе цветовая дифференциация диффа теряет смысл."""
    assert _distance(palette.DIFF_ADD_BG, palette.DIFF_DEL_BG) >= _DISTANCE_THRESHOLD


def test_diff_row_backgrounds_distinguishable_from_base_bg():
    """Фон строки диффа должен быть заметен на общем фоне экрана — иначе дифф
    невидим на фоне остального текста."""
    for token in (palette.DIFF_ADD_BG, palette.DIFF_DEL_BG):
        assert _distance(token, palette.BG) >= _DISTANCE_THRESHOLD


def test_gutter_tokens_readable_on_block_background():
    """Номер строки сидит на фоне БЛОКА, а не на заливке строки (заливка
    начинается после гаттера). Ориентир — контекстный DIFF_GUTTER: цветные
    номера не должны читаться на этом фоне хуже него."""
    baseline = _distance(palette.DIFF_GUTTER, palette.BLOCK_BG)
    for name in _GUTTER_BY_KIND:
        assert _distance(getattr(palette, name), palette.BLOCK_BG) >= baseline, name


def test_gutter_tokens_are_the_semantic_ok_err():
    """Колонка правки красится РОВНО семантическими OK/ERR — теми же зелёным
    галочек и красным ошибок, что уже приняты в интерфейсе (живая правка
    2026-08-11). Дифф не заводит собственный «почти зелёный»: один смысл — один
    цвет во всём TUI. Две попытки-предшественницы отвергнуты на смоуке: примесь
    к MUTED вышла темнее обоих базовых цветов, примесь к INK — светлее, но
    обесцвеченной; насыщенность тут важнее светимости."""
    assert palette.DIFF_ADD_GUTTER == palette.OK
    assert palette.DIFF_DEL_GUTTER == palette.ERR


def test_gutter_tokens_differ_from_muted_context():
    """Колонка правки не должна сливаться с контекстной.

    Метрика — дистанция, а не светимость: ERR темнее серого MUTED, но отличается
    от него тоном, и на экране это читается мгновенно."""
    for name in _GUTTER_BY_KIND:
        assert _distance(getattr(palette, name),
                         palette.DIFF_GUTTER) >= _DISTANCE_THRESHOLD, name


def test_gutter_add_and_del_distinguishable():
    """Иначе тип строки не считывается по колонке — вся затея бессмысленна."""
    assert _distance(palette.DIFF_ADD_GUTTER,
                     palette.DIFF_DEL_GUTTER) >= _DISTANCE_THRESHOLD


def test_markers_readable_on_their_row_backgrounds():
    """Маркер `+`/`-` красится цветом гаттера своей строки, но лежит уже НА
    заливке — контраст проверяется против неё, а не против фона блока."""
    assert _distance(palette.DIFF_ADD_GUTTER, palette.DIFF_ADD_BG) >= _DISTANCE_THRESHOLD
    assert _distance(palette.DIFF_DEL_GUTTER, palette.DIFF_DEL_BG) >= _DISTANCE_THRESHOLD
