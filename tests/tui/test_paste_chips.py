"""Юнит-тесты сервиса paste_chips — без Textual."""
from tui.services.paste_chips import (
    should_collapse, chip_label, ChipStore, token_spans, expand,
)


def test_should_collapse_by_lines_boundary():
    assert should_collapse("a\n" * 4 + "a") is True    # 5 строк
    assert should_collapse("a\n" * 3 + "a") is False   # 4 строки


def test_should_collapse_by_chars_boundary():
    assert should_collapse("x" * 500) is True
    assert should_collapse("x" * 499) is False


def test_should_collapse_blank():
    assert should_collapse("") is False
    assert should_collapse("   \n  ") is False


def test_chip_label_multiline():
    assert chip_label(1, "a\n" * 4 + "a") == "[Вставка #1 · 5 строк]"


def test_chip_label_single_line_chars():
    assert chip_label(2, "x" * 500) == "[Вставка #2 · 500 симв.]"


def test_chip_label_plural_lines():
    assert chip_label(1, "a\na") == "[Вставка #1 · 2 строки]"
    assert chip_label(1, "\n".join(["a"] * 11)) == "[Вставка #1 · 11 строк]"
    assert chip_label(1, "\n".join(["a"] * 21)) == "[Вставка #1 · 21 строка]"


def test_chip_store_add_get_remove_reset():
    s = ChipStore()
    cid, label = s.add("big")
    assert cid == 1 and s.get(1) == "big" and label == "[Вставка #1 · 3 симв.]"
    cid2, _ = s.add("big2")
    assert cid2 == 2
    s.remove(1)
    assert s.get(1) is None
    cid3, _ = s.add("big3")          # стабильная нумерация — продолжаем с 3
    assert cid3 == 3
    s.reset()
    assert s.get(2) is None
    cid4, _ = s.add("again")         # после reset — снова с 1
    assert cid4 == 1


def test_token_spans_locates_label():
    s = ChipStore()
    cid, label = s.add("a\n" * 4 + "a")
    text = f"before {label} after"
    assert token_spans(text, s) == [(cid, 0, 7, 7 + len(label))]


def test_token_spans_multiple_labels():
    s = ChipStore()
    _, l1 = s.add("a\n" * 4 + "a")
    _, l2 = s.add("b\n" * 5 + "b")
    text = f"{l1}{l2}"
    spans = token_spans(text, s)
    assert (1, 0, 0, len(l1)) in spans
    assert (2, 0, len(l1), len(l1) + len(l2)) in spans


def test_expand_interleaved_order():
    s = ChipStore()
    _, l1 = s.add("ALPHA\nALPHA\nALPHA\nALPHA\nALPHA")
    _, l2 = s.add("y" * 500)
    text = f"{l1} middle {l2} end"
    assert expand(text, s) == "ALPHA\nALPHA\nALPHA\nALPHA\nALPHA middle " + "y" * 500 + " end"


def test_expand_no_chips_is_identity():
    s = ChipStore()
    assert expand("just text", s) == "just text"


def test_expand_ignores_removed_chip():
    s = ChipStore()
    _, l1 = s.add("A\nA\nA\nA\nA")
    text = f"x {l1} y"
    s.remove(1)
    assert expand(text, s) == f"x {l1} y"   # маппинга нет → подпись остаётся литералом


def test_expand_does_not_reexpand_embedded_label():
    s = ChipStore()
    # полный текст плашки #1 буквально содержит будущую подпись плашки #2
    _, l1 = s.add("Z\nZ\nZ\nZ\n[Вставка #2 · 3 симв.]")
    _, l2 = s.add("abc")
    assert l2 == "[Вставка #2 · 3 симв.]"
    out = expand(f"{l1}::{l2}", s)
    # вложенная подпись внутри текста #1 остаётся литералом; разворачивается
    # только настоящая ссылка на плашку #2
    assert out == "Z\nZ\nZ\nZ\n[Вставка #2 · 3 симв.]::abc"
