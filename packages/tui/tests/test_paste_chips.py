"""Юнит-тесты сервиса paste_chips — без Textual."""

from gc_core.attachments.model import chip_text
from gc_tui.services.paste_chips import (
    CHIP_IMAGE,
    CHIP_TEXT,
    ChipStore,
    chip_label,
    expand,
    should_collapse,
    token_spans,
)
from image_helpers import make_attachment


def test_should_collapse_by_lines_boundary():
    assert should_collapse("a\n" * 4 + "a") is True  # 5 строк
    assert should_collapse("a\n" * 3 + "a") is False  # 4 строки


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


def test_chip_store_add_get_reset():
    s = ChipStore()
    cid, label = s.add("big")
    assert cid == 1 and s.get(1) == "big" and label == "[Вставка #1 · 3 симв.]"
    cid2, _ = s.add("big2")
    assert cid2 == 2
    cid3, _ = s.add("big3")  # монотонная нумерация
    assert cid3 == 3
    s.reset()
    assert s.get(2) is None
    cid4, _ = s.add("again")  # после reset — снова с 1
    assert cid4 == 1


def test_token_spans_locates_label():
    s = ChipStore()
    cid, label = s.add("a\n" * 4 + "a")
    text = f"before {label} after"
    assert token_spans(text, s) == [((CHIP_TEXT, cid), 0, 7, 7 + len(label))]


def test_token_spans_multiple_labels():
    s = ChipStore()
    _, l1 = s.add("a\n" * 4 + "a")
    _, l2 = s.add("b\n" * 5 + "b")
    text = f"{l1}{l2}"
    spans = token_spans(text, s)
    assert ((CHIP_TEXT, 1), 0, 0, len(l1)) in spans
    assert ((CHIP_TEXT, 2), 0, len(l1), len(l1) + len(l2)) in spans


def test_expand_interleaved_order():
    s = ChipStore()
    _, l1 = s.add("ALPHA\nALPHA\nALPHA\nALPHA\nALPHA")
    _, l2 = s.add("y" * 500)
    text = f"{l1} middle {l2} end"
    assert (
        expand(text, s)
        == "ALPHA\nALPHA\nALPHA\nALPHA\nALPHA middle " + "y" * 500 + " end"
    )


def test_expand_no_chips_is_identity():
    s = ChipStore()
    assert expand("just text", s) == "just text"


def test_expand_leaves_the_label_literal_after_reset():
    s = ChipStore()
    _, l1 = s.add("A\nA\nA\nA\nA")
    text = f"x {l1} y"
    s.reset()
    assert (
        expand(text, s) == f"x {l1} y"
    )  # записи нет → подпись остаётся литералом


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


def test_add_image_returns_the_chip_text_and_labels_the_attachment():
    s = ChipStore()
    assert s.add_image(make_attachment("a" * 16, 0), 5) == chip_text(5)
    assert s.images_in_text(chip_text(5)) == [
        make_attachment("a" * 16, 5)
    ]  # подпись — по номеру
    assert s.add("big")[0] == 1  # у текстовых плашек своя нумерация


def test_images_in_text_first_occurrence_per_id_in_text_order():
    s = ChipStore()
    a, b = make_attachment("a" * 16, 1), make_attachment("b" * 16, 2)
    s.add_image(a, 1)
    s.add_image(b, 2)
    s.add_image(
        make_attachment("a" * 16, 3), 3
    )  # та же картинка второй плашкой
    text = (
        f"{chip_text(2)} и\n{chip_text(1)} и {chip_text(3)} и {chip_text(2)}"
    )
    assert s.images_in_text(text) == [b, a]


def test_image_label_is_not_found_inside_a_longer_number():
    s = ChipStore()
    s.add_image(make_attachment("a" * 16, 1), 1)
    ten = make_attachment("b" * 16, 10)
    s.add_image(ten, 10)
    assert s.images_in_text(chip_text(10)) == [ten]
    assert token_spans(chip_text(10), s) == [
        ((CHIP_IMAGE, 10), 0, 0, len(chip_text(10)))
    ]


def test_expand_leaves_image_chips_as_labels():
    s = ChipStore()
    _, l1 = s.add("A\nA\nA\nA\nA")
    s.add_image(make_attachment("a" * 16, 1), 1)
    assert expand(f"{l1} {chip_text(1)}", s) == f"A\nA\nA\nA\nA {chip_text(1)}"


def test_renumber_images_moves_entries_and_rewrites_labels():
    """Смена сессии: номера — подряд с first_n, подпись вложения — новая (ядро не
    получит «Изображение #2» при тексте «#8»), словарь — chip_text(старый) → chip_text(новый)."""
    s = ChipStore()
    s.add_image(make_attachment("a" * 16, 2), 2)
    s.add_image(make_attachment("b" * 16, 5), 5)
    text = f"{chip_text(2)} {chip_text(5)}"
    assert s.renumber_images(8, text) == {
        chip_text(2): chip_text(8),
        chip_text(5): chip_text(9),
    }
    moved = s.images_in_text(f"{chip_text(8)} {chip_text(9)}")
    assert moved == [
        make_attachment("a" * 16, 8),
        make_attachment("b" * 16, 9),
    ]
    assert [att.label for att in moved] == ["Изображение #8", "Изображение #9"]
    assert s.images_in_text(chip_text(2)) == []


def test_renumber_images_with_overlapping_ranges_and_chips_out_of_order():
    """#1, #2 → #2, #3: новый номер одной плашки — старый номер другой. Порядок номеров —
    по старым номерам, а не по месту в тексте; каждая подпись ведёт к своей картинке."""
    s = ChipStore()
    a, b = make_attachment("a" * 16, 1), make_attachment("b" * 16, 2)
    s.add_image(a, 1)
    s.add_image(b, 2)
    text = f"{chip_text(2)} потом {chip_text(1)}"
    assert s.renumber_images(2, text) == {
        chip_text(1): chip_text(2),
        chip_text(2): chip_text(3),
    }
    renumbered = f"{chip_text(3)} потом {chip_text(2)}"
    assert [(x.id, x.label) for x in s.images_in_text(renumbered)] == [
        (b.id, "Изображение #3"),
        (a.id, "Изображение #2"),
    ]


def test_renumber_images_drops_records_whose_chips_left_the_text():
    """Запись стёртой плашки хранится для Ctrl+Z, но при смене сессии её номер не
    занимает место в последовательности нового треда — запись уходит."""
    s = ChipStore()
    s.add_image(make_attachment("a" * 16, 1), 1)
    s.add_image(make_attachment("b" * 16, 2), 2)
    assert s.renumber_images(5, f"только {chip_text(2)}") == {
        chip_text(2): chip_text(5)
    }
    assert s.labels() == {(CHIP_IMAGE, 5): chip_text(5)}


def test_add_images_numbers_new_images_and_reuses_known_ones():
    """Картинка, уже записанная в сторе (тот же id), получает свою плашку — номер не
    тратится; дубль внутри одной вставки — тоже."""
    s = ChipStore()
    a, b = make_attachment("a" * 16, 0), make_attachment("b" * 16, 0)
    assert s.add_images([a], 4) == ([chip_text(4)], 1)
    assert s.add_images([b, a, b], 5) == (
        [chip_text(5), chip_text(4), chip_text(5)],
        1,
    )
    assert s.images_in_text(f"{chip_text(4)} {chip_text(5)}") == [
        make_attachment("a" * 16, 4),
        make_attachment("b" * 16, 5),
    ]
