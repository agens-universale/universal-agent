"""Хранилище файлов картинок: карман процесса и файлы тредов.

Корень — ``<threads_dir>/images``; без каталога тредов (DB-хранилища SDK) —
``<system temp>/gc-images-<uid или имя пользователя>``. Внутри:

- файл треда ``<root>/<sys>_<tid>_<sha16>.<png|jpg>`` — плоско, по образцу
  ``data_`` / ``meta_`` / ``touched_``; имя короткое из-за MAX_PATH на Windows;
- карман процесса ``<root>/_staging/<pid>/<sha16>.<ext>`` — картинка до
  отправки и в CLI без ``-t``.

Имя файла — хеш нормализованных байт, поэтому запись идемпотентна: есть файл —
второй раз он не пишется. Запись атомарная: временный файл и замена. Сам
объект хранилища на диске ничего не создаёт: каталоги появляются при первой
записи (``stage`` / ``commit`` / ``snapshot``).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from dataclasses import replace
from typing import Any, Mapping

from gc_core.attachments.model import (
    CLIPBOARD_SOURCE,
    KIND_IMAGE,
    MAX_INPUT_BYTES,
    Attachment,
    attachments_of,
)
from gc_core.attachments.normalize import (
    MIME_JPEG,
    NormalizedImage,
    normalize_image,
)
from gc_core.thread_paths import (
    resolve_threads_dir,
    sanitize_system_id,
    thread_address,
    validate_thread_id,
)
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.pid_alive import is_pid_alive

logger = logging.getLogger(__name__)

IMAGES_DIRNAME = "images"
STAGING_DIRNAME = "_staging"

_TEMP_ROOT_PREFIX = "gc-images-"
_ID_HEX_CHARS = 16
_PID_DIRNAME = re.compile(r"[0-9]+")


class ImageStore:
    def __init__(self, root: str, *, pid: int | None = None) -> None:
        self.root = os.path.abspath(root)
        self.pid = os.getpid() if pid is None else pid
        self.staging_dir = os.path.join(
            self.root, STAGING_DIRNAME, str(self.pid)
        )

    @classmethod
    def for_threads_dir(cls, threads_dir: str) -> ImageStore:
        """Хранилище каталога тредов; ``""`` — свой каталог во временном каталоге системы."""
        if threads_dir:
            return cls(os.path.join(threads_dir, IMAGES_DIRNAME))
        return cls(_temp_root())

    @classmethod
    def for_config(cls, config_dict: Mapping[str, Any]) -> ImageStore:
        """Хранилище конфига запуска — единственное правило корня для ядра, охраны
        путей (``security/media_roots.py``), CLI и TUI.

        Каталог тредов — ``resolve_threads_dir``; абсолютного ``threads_data_path``
        в конфиге нет — свой каталог во временном каталоге системы. Хранилище
        тредов встраивающего SDK корень не меняет: иначе картинки легли бы мимо
        корня, который охрана считает ``media``.
        """
        try:
            threads_dir = resolve_threads_dir(config_dict)
        except ValueError:
            threads_dir = ""
        return cls.for_threads_dir(threads_dir)

    def stage(
        self, image: NormalizedImage, *, origin: str, label: str, source: str
    ) -> Attachment:
        """Положить картинку в карман процесса."""
        att_id = image_id(image.data)
        path = os.path.join(
            self.staging_dir, f"{att_id}.{_extension(image.mime)}"
        )
        return _put(
            image, att_id, path, origin=origin, label=label, source=source
        )

    def commit(
        self, att: Attachment, system_id: str, thread_id: str
    ) -> Attachment:
        """Копия из кармана в файл треда; вложение — с новым путём.

        Идемпотентно: файл треда уже есть — не переписывается. Не бросает
        никогда: копия не удалась (карман уже почищен, диск полон, путь длиннее
        MAX_PATH) — предупреждение в лог и вложение как было. Вызывающие к этому
        моменту уже сняли сообщение с очереди или приняли ответ человека —
        исключение потеряло бы их; при сборке запроса модель получит картинку из
        кармана или записку «файл недоступен», ход не падает.
        """
        path = self._thread_path(system_id, thread_id, att.id, att.mime)
        if not os.path.exists(path):
            try:
                with open(att.path, "rb") as handle:
                    data = handle.read()
                _write_atomic(path, data)
            except OSError as exc:
                logger.warning(
                    "image_commit_failed: картинка %s не скопирована в тред: %s",
                    att.id,
                    exc,
                )
                return att
        return replace(att, path=path)

    def snapshot(
        self,
        image: NormalizedImage,
        system_id: str,
        thread_id: str,
        *,
        origin: str,
        label: str,
        source: str,
    ) -> Attachment:
        """Записать картинку сразу в файл треда (снимок тула, минуя карман)."""
        att_id = image_id(image.data)
        path = self._thread_path(system_id, thread_id, att_id, image.mime)
        return _put(
            image, att_id, path, origin=origin, label=label, source=source
        )

    def read_for_send(self, att: Attachment) -> bytes | None:
        """Байты для запроса; ``None`` — файла нет или хеш не совпал с ``att.id``."""
        try:
            with open(att.path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        return data if image_id(data) == att.id else None

    def delete_thread_images(self, system_id: str, thread_id: str) -> None:
        """Удалить файлы треда; отказ по одному файлу не срывает сброс.

        Имя сверяется целиком: тред ``a`` не заденет ``a_b``.
        """
        name = re.compile(
            re.escape(_thread_prefix(system_id, thread_id))
            + r"[0-9a-f]{16}\.(?:png|jpg)"
        )
        try:
            entries = os.listdir(self.root)
        except FileNotFoundError:
            return
        except OSError:
            logger.warning(
                "image_list_failed: каталог картинок недоступен: %s",
                self.root,
                exc_info=True,
            )
            return
        for entry in entries:
            if name.fullmatch(entry):
                path = os.path.join(self.root, entry)
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(
                        "image_delete_failed: картинка не удалена: %s",
                        path,
                        exc_info=True,
                    )

    def clear_own_staging(self) -> None:
        """Очистить карман своего процесса — только на выходе процесса: сообщение,
        отправленное посреди запуска, ядро коммитит позже и берёт файл из кармана."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)

    def sweep_dead_staging(self) -> None:
        """Удалить карманы мёртвых процессов (старт TUI/CLI). Свой и живые не трогаются."""
        base = os.path.join(self.root, STAGING_DIRNAME)
        try:
            entries = os.listdir(base)
        except FileNotFoundError:
            return
        for entry in entries:
            if not _PID_DIRNAME.fullmatch(entry) or int(entry) == self.pid:
                continue
            if not is_pid_alive(int(entry)):
                shutil.rmtree(os.path.join(base, entry), ignore_errors=True)

    def _thread_path(
        self, system_id: str, thread_id: str, att_id: str, mime: str
    ) -> str:
        name = f"{_thread_prefix(system_id, thread_id)}{att_id}.{_extension(mime)}"
        return os.path.join(self.root, name)


