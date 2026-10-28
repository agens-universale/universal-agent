"""Чтение журнала правок для интерфейса: поиск по call_id и сводка хода.

Тонкая обёртка над `agent_edits.journal.EditsJournal` — без рендера, без
Textual, только данные. Индекс `call_id -> запись` строится лениво и
перестраивается, когда `read_all()` отдаёт новый список записей (журнал
дописан во время работы интерфейса); сам факт изменения журнала уже
отслеживает кэш `EditsJournal.read_all()` по (размер, mtime).

Сводка хода считается по ИТОГУ (`agent_edits.deltas.net_summary`): снимок
начала хода против диска. Сумма пошаговых счётчиков отвечала бы на другой
вопрос — «сколько работы проделано», — и на файле, переписанном несколько раз
за ход, разбухала кратно.

База для счёта — `counting_turn_baseline`: более ранняя из точного снимка
fs-тула и счётной базы шелл-канала. Точный снимок в одиночку оставлял бы ход,
где файл правила одна команда, со счётчиками журнала.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from gc_core.agent_edits.deltas import abs_path_for_key, net_summary
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.snapshots import (
    SnapshotStore,
    snapshots_root,
    thread_key_from_journal_path,
)
from gc_core.config_modules.agent_edits import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
)
from gc_core.thread_store import edits_journal_path

_logger = logging.getLogger(__name__)


def journal_signature(
    records: Iterable[EditRecord],
) -> Tuple[int, Optional[float]]:
    """Подпись набора записей — часть ключа любого кэша над журналом.

    Записи append-only, но сам файл может быть пересоздан целиком
    (удаление треда хранилищем при сбросе). Одной длины мало —
    новый журнал может дорасти ровно до прежнего числа записей, и кэш со
    старыми данными молча переживёт подмену файла. Пара (длина, ts последней
    записи) это ловит и остаётся O(1).
    """
    records = list(records)
    return (len(records), records[-1].ts if records else None)


def disk_signature(launch_root: str, records: Iterable[EditRecord]) -> Tuple:
    """Подписи (размер, mtime) файлов набора — вторая часть ключа кэша.

    Итог считается против ДИСКА, поэтому правка файла мимо агента обязана
    вызывать пересчёт так же, как дописанный журнал.
    """
    signature = []
    for path_key in sorted({rec.path for rec in records}):
        try:
            stat = abs_path_for_key(launch_root or "", path_key).stat()
        except OSError:
            signature.append((path_key, None))
        else:
            signature.append((path_key, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


class EditsLookup:
    """Индекс журнала правок одного треда для нужд UI."""

    def __init__(
        self,
        threads_dir: str,
        system_id: str,
        thread_id: str,
        launch_root: Optional[str] = None,
        max_diff_source_bytes: Optional[int] = None,
        hunk_context_lines: Optional[int] = None,
    ) -> None:
        path = edits_journal_path(threads_dir, system_id, thread_id)
        self._journal = EditsJournal(path)
        self._threads_dir = Path(threads_dir)
        self._thread_key = thread_key_from_journal_path(path)
        self._launch_root = (launch_root or "").strip() or None
        # Лимиты приходят от вызывающего (`tui.services.edits_view.diff_limits`):
        # None — дефолты, чтобы старые вызовы без конфига остались рабочими.
        self._max_source_bytes = (
            DEFAULT_MAX_DIFF_SOURCE_BYTES
            if max_diff_source_bytes is None
            else max_diff_source_bytes
        )
        self._hunk_context_lines = (
            DEFAULT_HUNK_CONTEXT_LINES
            if hunk_context_lines is None
            else hunk_context_lines
        )
        self._indexed_signature: Optional[Tuple[int, Optional[float]]] = None
        self._index: Dict[str, EditRecord] = {}
        self._turn_ids: List[str] = []
        self._summary_cache: Optional[
            Tuple[str, Any, Tuple[int, int, int]]
        ] = None

    def by_call_id(self, call_id: str) -> Optional[EditRecord]:
        self._ensure_index()
        return self._index.get(call_id)

    def turn_ids(self) -> List[str]:
        """Уникальные turn_id ходов с правками из тулов, в порядке появления.

        Ходы, собранные из одних шелл-записей (`call_id=None`), пропущены: они
        не дают блоков в ленте, и окно реплея на них тратить нечего."""
        self._ensure_index()
        return list(self._turn_ids)

    def turn_summary(self, turn_id: str) -> Tuple[int, int, int]:
        """(файлов, +, −) итога хода: снимок начала хода против диска.

        Результат кэшируется: сводку зовут на каждом завершении хода, а ключ
        кэша учитывает и состояние журнала, и подписи самих файлов — правка
        любой из сторон пересчёт вызывает, простой повтор вызова нет.
        """
        self._ensure_index()
        records = self.records_for_turn(turn_id)
        if not records:
            return (0, 0, 0)
        if self._launch_root is None:
            # Без launch_root не найти ни снапшоты, ни файлы на диске. Сумма
            # пошаговых счётчиков вместо итога — это те самые раздутые цифры,
            # ради которых всё и затевалось: честнее показать, что дельта
            # неизвестна. В TUI ветка недостижима (launch_root ставит рантайм).
            _logger.warning(
                "сводка правок без launch_root: дельта не считается"
            )
            return (len({r.path for r in records}), 0, 0)

        key = (
            self._indexed_signature,
            disk_signature(self._launch_root, records),
        )
        cached = self._summary_cache
        if cached is not None and cached[0] == turn_id and cached[1] == key:
            return cached[2]

        store = SnapshotStore(
            snapshots_root(self._launch_root),
            self._thread_key,
            self._threads_dir,
        )
        summary = net_summary(
            records,
            lambda path_key: store.counting_turn_baseline(path_key, turn_id),
            self._launch_root,
            max_source_bytes=self._max_source_bytes,
            hunk_context_lines=self._hunk_context_lines,
        )
        self._summary_cache = (turn_id, key, summary)
        return summary

    def last_turn_id(self) -> Optional[str]:
        return self._journal.last_turn_id()

    def records_for_turn(self, turn_id: str) -> List[EditRecord]:
        return self._journal.turn_records(turn_id)

    def _ensure_index(self) -> None:
        records = self._journal.read_all()
        # Почему подписи мало одной длины — см. `journal_signature`: журнал
        # append-only внутри файла, но сам файл пересоздаётся при сбросе треда
        # (удаление треда хранилищем, `-n`/новая сессия с тем же
        # thread_id), и индекс со старыми call_id пережил бы подмену молча.
        signature = journal_signature(records)
        if (
            self._indexed_signature is not None
            and signature == self._indexed_signature
        ):
            return
        index: Dict[str, EditRecord] = {}
        turns: Dict[str, None] = {}  # dict — упорядоченное множество ходов
        turn_of: Dict[str, str] = {}
        ambiguous: set = set()
        for rec in records:
            if rec.call_id:
                # Ходы без call_id (записи шелл-канала) блоков не дают — не
                # тратим на них окно реплея.
                turns[rec.turn_id] = None
            if rec.call_id is not None:
                # При нескольких записях на один call_id (тул правил
                # несколько файлов за один вызов) остаётся последняя.
                if turn_of.setdefault(rec.call_id, rec.turn_id) != rec.turn_id:
                    ambiguous.add(rec.call_id)
                index[rec.call_id] = rec
        # Один и тот же идентификатор в РАЗНЫХ ходах — не «несколько файлов за
        # вызов», а совпадение: уникальности его никто не обещал, он приходит от
        # провайдера как есть. Показать по нему запись значило бы соврать о том,
        # что агент сделал, поэтому такой путь просто молчит (пятый раунд
        # ревью, находка 2).
        for call_id in ambiguous:
            index.pop(call_id, None)
        self._index = index
        self._turn_ids = list(turns)
        self._indexed_signature = signature
