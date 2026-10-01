"""Нормализация картинки: одна форма на выходе, отказы — до декодирования, где можно.

Картинки рисуются здесь же Pillow'ом; бинарных фикстур в репозитории нет.
Заголовок PNG «на 50 мегапикселей» собирается руками: сама картинка в память
не поднимается, а ``Image.open`` читает только заголовок.
"""

import random
import struct
import warnings
import zlib
from io import BytesIO

import pytest
from PIL import Image

from gc_core.attachments.model import JPEG_THRESHOLD, MAX_INPUT_BYTES
from gc_core.attachments.normalize import (
    MIME_JPEG,
    MIME_PNG,
    SNIFF_BYTES,
    NormalizeError,
    _is_clean_jpeg,
    looks_like_image,
    normalize_image,
)


def _encode(img: Image.Image, fmt: str, **params) -> bytes:
    buffer = BytesIO()
    img.save(buffer, format=fmt, **params)
    return buffer.getvalue()


def _decode(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    img.load()
    return img


def _png_header_only(width: int, height: int) -> bytes:
    """PNG из одних IHDR и IEND: размер есть, пикселей нет."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_small_png_keeps_its_size_and_format():
    result = normalize_image(
        _encode(Image.new("RGB", (200, 100), (10, 20, 30)), "PNG")
    )
    assert (result.mime, result.width, result.height) == (MIME_PNG, 200, 100)
    assert (result.orig_width, result.orig_height) == (200, 100)
    assert _decode(result.data).getpixel((5, 5)) == (10, 20, 30)


def test_retina_5k_screenshot_is_scaled_to_the_long_edge():
    """Review Focus №1: скриншот 5120×2880 ужимается до длинной стороны 1568."""
    shot = Image.linear_gradient("L").resize((5120, 2880)).convert("RGB")
    result = normalize_image(_encode(shot, "PNG"))
    assert (result.width, result.height) == (1568, 882)
    assert (result.orig_width, result.orig_height) == (5120, 2880)
    assert _decode(result.data).size == (1568, 882)
    assert len(result.data) <= JPEG_THRESHOLD


def test_noisy_5k_screenshot_is_scaled_then_reencoded_as_jpeg():
    """Review Focus №1, ветка «ужать, затем JPEG»: у шумного 5K (фото, зерно) PNG даже
    после ужатия тяжелее порога. Источник — BMP: он кодируется мгновенно и не
    выходит за MAX_INPUT_BYTES."""
    size = (5120, 2880)
    noise = Image.frombytes(
        "RGB", size, random.Random(0).randbytes(size[0] * size[1] * 3)
    )
    result = normalize_image(_encode(noise, "BMP"))
    assert (result.mime, result.width, result.height) == (MIME_JPEG, 1568, 882)
    assert (result.orig_width, result.orig_height) == size
    assert _decode(result.data).format == "JPEG"


def test_small_jpeg_is_kept_byte_for_byte():
    data = _encode(
        Image.new("RGB", (300, 200), (200, 50, 50)), "JPEG", quality=90
    )
    result = normalize_image(data)
    assert result.data == data
    assert (result.mime, result.width, result.height) == (MIME_JPEG, 300, 200)


def test_normalizing_twice_gives_the_same_bytes():
    """Id картинки — хеш нормализованных байт: повторный прогон (снимок тулом
    сохранённого файла) обязан дать тот же id, иначе ломаются «уже в контексте» и
    «одна картинка — один раз» (ревью 2026-10-01)."""
    noisy = Image.effect_noise((800, 600), 60).convert("RGB")
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    for data in (
        _encode(noisy, "JPEG", quality=85, exif=exif),
        _encode(Image.effect_noise((2400, 1800), 90).convert("RGB"), "PNG"),
    ):
        once = normalize_image(data).data
        assert normalize_image(once).data == once


def test_small_jpeg_loses_its_metadata():
    """EXIF (GPS, серийный номер, программа) провайдеру не уходит — как у всех
    остальных картинок, которые мы перекодируем (ревью 2026-10-01)."""
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[0x0131] = "SecretSoft 1.0"
    data = _encode(
        Image.new("RGB", (300, 200), (200, 50, 50)),
        "JPEG",
        quality=90,
        exif=exif,
    )
    assert dict(Image.open(BytesIO(data)).getexif())
    result = normalize_image(data)
    assert dict(Image.open(BytesIO(result.data)).getexif()) == {}


def _with_segment(jpeg: bytes, marker: int, payload: bytes) -> bytes:
    """JPEG с дописанным сразу после SOI сегментом ``marker``."""
    length = (len(payload) + 2).to_bytes(2, "big")
    return jpeg[:2] + bytes([0xFF, marker]) + length + payload + jpeg[2:]


def test_jpeg_fill_byte_cannot_hide_exif_before_the_scan():
    """FF FF E1 не должен давать ложный переход через EXIF прямо к SOS."""
    clean = _encode(
        Image.new("RGB", (300, 200), (200, 50, 50)), "JPEG", quality=90
    )
    exif = Image.Exif()
    exif[0x010F] = "PrivatePhone"
    exif[0xA431] = "PrivateSerial"
    # Длина APP1 подобрана так, чтобы ошибочный разбор FF FF как сегмента
    # перепрыгивал EXIF и весь заголовок, попадая на SOS.
    payload = exif.tobytes().ljust(57213, b"\x00")
    with_exif = _with_segment(clean, 0xE1, payload)
    source = with_exif[:2] + b"\xff" + with_exif[2:]
    assert _decode(source).size == (300, 200)
    assert Image.open(BytesIO(source)).getexif()[0xA431] == "PrivateSerial"
    result = normalize_image(source).data
    assert dict(Image.open(BytesIO(result)).getexif()) == {}
    assert b"PrivatePhone" not in result
    assert b"PrivateSerial" not in result
    assert normalize_image(result).data == result


@pytest.mark.parametrize("fill_count", [1, 2, 4])
@pytest.mark.parametrize("marker", [0xE1, 0xFE])
def test_jpeg_metadata_after_fill_bytes_is_removed(fill_count, marker):
    clean = _encode(Image.new("RGB", (300, 200)), "JPEG")
    with_metadata = _with_segment(clean, marker, b"private metadata")
    source = with_metadata[:2] + b"\xff" * fill_count + with_metadata[2:]
    assert _decode(source).size == (300, 200)
    result = normalize_image(source).data
    assert b"private metadata" not in result
    assert normalize_image(result).data == result


@pytest.mark.parametrize("fill_count", [1, 2, 4])
def test_clean_jpeg_with_fill_bytes_is_unchanged(fill_count):
    clean = _encode(Image.new("RGB", (300, 200)), "JPEG")
    source = clean[:2] + b"\xff" * fill_count + clean[2:]
    assert normalize_image(source).data == source


@pytest.mark.parametrize("marker", [0xE0, 0xDA])
@pytest.mark.parametrize("length", [0, 1, 65535])
def test_jpeg_invalid_segment_length_is_not_trusted(marker, length):
    source = (
        b"\xff\xd8"
        + bytes([0xFF, marker])
        + length.to_bytes(2, "big")
        + b"\xff\xd9"
    )
    assert not _is_clean_jpeg(source)


def test_small_jpeg_with_any_other_app_segment_is_reencoded():
    """Не только EXIF: C2PA-манифест (APP11) несёт автора и устройство — мелкий JPEG
    как есть уходит, только если в нём нет ничего, кроме JFIF, профиля и Adobe
    (ревью 2026-10-01)."""
    clean = _encode(
        Image.new("RGB", (300, 200), (200, 50, 50)), "JPEG", quality=90
    )
    signed = _with_segment(clean, 0xEB, b"JP\x00\x00jumb c2pa manifest")
    result = normalize_image(signed).data
    assert b"c2pa manifest" not in result
    assert normalize_image(result).data == result


@pytest.mark.parametrize("ending", [b"", b"\xff\xd9"])
def test_small_jpeg_trailer_after_the_image_does_not_leave(ending):
    """«Живое фото»: за концом картинки вшито видео (со своими координатами) —
    провайдеру оно не уходит (ревью 2026-10-01)."""
    clean = _encode(
        Image.new("RGB", (300, 200), (200, 50, 50)), "JPEG", quality=90
    )
    result = normalize_image(clean + b"ftypmp42 video with gps" + ending).data
    assert b"video with gps" not in result


@pytest.mark.parametrize("marker", [0xE1, 0xEB, 0xFE])
def test_progressive_jpeg_metadata_between_scans_does_not_leave(marker):
    clean = _encode(
        Image.new("RGB", (300, 200), (200, 50, 50)),
        "JPEG",
        progressive=True,
    )
    assert normalize_image(clean).data == clean
    second_scan = clean.index(b"\xff\xda", clean.index(b"\xff\xda") + 2)
    segment = _with_segment(b"\xff\xd8", marker, b"private metadata")[2:]
    with_metadata = clean[:second_scan] + segment + clean[second_scan:]
    result = normalize_image(with_metadata).data
    assert b"private metadata" not in result
    assert _decode(result).size == (300, 200)
    assert normalize_image(result).data == result


def test_large_jpeg_is_scaled_and_reencoded():
    result = normalize_image(
        _encode(Image.new("RGB", (3000, 2000), (200, 50, 50)), "JPEG")
    )
    assert (result.mime, result.width, result.height) == (MIME_PNG, 1568, 1045)
    assert (result.orig_width, result.orig_height) == (3000, 2000)


def test_exif_orientation_is_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # «повернуть на 90° по часовой»
    data = _encode(Image.new("RGB", (40, 80), (0, 0, 255)), "JPEG", exif=exif)
    result = normalize_image(data)
    assert (result.width, result.height) == (80, 40)
    assert (result.orig_width, result.orig_height) == (80, 40)
    assert result.data != data


def test_transparency_is_flattened_on_white():
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (0, 0, 20, 40))
    flat = _decode(normalize_image(_encode(img, "PNG")).data)
    assert flat.mode == "RGB"
    assert flat.getpixel((5, 5)) == (255, 0, 0)
    assert flat.getpixel((30, 5)) == (255, 255, 255)


def test_all_zero_alpha_is_ignored():
    """DIBV5 из буфера Windows: альфа нулевая целиком, цвет — в RGB."""
    img = Image.new("RGBA", (40, 40), (10, 20, 30, 0))
    flat = _decode(normalize_image(_encode(img, "PNG")).data)
    assert flat.getpixel((5, 5)) == (10, 20, 30)


def test_animated_gif_keeps_the_first_frame():
    frames = [
        Image.new("RGB", (40, 40), (255, 0, 0)),
        Image.new("RGB", (40, 40), (0, 0, 255)),
    ]
    buffer = BytesIO()
    frames[0].save(
        buffer, format="GIF", save_all=True, append_images=frames[1:]
    )
    red, green, blue = _decode(
        normalize_image(buffer.getvalue()).data
    ).getpixel((5, 5))
    assert red > 200 and blue < 50


def test_16_bit_gray_is_scaled_not_clipped():
    img = Image.new("I;16", (40, 40), 32768)
    flat = _decode(normalize_image(_encode(img, "PNG")).data)
    assert (flat.mode, flat.getpixel((5, 5))) == ("L", 128)


def test_cmyk_becomes_rgb():
    data = _encode(Image.new("CMYK", (40, 40), (0, 255, 255, 0)), "JPEG")
    result = normalize_image(data)
    assert result.mime == MIME_PNG
    assert _decode(result.data).mode == "RGB"


@pytest.mark.parametrize(
    "size, expected",
    [
        ((10, 10), (32, 32)),
        ((20, 100), (32, 160)),
        ((1, 2000), (32, 1568)),  # полоса: оба правила сразу не выполнить
    ],
)
def test_short_edge_is_raised_to_the_minimum(size, expected):
    result = normalize_image(
        _encode(Image.new("RGB", size, (0, 128, 0)), "PNG")
    )
    assert (result.width, result.height) == expected


def test_heavy_png_is_reencoded_as_jpeg():
    noise = Image.merge(
        "RGB", [Image.effect_noise((1200, 1200), 128) for _ in range(3)]
    )
    assert len(_encode(noise, "PNG")) > JPEG_THRESHOLD
    result = normalize_image(_encode(noise, "PNG"))
    assert result.mime == MIME_JPEG
    assert _decode(result.data).format == "JPEG"


def test_bytes_over_the_limit_are_refused_before_decoding():
    data = _encode(Image.new("RGB", (40, 40)), "PNG") + bytes(MAX_INPUT_BYTES)
    with pytest.raises(NormalizeError) as err:
        normalize_image(data)
    assert err.value.reason == "too_large"


def test_pixels_over_the_limit_are_refused_before_load():
    with pytest.raises(NormalizeError) as err:
        normalize_image(_png_header_only(8000, 6300))  # 50.4 Мп
    assert err.value.reason == "too_large"


@pytest.mark.parametrize("action", ["error", "ignore"])
def test_decompression_bomb_warning_is_too_large_under_any_filter(action):
    """100 Мп: Pillow предупреждает внутри ``open``. Под фильтром ``error``
    предупреждение летит исключением, под ``ignore`` — отказывает своя проверка."""
    with warnings.catch_warnings():
        warnings.simplefilter(action, Image.DecompressionBombWarning)
        with pytest.raises(NormalizeError) as err:
            normalize_image(_png_header_only(10000, 10000))
    assert err.value.reason == "too_large"


def test_decompression_bomb_error_is_too_large():
    with pytest.raises(NormalizeError) as err:
        normalize_image(
            _png_header_only(20000, 20000)
        )  # 400 Мп — выше 2×MAX_IMAGE_PIXELS
    assert err.value.reason == "too_large"


def test_global_pillow_pixel_limit_is_untouched():
    before = Image.MAX_IMAGE_PIXELS
    normalize_image(_encode(Image.new("RGB", (40, 40)), "PNG"))
    assert Image.MAX_IMAGE_PIXELS == before


def test_eps_is_refused():
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 40 40\n%%EndComments\nshowpage\n%%EOF\n"
    assert (
        Image.open(BytesIO(eps)).format == "EPS"
    )  # без ограничения Pillow его узнаёт
    with pytest.raises(NormalizeError) as err:
        normalize_image(eps)
    assert err.value.reason == "not_an_image"


@pytest.mark.parametrize(
    "data",
    [
        b"hello, world",
        _encode(Image.effect_noise((200, 200), 64), "PNG")[
            :600
        ],  # оборванный PNG
    ],
)
def test_garbage_is_not_an_image(data):
    with pytest.raises(NormalizeError) as err:
        normalize_image(data)
    assert err.value.reason == "not_an_image"


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF"])
def test_looks_like_image_knows_every_allowed_format(fmt):
    head = _encode(Image.new("RGB", (8, 8)), fmt)[:SNIFF_BYTES]
    assert looks_like_image(head) is True


@pytest.mark.parametrize(
    "head",
    [b"", b"hello, world!!!!", b"BMW report 2026.", b"%!PS-Adobe-3.0 E"],
)
def test_looks_like_image_rejects_other_bytes(head):
    assert looks_like_image(head) is False
