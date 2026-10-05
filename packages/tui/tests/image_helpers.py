"""Общие помощники TUI-тестов про изображения. Импорт: `from image_helpers import
make_attachment` (каталог набора стоит на sys.path, как для form_harness)."""

from pathlib import Path

from gc_core.attachments.model import ORIGIN_USER, Attachment, user_label


def make_attachment(sha: str, n: int, **fields) -> Attachment:
    """Вложение пользователя с подписью `Изображение #n`; файл на диске не создаётся.
    `fields` перекрывают любые поля (путь, источник, размеры)."""
    values = dict(
        id=sha,
        kind="image",
        mime="image/png",
        width=640,
        height=480,
        size=1000,
        path=f"/staging/{sha}.png",
        origin=ORIGIN_USER,
        label=user_label(n),
        source="clipboard",
        orig_width=640,
        orig_height=480,
    )
    values.update(fields)
    return Attachment(**values)


def make_attachment_file(
    n: int,
    directory: Path,
    *,
    source: str = "скрин.png",
    mime: str = "image/png",
    width: int = 1456,
    height: int = 819,
) -> Attachment:
    """Вложение `Изображение #n` с файлом в `directory`: несколько байт — тестам
    ленты и Ctrl+клика нужен путь, а не пиксели."""
    ext = "png" if mime == "image/png" else "jpg"
    path = directory / f"img{n}.{ext}"
    path.write_bytes(b"\x89PNG")
    return make_attachment(
        f"{n:016x}",
        n,
        mime=mime,
        width=width,
        height=height,
        size=4,
        path=str(path),
        source=source,
        orig_width=width,
        orig_height=height,
    )
