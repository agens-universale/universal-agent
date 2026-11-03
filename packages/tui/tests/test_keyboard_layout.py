"""Перевод набранного между раскладками ЙЦУКЕН и QWERTY."""

from gc_tui.services.keyboard_layout import swap_layout


def test_latin_word_typed_on_russian_layout_maps_back():
    assert swap_layout("ьщвуд") == "model"


def test_latin_word_maps_to_russian_layout():
    assert swap_layout("model") == "ьщвуд"


def test_russian_word_typed_on_latin_layout_maps_back():
    assert swap_layout("c;fnm") == "сжать"


def test_shifted_keys_keep_case_and_colon():
    assert swap_layout("зфслЖтфьу") == "pack:name"


def test_keys_without_counterpart_stay():
    assert swap_layout("qa-1 ") == "йф-1 "
