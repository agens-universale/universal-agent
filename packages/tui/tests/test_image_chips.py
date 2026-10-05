"""image_chips: плашки картинок в отправленном тексте — поиск, вырезание, сцепка."""

from gc_core.attachments.model import chip_text

from gc_tui.services.image_chips import (
    chip_number,
    chip_of,
    chip_spans,
    images_mentioned,
    strip_image_chips,
)
from image_helpers import make_attachment_file


def test_chip_of_is_the_core_chip_text(tmp_path):
    assert chip_of(make_attachment_file(8, tmp_path)) == chip_text(8)


def test_chip_number_reads_the_user_label(tmp_path):
    assert chip_number(make_attachment_file(12, tmp_path)) == 12


def test_chip_spans_finds_every_occurrence_in_text_order(tmp_path):
    a8, a9 = (
        make_attachment_file(8, tmp_path),
        make_attachment_file(9, tmp_path),
    )
    line = f"{chip_text(9)} и {chip_text(8)} и снова {chip_text(9)}"
    spans = chip_spans(line, [a8, a9])
    assert [(line[s:e], att.id) for s, e, att in spans] == [
        (chip_text(9), a9.id),
        (chip_text(8), a8.id),
        (chip_text(9), a9.id),
    ]


def test_chip_spans_does_not_find_1_inside_10(tmp_path):
    assert chip_spans(chip_text(10), [make_attachment_file(1, tmp_path)]) == []


def test_strip_image_chips_drops_labels_with_their_space(tmp_path):
    a8, a9 = (
        make_attachment_file(8, tmp_path),
        make_attachment_file(9, tmp_path),
    )
    text = f"{chip_text(8)} {chip_text(9)} почему кнопка съехала?"
    assert strip_image_chips(text, [a8, a9]) == "почему кнопка съехала?"
    assert (
        strip_image_chips(f"сравни {chip_text(8)} и {chip_text(9)}", [a8, a9])
        == "сравни и"
    )


def test_strip_image_chips_of_an_image_only_message_is_empty(tmp_path):
    assert (
        strip_image_chips(chip_text(8), [make_attachment_file(8, tmp_path)])
        == ""
    )


def test_images_mentioned_keeps_text_order_and_skips_absent(tmp_path):
    a3, a4, a5 = (make_attachment_file(n, tmp_path) for n in (3, 4, 5))
    index = {att.label: att for att in (a3, a4, a5)}
    assert images_mentioned(f"{chip_text(5)} потом {chip_text(3)}", index) == (
        a5,
        a3,
    )
