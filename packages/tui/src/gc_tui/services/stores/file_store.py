"""FileTaskStore — треды TUI через фасад ``Threads``.

TUI работает с локальным файловым хранилищем, и его экраны синхронные:
вызовы фасада идут через ``run_sync``.

Записи истории экраны получают в форме конверта develop
(``{"type": "message", "seq", "ts", "payload", …}``) — разбор трейса прежний.
Курсор хвоста (``TaskCursor``) для экранов непрозрачен:
  position_token  → курсор фасада (поколение треда, версия истории, seq);
                    ``None`` — треда ещё не было
  ordering_key    → последний прочитанный seq
  storage_epoch   → «поколение:версия истории»
  archives        → число архивов сжатия треда

Итог хода — поток ``turns``: хранилище отдаёт потоки пачками по потоку,
поэтому запись встаёт по привязке (``after`` — последнее сообщение хода),
а не по месту в пачке: за своим сообщением, а его нет в пачке (прочитано
раньше) — в начало пачки. Полное чтение (снимок, сегменты) снимает
записи-сироты: сообщения их хода в истории нет (откат, сброс).

Сжатие (новая версия истории того же поколения и ровно один новый архив)
хвост отдаёт шагом (``CompactionStep``): трейс дописывает хвост архива,
маркер и новую живую историю, а не перерисовывается. Остальная замена
истории и пересоздание треда — сброс показа. Сжатие в хранилище — одна
запись: промежуточных состояний читатель не видит.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from gc_core.message_format.serialization import (
    calc_history_chars_for_trigger,
)
from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
    user_content_to_text,
)
from gc_core.persistence.live_volume import live_volume_from_messages
from gc_core.persistence.turn_outcome import (
    TURN_OUTCOME_TYPE,
    is_turn_outcome_record,
    turn_shown_in,
)
from gc_core.threads.facade import Cursor, TailBatch, Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import (
    TURNS,
    StreamEntry,
    ThreadKey,
    ThreadRecord,
)

from gc_tui.models import CompactionStep, TailResult, TaskCursor, TaskSummary
from gc_tui.services.stores.record_view import outcome_status, tui_section
from gc_tui.services.trace_mapper import _clean_summary
from gc_tui.services.turn_outcome import status_of_last_turn

logger = logging.getLogger(__name__)

_HISTORY = "messages"
_TUI = "tui"
#: Потоки ленты: история и итоги хода.
_FEED = (_HISTORY, TURNS)
# Сколько раз перечитать снимок, пока тред не перестанет меняться между
# чтением записи треда и его истории: каждое сжатие рушит одну попытку, а
# следующее требует вызова модели.
_SNAPSHOT_ATTEMPTS = 3


class FileTaskStore:
    """Треды одного каталога; ``system`` — система тредов TUI по умолчанию."""

    def __init__(self, threads_dir: str, system: str = "cli") -> None:
        self._threads = Threads(FileThreadStore(threads_dir))
        self._system = system

    def _key(self, task_id: str, system: Optional[str] = None) -> ThreadKey:
        return ThreadKey(system=system or self._system, thread=task_id)

    # ---- listing ------------------------------------------------------

    def list_tasks(
        self, *, limit: int = 200, offset: int = 0
    ) -> list[TaskSummary]:
        records = run_sync(_collect(self._threads, self._system))
        items = [_summary(record) for record in records]
        items.sort(key=lambda s: s.updated_at, reverse=True)
        return items[offset : offset + limit]

    # ---- per-task -----------------------------------------------------

    def get_record(
        self, task_id: str, system: Optional[str] = None
    ) -> Optional[ThreadRecord]:
        """Запись треда (части: ``info``, ``state``, ``usage``, ``runtime``,
        ``frontend``, ``extra``); нет треда или он не читается — ``None``."""
        try:
            return run_sync(self._threads.get(self._key(task_id, system)))
        except (OSError, ValueError):
            return None

    def update_tui_meta(
        self, task_id: str, patch: dict, system: Optional[str] = None
    ) -> None:
        """Слить ``patch`` в состояние TUI треда (атомарно в хранилище; шаг
        исполнителя его не трогает). Треда нет — он создаётся."""
        run_sync(
            self._threads.update_frontend(
                self._key(task_id, system), _TUI, dict(patch)
            )
        )

    def append_messages(
        self, task_id: str, messages: list[dict], system: Optional[str] = None
    ) -> int:
        """Дописать сообщения в историю (уже сохранённые не повторяются)."""
        return run_sync(
            self._threads.append_messages(self._key(task_id, system), messages)
        )

    def clear_plan(self, task_id: str, system: Optional[str] = None) -> bool:
        """Убрать план из сессии (`/plan clear`); True — план был. Решение и
        запись — в фасаде ``Threads`` (под арендой треда)."""
        return run_sync(self._threads.clear_plan(self._key(task_id, system)))

    def delete_task(self, task_id: str, system: Optional[str] = None) -> None:
        """Полностью удалить тред: историю, архивы, трассу, картинки."""
        run_sync(self._threads.delete(self._key(task_id, system)))

    def discard_message_by_id(
        self,
        task_id: str,
        message_id: str,
        system: Optional[str] = None,
        *,
        tags: Sequence[str] = (),
        strip_think: bool = True,
    ) -> bool:
        """Удалить одно сообщение из треда по id (откат осиротевшей реплики)
        и пересчитать объём живой истории."""
        if not message_id:
            return False

        def recount(messages: list[dict]) -> dict:
            return live_volume_from_messages(
                messages, tags=tags, strip_think=strip_think
            )

        return run_sync(
            self._threads.discard_message(
                self._key(task_id, system), message_id, recount=recount
            )
        )

    def read_subagent_trace(
        self, task_id: str, system: Optional[str] = None
    ) -> list[dict]:
        """Трасса субагентов треда — записи в форме develop."""
        entries = run_sync(
            self._threads.subagent_events(self._key(task_id, system))
        )
        return [{"ts": e.ts, "seq": e.seq, **e.data} for e in entries]

    # ---- read & tail --------------------------------------------------

    @staticmethod
    def is_compaction_summary(rec: dict) -> bool:
        """Запись — синтетический dialog_summary (лидирующая строка после компакции).

        Публичный предикат: TUI определяет «была ли компакция» по голове
        live-истории (после turn_end-сжатия первая запись = последняя сводка)
        без доп. I/O.
        """
        payload = rec.get("payload")
        if not isinstance(payload, dict) or payload.get("role") != "user":
            return False
        return is_dialog_summary_user_message_text(
            user_content_to_text(payload.get("content"))
        )

    def read_all_events(
        self, task_id: str, system: Optional[str] = None
    ) -> tuple[list[dict], TaskCursor]:
        """Вся живая история и курсор сразу за ней — одним снимком; в
        курсоре и число архивов: по нему тик узнает шаг сжатия."""
        key = self._key(task_id, system)
        view = run_sync(_aview(self._threads, key))
        if view is None:
            return [], TaskCursor(None, 0, None)
        batch, archives = view
        return _complete(_feed(key, batch)), _task_cursor(
            batch.cursor, archives
        )

    def read_raw_thread_jsonl(
        self, task_id: str, system: Optional[str] = None
    ) -> str:
        """Весь тред (архивы сжатия + живая история) строками конвертов
        develop — сырьё для /export log-json. В отличие от read_segments НЕ
        отбрасывает лидирующие dialog_summary архивов."""
        import json

        key = self._key(task_id, system)
        parts = run_sync(self._threads.parts(key))
        return "".join(
            json.dumps(_envelope(key, entry), ensure_ascii=False) + "\n"
            for part in parts
            for entry in part
        )

    def read_segments(
        self, task_id: str, system: Optional[str] = None
    ) -> list[dict]:
        """Тред → сегменты (части между сжатиями). Read-only реконструкция.

        Каждый сегмент: {"index", "messages": [envelope-records], "compacted_by":
        {"ts", "messages_in_segment", "synthesis", "chars_before", "chars_after"} | None}.
        Сегмент i = часть i минус лидирующая dialog_summary; его synthesis =
        лидирующая dialog_summary СЛЕДУЮЩЕЙ части; у последней (живой) — None.
        """
        key = self._key(task_id, system)
        parts, turns = run_sync(_aparts_and_turns(self._threads, key))
        # parsed[i] = (seg_records, leading_summary_record_or_None)  # noqa: ERA001  # описание формата, не выключенный код
        parsed: list[tuple[list[dict], Optional[dict]]] = []
        for part in parts:
            leading, seg = _split_leading([_envelope(key, e) for e in part])
            parsed.append((seg, leading))

        segments: list[dict] = []
        for i, (seg, _leading) in enumerate(parsed):
            compacted_by = None
            if i + 1 < len(parsed):
                next_leading = parsed[i + 1][1]
                if next_leading is not None:
                    compacted_by = _compaction(seg, next_leading)
            segments.append(
                {"index": i, "messages": seg, "compacted_by": compacted_by}
            )
        _place_in_segments(segments, parsed, turns)
        return segments

    def tail_events(
        self, task_id: str, cursor: TaskCursor, system: Optional[str] = None
    ) -> TailResult:
        """Хвост истории для опроса UI. Блокировку треда держит пишущий
        агент, а на Windows ``msvcrt.locking`` после ~10 с ожидания бросает
        ``OSError``: тик пропускается — следующий дочитает, — а не роняет
        TUI."""
        try:
            return self._tail_events(task_id, cursor, system)
        except OSError:
            logger.debug("хвост треда %s не прочитан", task_id, exc_info=True)
            return TailResult(new_records=[], next_cursor=cursor)

    def _tail_events(
        self, task_id: str, cursor: TaskCursor, system: Optional[str]
    ) -> TailResult:
        key = self._key(task_id, system)
        if cursor.position_token is None:
            # Треда при прошлом чтении не было: появился — читаем с начала,
            # как дописанный файл, без сброса показа.
            view = run_sync(_aview(self._threads, key))
            if view is None:
                return TailResult(new_records=[], next_cursor=cursor)
            batch, archives = view
            return TailResult(
                new_records=_feed(key, batch),
                next_cursor=_task_cursor(batch.cursor, archives),
            )
        batch = run_sync(self._threads.tail(key, _FEED, cursor.position_token))
        if batch.reset:
            step = run_sync(_acompaction_step(self._threads, key, cursor))
            if step is not None:
                return step
            return TailResult(
                new_records=[],
                next_cursor=_task_cursor(batch.cursor, cursor.archives),
                stream_reset=True,
                reason=_reset_reason(cursor.position_token, batch.cursor),
            )
        return TailResult(
            new_records=_feed(key, batch),
            next_cursor=_task_cursor(batch.cursor, cursor.archives),
        )


async def _collect(threads: Threads, system: str) -> list[ThreadRecord]:
    return [record async for record in threads.iter_threads(system=system)]


async def _aview(
    threads: Threads, key: ThreadKey
) -> Optional[tuple[TailBatch, int]]:
    """Живая история, курсор за ней и число архивов — согласованные: тред,
    сжатый между чтением его записи и истории, читается заново (иначе
    курсор новой версии истории нёс бы прежнее число архивов, и следующее
    сжатие стало бы сбросом). Треда нет — ``None``."""
    for _ in range(_SNAPSHOT_ATTEMPTS):
        record = await threads.store.get_thread(key)
        if record is None:
            return None
        start = Cursor(record.generation, record.history_version)
        batch = await threads.tail(key, _FEED, start)
        if not batch.reset:
            break
    return batch, len(record.archives)


async def _acompaction_step(
    threads: Threads, key: ThreadKey, cursor: TaskCursor
) -> Optional[TailResult]:
    """Замена истории — шаг сжатия? ``None`` — нет, сброс показа.

    Шаг — то же поколение, версия истории на одну дальше и ровно один новый
    архив, в котором курсор стоит на прочитанной записи, а живая история
    начинается сводкой. Шаг несёт хвост архива за курсором, сводку сжатия и
    новую живую историю."""
    seen: Cursor = cursor.position_token
    record = await threads.store.get_thread(key)
    if (
        record is None
        or record.generation != seen.generation
        or record.history_version != seen.history_version + 1
        or len(record.archives) != cursor.archives + 1
    ):
        return None
    archived = [
        _envelope(key, e)
        for e in await threads.store.read_archive(key, record.archives[-1].id)
    ]
    live = await threads.store.read_stream(key, _HISTORY)
    after = await threads.store.get_thread(key)
    if after is None or after.history_version != record.history_version:
        return None
    last = seen.seq.get(_HISTORY, 0)
    if last > _max_seq(archived):
        return None
    archive_leading, archive_seg = _split_leading(archived)
    live_leading, live_records = _split_leading(
        [_envelope(key, e) for e in live]
    )
    if live_leading is None:
        return None
    step = CompactionStep(
        archive_tail=[
            r for r in archived if r["seq"] > last and r is not archive_leading
        ],
        segment_index=len(record.archives) - 1,
        compacted_by=_compaction(archive_seg, live_leading),
        window_ids=_payload_ids([*archived, *live_records]),
        live_records=live_records,
    )
    # Итоги хода шаг не несёт: курсор их потока прежний, следующий тик
    # дочитает их после шага.
    next_cursor = Cursor(
        record.generation,
        record.history_version,
        {_HISTORY: live[-1].seq, TURNS: seen.seq.get(TURNS, 0)},
    )
    return TailResult(
        new_records=[],
        next_cursor=_task_cursor(next_cursor, len(record.archives)),
        compaction=step,
    )


#: Статусы меты, которые держит живой (или убитый посреди хода) ход.
_LIVE_STATUSES = frozenset({"running", "asking"})


def _list_status(record: ThreadRecord) -> Optional[str]:
    """Исход сессии для списка: короткий итог последнего хода (его пишет
    ядро в любом фронтенде), кроме живого хода и хода, открытого без
    итога (``runtime.turn``), — у них статус меты. Итога нет — статус
    меты (TUI — ``tui.status``, CLI и сервер — верхний). Тред, ход
    которого приостановлен на вопросе (A2A), ждёт ответа: ход не закончен.
    """
    if record.state.pending_question:
        return "asking"
    status = outcome_status(record)
    if status in _LIVE_STATUSES or record.runtime.turn is not None:
        return status
    return status_of_last_turn(record.runtime.last_turn) or status


def _summary(record: ThreadRecord) -> TaskSummary:
    tui_meta = tui_section(record)
    # Исход сессии не записан → None: список не выдумывает «stopped».
    usage = record.usage
    written = bool(usage.model_fields_set or usage.model_extra)
    unread = record.extra.get("unread_count", 0)
    last_error = record.extra.get("last_error")
    return TaskSummary(
        thread_id=record.key.thread,
        status=_list_status(record),
        updated_at=record.info.updated_at or "",
        messages_count=record.info.messages_count,
        skill_id=tui_meta.get("skill_id"),
        unread_count=unread if type(unread) is int else 0,
        last_error=last_error if isinstance(last_error, str) else None,
        tui_meta=tui_meta,
        token_usage=usage.model_dump() if written else None,
    )


def _envelope(key: ThreadKey, entry: StreamEntry) -> dict:
    """Запись истории в форме строки develop."""
    return {
        "type": "message",
        "thread_id": key.thread,
        "system": key.system,
        "seq": entry.seq,
        "ts": entry.ts,
        "payload": entry.data,
    }


def _turn_envelope(key: ThreadKey, entry: StreamEntry) -> dict:
    """Запись итога хода в форме, которую ждут читатели ленты."""
    return {
        "type": TURN_OUTCOME_TYPE,
        "thread_id": key.thread,
        "system": key.system,
        "seq": entry.seq,
        "ts": entry.ts,
        **entry.data,
    }


def _feed(key: ThreadKey, batch: TailBatch) -> list[dict]:
    """Пачка хвоста одним списком: записи итога — за сообщением своей
    привязки, а без него в пачке — в начале (их ход прочитан раньше)."""
    records = [
        _envelope(key, e) for stream, e in batch.entries if stream == _HISTORY
    ]
    turns = [
        _turn_envelope(key, e)
        for stream, e in batch.entries
        if stream == TURNS
    ]
    return _placed(records, turns)


def _placed(records: list[dict], turns: list[dict]) -> list[dict]:
    lead, after = [], {}
    ids = _payload_ids(records)
    for turn in turns:
        anchor = turn.get("after")
        if anchor in ids:
            after.setdefault(anchor, []).append(turn)
        else:
            lead.append(turn)
    out = list(lead)
    for rec in records:
        out.append(rec)
        out.extend(after.get((rec.get("payload") or {}).get("id"), ()))
    return out


def _complete(records: list[dict]) -> list[dict]:
    """Полное чтение: без записей-сирот (их хода нет в истории)."""
    ids = _payload_ids(records)
    return [
        r
        for r in records
        if not is_turn_outcome_record(r) or turn_shown_in(r, ids)
    ]


async def _aparts_and_turns(
    threads: Threads, key: ThreadKey
) -> tuple[list[list[StreamEntry]], list[dict]]:
    parts = await threads.parts(key)
    entries = await threads.turns(key) if parts else []
    return parts, [_turn_envelope(key, e) for e in entries]


def _place_in_segments(
    segments: list[dict], parsed: list, turns: list[dict]
) -> None:
    """Записи итога — в сегмент своего хода, за сообщением привязки (или
    за репликой хода, если привязка — сводка сжатия); сироты снимаются."""
    where: dict[str, int] = {}
    for i, (seg, leading) in enumerate(parsed):
        for rec in [leading, *seg] if leading else seg:
            mid = (rec.get("payload") or {}).get("id")
            if isinstance(mid, str):
                where.setdefault(mid, i)
    for turn in turns:
        if not turn_shown_in(turn, where):
            continue
        anchor = turn.get("after")
        if anchor not in where:
            anchor = (turn.get("outcome") or {}).get("opened_by")
        _insert_after(segments[where[anchor]]["messages"], anchor, turn)


def _insert_after(records: list[dict], anchor: str, turn: dict) -> None:
    at = next(
        (
            i + 1
            for i, rec in enumerate(records)
            if (rec.get("payload") or {}).get("id") == anchor
        ),
        0,
    )
    while at < len(records) and is_turn_outcome_record(records[at]):
        at += 1
    records.insert(at, turn)


def _task_cursor(cursor: Cursor, archives: int) -> TaskCursor:
    return TaskCursor(
        position_token=cursor,
        ordering_key=cursor.seq.get(_HISTORY, 0),
        storage_epoch=f"{cursor.generation}:{cursor.history_version}",
        archives=archives,
    )


def _reset_reason(before: Cursor, after: Cursor) -> str:
    if before.generation != after.generation:
        return "thread recreated"
    return "storage_epoch changed"


def _compaction(seg: list[dict], leading: dict) -> dict:
    seg = [r for r in seg if not is_turn_outcome_record(r)]
    payload = leading.get("payload") or {}
    synthesis = _clean_summary(user_content_to_text(payload.get("content")))
    # Мера триггера ядра: аргументы вызовов и вывод тулов тоже объём.
    chars_before = calc_history_chars_for_trigger(
        [r.get("payload") or {} for r in seg]
    )
    return {
        "ts": leading.get("ts", ""),
        "messages_in_segment": len(seg),
        "synthesis": synthesis,
        "chars_before": chars_before,
        "chars_after": len(synthesis),
    }


def _split_leading(recs: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """(лидирующая dialog_summary или None, записи без неё)."""
    if recs and FileTaskStore.is_compaction_summary(recs[0]):
        return recs[0], recs[1:]
    return None, recs


def _payload_ids(records: list[dict]) -> frozenset[str]:
    ids: set[str] = set()
    for rec in records:
        payload = rec.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            ids.add(payload["id"])
    return frozenset(ids)


def _max_seq(records: list[dict]) -> int:
    seqs = [r.get("seq") for r in records]
    return max((s for s in seqs if isinstance(s, int)), default=0)