def commit_to_run_thread(ctx: Any, att: Attachment) -> Attachment:
    """Копия вложения в тред прогона — единственный путь ядра «карман → тред».

    Хранилище — ``ctx.image_store``, адрес — ``thread_address(ctx)``. Зовут ход
    (сообщения запуска и принятые из очереди) и ``turn_ask_human`` (картинки
    ответа); фронты отдают вложения из кармана как есть (спека §3). Прогон без
    треда (CLI без ``-t``, SDK без ``thread_id``) или контекст без хранилища —
    вложение как есть, в кармане. Не бросает: сбой копии ``commit`` сам
    возвращает вложение как было.
    """
    store = getattr(ctx, "image_store", None)
    address = thread_address(ctx)
    if store is None or address is None:
        return att
    return store.commit(att, *address)


def with_committed_attachments(message: dict, ctx: Any) -> dict:
    """Сообщение очереди с вложениями, переложенными в тред прогона
    (``commit_to_run_thread``); без вложений — как есть. Зовёт тот, кто принимает
    сообщение из очереди в прогон: родитель и субагент (у ребёнка адрес треда —
    родителя).

    Копия словаря, а не правка на месте: его положил в очередь чужой поток
    (TUI, обработчик сервера), и держать на него ссылку он вправе.
    """
    atts = attachments_of(message)
    if not atts:
        return message
    return {
        **message,
        "attachments": [commit_to_run_thread(ctx, a).to_dict() for a in atts],
    }


