"""Рекордер правок: единая точка записи в журнал агента.

fs-тулы после успешной правки файла зовут ``record_edit`` — функция считает
unified diff, sha256 обеих сторон, снимает session/turn-снапшоты «до правки»
(а у созданного файла, у которого «до» нет, ставит отметку создания) и
добавляет запись в журнал треда (``EditsJournal``). Лимиты диффа берутся из
секции ``agent_edits`` конфига на ctx (как во втором канале записи,
``shell_delta``); одноимённый параметр вызова, если передан, побеждает конфиг.
``is_recording`` — дешёвый предикат для тулов: стоит ли вообще поднимать в
память старый контент ради журнала.

Soft-fail: любое исключение внутри логируется и глотается — правка файла
уже произошла, запись в журнал вторична и не должна ронять тул.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from gc_core.agent_edits.ctx import (
    identity_from_ctx,
    launch_root_from_ctx,
    limit,
    protected_extra_from_ctx,
    recording_enabled,
)
from gc_core.agent_edits.deltas import (
    build_diff,
    decode_edit_text,
    truncate_hunks,
)
from gc_core.agent_edits.git_ops import head_rev_from_files
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.paths import canonical_path_key, is_external_key
from gc_core.agent_edits.snapshots import (
    SnapshotStore,
    snapshots_root,
    thread_key_from_journal_path,
)
from gc_core.config_modules.agent_edits import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
    DEFAULT_SNAPSHOTS_MAX_FILES,
)
from gc_core.thread_store import edits_journal_path
from gc_core.tools.subagents.sidecar import current_turn_id
from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    classify_content_from_sample,
)

_logger = logging.getLogger(__name__)


def is_recording(ctx: Any) -> bool:
    """Ведётся ли запись правок для этого ctx: режим запуска её включил
    (``agent_edits.ctx.recording_enabled``) и есть thread-identity.

    Дешёвый предикат без файловых операций: тулу нужно знать заранее, стоит
    ли поднимать в память старый контент файла ради журнала. Периметр пути
    здесь не проверяется — он известен только по конкретному пути правки.
    """
    return recording_enabled(ctx) and identity_from_ctx(ctx) is not None


async def record_edit(
    ctx: Any,
    *,
    abs_path: str,
    old_bytes: bytes | None,
    new_bytes: bytes | None,
    op: str,
    call_id: str | None,
    backup: bool = False,
    partial: bool = False,
    max_diff_source_bytes: int | None = None,
    hunk_context_lines: int | None = None,
    max_diff_chars: int | None = None,
) -> None:
    """Записать одну правку файла в журнал треда.

    Единая точка записи для fs-тулов: считает дифф, хэши, снимает снапшоты
    «до правки» и добавляет запись в ``EditsJournal``. Никогда не бросает —
    правка файла уже произошла, любая ошибка здесь глотается и логируется.

    Сама работа (``_record_edit``) блокирующая по существу: дифф, хэши и
    снимки базы на диск — это синхронный ввод-вывод и CPU. Раньше это было не
    страшно — агент исполнялся синхронно. После перехода ядра на корутины
    (fs-тулы — ``async def acall`` на общем event loop) та же самая работа,
    исполненная прямо в корутине, морозит loop на время диффа и записи
    снапшотов — не код изменился, а то, ГДЕ он исполняется. Поэтому
    ``record_edit`` — корутина, а тяжёлая часть уходит в поток через
    ``asyncio.to_thread``.

    Лимиты (``max_diff_source_bytes``/``hunk_context_lines``/``max_diff_chars``)
    по умолчанию — ``None``: значение берётся из секции ``agent_edits`` конфига на
    ctx, а переданное явно побеждает конфиг.

    ``partial=True`` — вызывающий знает лишь фрагмент правки, а не оба полных
    состояния файла (append сверх капа: есть дописанный кусок, полного файла
    нет). Тогда не пишутся ни снапшот, ни отметка создания, оба ``*_sha256``
    — ``None`` (в поля хэша файла нельзя класть хэш куска: цепочка целостности
    прочтёт это как «файл правился извне»), в ``flags`` добавляется
    ``partial``. Хэнки и счётчики считаются как обычно — по тому, что передали.
    """
    try:
        await asyncio.to_thread(
            _record_edit,
            ctx,
            abs_path=abs_path,
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            op=op,
            call_id=call_id,
            backup=backup,
            partial=partial,
            max_diff_source_bytes=max_diff_source_bytes,
            hunk_context_lines=hunk_context_lines,
            max_diff_chars=max_diff_chars,
        )
    except Exception:
        _logger.exception(
            "record_edit failed: abs_path=%s op=%s", abs_path, op
        )


def _record_edit(
    ctx: Any,
    *,
    abs_path: str,
    old_bytes: bytes | None,
    new_bytes: bytes | None,
    op: str,
    call_id: str | None,
    backup: bool,
    partial: bool,
    max_diff_source_bytes: int | None,
    hunk_context_lines: int | None,
    max_diff_chars: int | None,
) -> None:
    if backup:
        return

    # Часть тулов зовёт запись без предварительной проверки (у них старое
    # содержимое и так прочитано) — режим запуска проверяется здесь.
    if not recording_enabled(ctx):
        return
    identity = identity_from_ctx(ctx)
    if identity is None:
        return

    max_diff_source_bytes = limit(
        max_diff_source_bytes,
        ctx,
        "max_diff_source_bytes",
        DEFAULT_MAX_DIFF_SOURCE_BYTES,
    )
    hunk_context_lines = limit(
        hunk_context_lines,
        ctx,
        "hunk_context_lines",
        DEFAULT_HUNK_CONTEXT_LINES,
    )
    max_diff_chars = limit(
        max_diff_chars, ctx, "max_diff_chars", DEFAULT_MAX_DIFF_CHARS
    )

    threads_dir, system_id, thread_id = identity
    journal_path = edits_journal_path(threads_dir, system_id, thread_id)
    thread_key = thread_key_from_journal_path(journal_path)

    launch_root = launch_root_from_ctx(ctx)
    path_key = (
        canonical_path_key(
            abs_path,
            launch_root,
            protected_extra=protected_extra_from_ctx(ctx),
        )
        if launch_root
        else None
    )
    if path_key is None:
        return

    if old_bytes is None and new_bytes is None:
        return

    flags: list[str] = []
    hunks = ""
    add = 0
    del_ = 0
    take_snapshot = False
    take_created_mark = False

    if (
        _is_binary_side(abs_path, old_bytes)
        or _is_binary_side(abs_path, new_bytes)
        or _exceeds_cap(old_bytes, max_diff_source_bytes)
        or _exceeds_cap(new_bytes, max_diff_source_bytes)
    ):
        flags = ["no_diff"]
    else:
        old_text = "" if old_bytes is None else decode_edit_text(old_bytes)
        new_text = "" if new_bytes is None else decode_edit_text(new_bytes)
        if old_text is None or new_text is None:
            flags = ["no_diff"]
        else:
            hunks, add, del_ = build_diff(
                old_text, new_text, hunk_context_lines
            )
            hunks, flags = truncate_hunks(hunks, max_diff_chars)
            # Взаимоисключающие ветки одного вопроса «что было до правки»: был
            # контент — снимаем его; не было И вызывающий сказал «create» —
            # файл создан здесь, и ступень HEAD ему может дать только отметка
            # создания (Д1). Бинарь и файл сверх капа диффа не получают ни
            # того, ни другого — как и раньше.
            #
            # `op` в условии обязателен, одного `old_bytes is None` мало:
            # ПЕРЕЗАПИСЬ существующего файла приходит без старого контента,
            # когда файл больше капа мутирующих тулов или чтение отказало
            # (`write_to_file_tool`, `save_base64_file`). Отметка объявила бы
            # базу такого пути пустой, и файл, живший до сессии — возможно,
            # грязный, — получил бы ступень HEAD.
            #
            # Гвард при этом НЕ точный, и знать это обязательно: `op="create"`
            # приходит и по существующему файлу НУЛЕВОГО размера — так его
            # видит `append_to_file` (`size == 0` → `is_create`, там решается
            # BOM, а не история пути), и старый контент в этой ветке не
            # поднимается вовсе. Отметка там не врёт о содержимом: `b""` —
            # правдивое «до» пустого файла, а ступень HEAD всё равно требует
            # НАСТОЯЩЕГО коммита именно этого пути (§3.2). Щель одна — путь,
            # усечённый шелл-командой раньше в этой же сессии: его честный
            # `.orig` (и записанная с ним ревизия) отметке уступать не должен.
            # Закрыто в сторе: `mark_created` молчит, если точный снимок у
            # пути уже есть.
            take_snapshot = old_bytes is not None
            take_created_mark = old_bytes is None and op == "create"

    if partial:
        # `partial` — правка файла, чьё «до» в память не поднимали: старого
        # контента нет, но файл БЫЛ. Отметка создания тут соврала бы о пустой
        # базе так же, как снимок соврал бы о содержимом.
        take_snapshot = False
        take_created_mark = False
        flags = [*flags, "partial"]

    if is_external_key(path_key):
        # Файл вне проекта: запись нужна ленте, база не нужна никому. Снимок
        # означал бы копию чужого содержимого в каталоге проекта, а отметка
        # создания — ступень отсчёта у пути, который на экране не появится.
        take_snapshot = False
        take_created_mark = False

    store = None
    wrote_session = False
    if take_snapshot or take_created_mark:
        # Свой try — как у соседа по каналу (`shell_delta._record`): снимок
        # базы это ВОЗМОЖНОСТЬ ОТКАТА, а запись ниже — основной контракт
        # («видно, что агент изменил»). Необязательное не вправе ронять
        # обязательное: отказ файловой системы здесь (путь длиннее предела ОС,
        # нет места, файл занят чужим процессом) обрывал всю функцию, внешний
        # `except` глотал исключение, и правка исчезала отовсюду разом — из
        # ленты, с экрана правок и из счётчиков хода, молча. Цена отказа
        # теперь ровно та, что он стоит: у пути не будет базы для отката.
        try:
            store = SnapshotStore(
                snapshots_root(launch_root), thread_key, Path(threads_dir)
            )
            # Признак ступени HEAD фиксируется вместе с первой записью о пути
            # (§3.2): ревизия читается ФАЙЛАМИ `.git`, без подпроцесса —
            # горячий путь тула не должен платить `rev-parse` за каждый первый
            # контакт с файлом.
            head_rev = head_rev_from_files(launch_root)
            if take_created_mark:
                store.mark_created(path_key, head_rev=head_rev)
            else:
                wrote_session = store.capture_session(
                    path_key, old_bytes, head_rev=head_rev
                )
                turn_id = current_turn_id(ctx)
                if turn_id is not None:
                    store.capture_turn(path_key, old_bytes, turn_id)
        except Exception:
            # `store`/`wrote_session` здесь НЕ сбрасываются: снимок сессии мог
            # уже лечь, а упасть — следующий за ним снимок хода. Обнулив флаг,
            # мы пропустили бы эвикцию, и хранилище переросло бы свой лимит.
            _logger.warning(
                "снимок базы не снят, запись всё равно идёт path=%r",
                path_key,
                exc_info=True,
            )

    rec = EditRecord(
        path=path_key,
        turn_id=current_turn_id(ctx) or "",
        ts=time.time(),
        call_id=call_id,
        op=op,
        hunks=hunks,
        add=add,
        del_=del_,
        before_sha256=None if partial else _sha256_or_none(old_bytes),
        after_sha256=None if partial else _sha256_or_none(new_bytes),
        flags=flags,
    )
    EditsJournal(journal_path).append(rec)

    # Эвикция — только когда session-снапшот реально записан (один раз на файл на
    # тред): capture_session зовётся на каждой правке, а evict_lru обходит каталог
    # целиком. Строго ПОСЛЕ записи журнала: живость треда evict_lru проверяет по
    # наличию edits_<thread_key>.jsonl, и до первого append свои же свежие снимки
    # оказались бы кандидатами на вытеснение.
    if wrote_session and store is not None:
        store.evict_lru(
            limit(
                None, ctx, "snapshots_max_files", DEFAULT_SNAPSHOTS_MAX_FILES
            )
        )


def _is_binary_side(path: str, data: bytes | None) -> bool:
    if data is None:
        return False
    sample = data[:BINARY_SAMPLE_BYTES]
    return classify_content_from_sample(path, sample).is_binary


def _exceeds_cap(data: bytes | None, cap: int) -> bool:
    return data is not None and len(data) > cap


def _sha256_or_none(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None