async def amake_attachment(
    store: ImageStore,
    src: bytes | str,
    *,
    origin: str,
    label: str,
    source: str | None = None,
) -> Attachment:
    """Картинка из пути (``str``) или байт → вложение в кармане процесса.

    Чтение, нормализация и запись — одним заходом в потоке: нормализация
    5K-скриншота — сотня-другая миллисекунд CPU, на loop она заморозила бы
    интерфейс. ``source`` по умолчанию — полный путь файла или ``CLIPBOARD_SOURCE``.
    Ошибки: ``NormalizeError`` (не картинка, слишком большая), ``OSError``
    (файл не прочитать).
    """
    return await asyncio.to_thread(
        _stage_from_source, store, src, origin, label, source
    )


def _stage_from_source(
    store: ImageStore,
    src: bytes | str,
    origin: str,
    label: str,
    source: str | None,
) -> Attachment:
    if isinstance(src, str):
        with open(src, "rb") as handle:
            data = handle.read(
                MAX_INPUT_BYTES + 1
            )  # больше не нужно: лимит откажет
        default_source = os.path.abspath(src)
    else:
        data = src
        default_source = CLIPBOARD_SOURCE
    image = normalize_image(data)
    return store.stage(
        image, origin=origin, label=label, source=source or default_source
    )


def _put(
    image: NormalizedImage,
    att_id: str,
    path: str,
    *,
    origin: str,
    label: str,
    source: str,
) -> Attachment:
    if not os.path.exists(path):
        _write_atomic(path, image.data)
    return Attachment(
        id=att_id,
        kind=KIND_IMAGE,
        mime=image.mime,
        width=image.width,
        height=image.height,
        size=len(image.data),
        path=path,
        origin=origin,
        label=label,
        source=source,
        orig_width=image.orig_width,
        orig_height=image.orig_height,
    )


def _temp_root() -> str:
    """Корень без каталога тредов — у каждого пользователя свой (см. `_private_temp_root`)."""
    return _private_temp_root(tempfile.gettempdir())


#: Запасной корень процесса по временному каталогу — если общий оказался не нашим.
_FALLBACK_ROOTS: dict[str, str] = {}


def _private_temp_root(tempdir: str) -> str:
    """Корень без каталога тредов в ``tempdir`` — у каждого пользователя свой.

    На Linux ``/tmp`` общий, а периметр чтения ``media`` идёт по realpath: общий
    каталог под общим именем открыл бы агенту картинки чужих сессий. Имя
    предсказуемо, поэтому на POSIX каталог обязан быть нашим: создаём его сами с
    правами только владельцу, а существующий принимаем, только если это не ссылка
    и владелец — мы (права при этом закрываем). Подложенная другим пользователем
    ссылка или его каталог иначе увели бы и чтение агента без вопроса, и запись
    картинок туда, куда они ведут, — тогда берём свой приватный каталог процесса.
    На Windows временный каталог у пользователя свой.
    """
    if not hasattr(os, "getuid"):
        return os.path.join(tempdir, f"{_TEMP_ROOT_PREFIX}{getpass.getuser()}")
    path = os.path.join(tempdir, f"{_TEMP_ROOT_PREFIX}{os.getuid()}")
    # Не создался (уже есть, временный каталог только на чтение) — решает проверка
    # ниже; нет и запасного — ошибка наверх: лучше отказ, чем чужой корень.
    with contextlib.suppress(OSError):
        os.mkdir(path, 0o700)
    if _close_own_dir(path):
        return path
    if tempdir not in _FALLBACK_ROOTS:
        _FALLBACK_ROOTS[tempdir] = tempfile.mkdtemp(
            prefix=_TEMP_ROOT_PREFIX, dir=tempdir
        )
    return _FALLBACK_ROOTS[tempdir]


def _close_own_dir(path: str) -> bool:
    """``path`` — наш каталог (не ссылка, владелец — мы); права — только владельцу."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        return False
    return True


def image_id(data: bytes) -> str:
    """Id картинки — первые 16 hex sha256 её нормализованных байт."""
    return hashlib.sha256(data).hexdigest()[:_ID_HEX_CHARS]


def _extension(mime: str) -> str:
    return "jpg" if mime == MIME_JPEG else "png"


def _thread_prefix(system_id: str, thread_id: str) -> str:
    return f"{sanitize_system_id(system_id)}_{validate_thread_id(thread_id)}_"


def _write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        atomic_replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
