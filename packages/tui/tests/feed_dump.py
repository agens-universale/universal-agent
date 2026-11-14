"""Фикстуры истории и нормализованный дамп ленты — для тестов паритета.

Фикстура — тред, записанный прогоном стенда «длинная сессия» (скриптовый
бэкенд, настоящий формат записей), обезличенный: конверты develop в порядке
ленты — сообщения частей треда и записи сбоку (итог хода, смена модели) за
сообщением привязки. Части треда со сжатиями — файлы каталога фикстуры;
трасса субагентов — файл ``<имя>.sidecar.jsonl``.

Оба пути получают одни и те же записи через настоящее файловое хранилище:
открытие сессии — тред записан целиком, экран его открывает; живой путь —
экран открыт на пустом треде, записи дописываются пачками и дочитываются
опросом хвоста (шаг сжатия — тоже им).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from gc_core.execution.progress import (
    TOOL_END_OUTPUT_MAX,
    TOOL_START_COMMAND_MAX,
    QueueSubagentProgressSink,
    SubagentProgressEvent,
    parse_tool_result_status,
    result_facts,
    run_command_text,
    tool_args_summary,
)
from gc_core.persistence.model_choice_record import parse_model_choice
from gc_core.persistence.turn_outcome import parse_turn_outcome
from gc_core.thread_store import append_subagent_record
from gc_core.threads.facade import run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.paths import module_dir
from gc_testkit.thread_writes import (
    acompact,
    model_choice_record,
    turn_outcome_record,
)
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.trace_ephemera import EPHEMERAL_CLASS

if TYPE_CHECKING:
    from gc_tui.screens.main import TraceScreen
    from textual.widget import Widget

#: Тред фикстуры в хранилище теста.
HISTORY_THREAD = "historyfixtureaaaaaaaaaaaaaaaaaa"
#: Классы виджета, которые входят в дамп: вложенность в агента, скрытость,
#: состояния строк итога и рамки плана, строки «!», признак эфемерного.
FEED_DUMP_CLASSES = frozenset(
    {
        EPHEMERAL_CLASS,
        "-in-agent",
        "hidden",
        "-latest",
        "-held",
        "-expanded",
        "plan-accepted",
        "line-bang",
        "line-bang-fail",
    }
)
_SIDE_TYPES = frozenset({"turn_outcome", "model_choice"})

DumpRow = tuple[str, str, frozenset[str]]


@dataclass(frozen=True)
class HistoryFixture:
    """``segments`` — части треда как в хранилище (у части после сжатия
    первой идёт сводка); ``records`` — они же подряд; ``sidecar`` — трасса
    субагентов."""

    records: tuple[dict, ...]
    segments: tuple[tuple[dict, ...], ...]
    sidecar: tuple[dict, ...]


def history_fixtures_dir() -> Path:
    return module_dir("tui").parents[1] / "tests" / "fixtures" / "history"


def _read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_history_fixture(name: str) -> HistoryFixture:
    base = history_fixtures_dir()
    parts_dir = base / name
    if parts_dir.is_dir():
        files = sorted(
            parts_dir.glob("part_*.jsonl"),
            key=lambda p: int(p.stem.split("_")[1]),
        )
        segments = tuple(tuple(_read_jsonl(p)) for p in files)
    else:
        segments = (tuple(_read_jsonl(base / f"{name}.jsonl")),)
    sidecar_path = base / f"{name}.sidecar.jsonl"
    sidecar = _read_jsonl(sidecar_path) if sidecar_path.exists() else []
    records = tuple(rec for seg in segments for rec in seg)
    return HistoryFixture(records, segments, tuple(sidecar))


# ---- дамп ленты -----------------------------------------------------------


def _plain(widget: Widget) -> str:
    rendered = widget.render()
    plain = getattr(rendered, "plain", None)
    if isinstance(plain, str):
        return plain
    return rendered if isinstance(rendered, str) else ""


def _shown(widget: Widget):
    """Виджет и его показанные потомки в порядке DOM (скрытый
    `display: none` потомок — без своего поддерева)."""
    yield widget
    for child in widget.children:
        if child.display:
            yield from _shown(child)


def _flat_text(widget: Widget) -> str:
    """Плоский текст отрисовки виджета и его показанных потомков, без
    хвостовых пробелов строк."""
    texts = (_plain(w) for w in _shown(widget))
    lines = "\n".join(t for t in texts if t).splitlines()
    return "\n".join(line.rstrip() for line in lines if line.strip())


def feed_dump(feed: Widget) -> list[DumpRow]:
    """Прямые дети ленты (кроме блока бренда): класс виджета, плоский
    текст, классы из `FEED_DUMP_CLASSES`."""
    return [
        (
            type(child).__name__,
            _flat_text(child),
            frozenset(child.classes) & FEED_DUMP_CLASSES,
        )
        for child in feed.children
        if child.id != "brand-block"
    ]


def without_ephemeral(rows: list[DumpRow]) -> list[DumpRow]:
    """Дамп без эфемерного (виджеты с классом `EPHEMERAL_CLASS`: мысли,
    «!», вывод команд, строки без события). Пометки трейса под строкой
    владельца живут внутри неё — в живой подаче фикстур их нет."""
    return [row for row in rows if EPHEMERAL_CLASS not in row[2]]


# ---- запись треда фикстуры в хранилище -----------------------------------


class _ThreadWriter:
    """Записи фикстуры — в файловый тред так, как их пишет ядро: сообщения
    ходом, сводка сжатия — заменой истории с архивом, итог хода и смена
    модели — записями сбоку, трасса субагентов — перед выводом вызова
    своего субагента."""

    def __init__(
        self,
        threads_dir: str,
        records: Sequence[dict],
        sidecar: Sequence[dict],
        on_model_choice: Callable[[dict], None] | None = None,
        on_subagent: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._store = FileThreadStore(threads_dir)
        self._key = ThreadKey(system="cli", thread=HISTORY_THREAD)
        self._sidecar = list(sidecar)
        self._seen_ids: set[str] = set()
        self._on_model_choice = on_model_choice
        self._on_subagent = on_subagent
        # Сводка не первой записью треда — сжатие (первая — начало треда).
        self._summaries = [
            rec
            for rec in records[1:]
            if FileTaskStore.is_compaction_summary(rec)
        ]

    def is_compaction(self, rec: dict) -> bool:
        return any(rec is summary for summary in self._summaries)

    def write(self, records: Sequence[dict]) -> None:
        """Записи пачки по порядку; подряд идущие сообщения — одной
        записью хода."""
        messages: list[dict] = []
        for rec in records:
            if rec.get("type") in _SIDE_TYPES:
                self._flush(messages)
                self._side(rec)
                continue
            self._sidecar_before(rec["payload"], messages)
            messages.append(rec["payload"])
        self._flush(messages)

    def compact(self, records: Sequence[dict]) -> int:
        """Сжатие: новая история — сводка и следующие за ней
        preserved-копии (id уже были). Возвращает, сколько записей взято."""
        taken = 1
        while taken < len(records) and self._is_preserved(records[taken]):
            taken += 1
        payloads = [rec["payload"] for rec in records[:taken]]
        run_sync(
            acompact(self._store, self._key, payloads, owner="history-fix")
        )
        self._remember(payloads)
        return taken

    def _is_preserved(self, rec: dict) -> bool:
        payload = rec.get("payload")
        return isinstance(payload, dict) and payload.get("id") in (
            self._seen_ids
        )

    def _remember(self, payloads: list[dict]) -> None:
        self._seen_ids.update(
            p["id"] for p in payloads if isinstance(p.get("id"), str)
        )

    def _flush(self, messages: list[dict]) -> None:
        if not messages:
            return
        FileTaskStore(self._store.threads_dir).append_messages(
            HISTORY_THREAD, list(messages)
        )
        self._remember(messages)
        messages.clear()

    def _sidecar_before(self, payload: dict, messages: list[dict]) -> None:
        if payload.get("type") != "function_call_output":
            return
        call_id = payload.get("call_id")
        mine = [r for r in self._sidecar if r["parent_call_id"] == call_id]
        if not mine:
            return
        self._flush(messages)
        for rec in mine:
            self._sidecar.remove(rec)
            self._subagent(rec)
        if self._on_subagent is not None:
            self._on_subagent(mine)

    def _subagent(self, rec: dict) -> None:
        run_sync(
            append_subagent_record(
                self._store,
                self._key,
                parent_call_id=rec["parent_call_id"],
                child_run_id=rec["child_run_id"],
                kind=rec["kind"],
                payload=rec["payload"],
            )
        )

    def _side(self, rec: dict) -> None:
        if rec["type"] == "model_choice" and self._on_model_choice:
            self._on_model_choice(rec)
            return
        threads_dir = self._store.threads_dir
        if rec["type"] == "turn_outcome":
            turn_outcome_record(
                threads_dir,
                "cli",
                HISTORY_THREAD,
                parse_turn_outcome(rec),
                after=rec.get("after"),
            )
        else:
            model_choice_record(
                threads_dir,
                "cli",
                HISTORY_THREAD,
                parse_model_choice(rec),
                after=rec.get("after"),
            )


def write_history(threads_dir: str, fixture: HistoryFixture) -> None:
    """Тред фикстуры целиком — в хранилище (для открытия сессии)."""
    writer = _ThreadWriter(threads_dir, fixture.records, fixture.sidecar)
    _write_all(writer, fixture.records)


def _write_all(writer: _ThreadWriter, records: Sequence[dict]) -> None:
    i = 0
    while i < len(records):
        if writer.is_compaction(records[i]):
            i += writer.compact(records[i:])
            continue
        j = i + 1
        while j < len(records) and not writer.is_compaction(records[j]):
            j += 1
        writer.write(records[i:j])
        i = j


def feed_live(
    screen: TraceScreen,
    records: Sequence[dict],
    *,
    splits: Sequence[int],
    sidecar: Sequence[dict] = (),
) -> None:
    """Записи — в тред открытого экрана пачками с разрезами по индексам
    ``splits`` и по концу каждого хода; после каждой пачки — опрос хвоста
    (живой путь), после конца хода — тик экрана без живого хода. Сжатие
    дочитывается шагом: опрос до и после каждой замены истории (за тик
    сжатие одно). ``sidecar`` — трасса субагентов, пишется перед выводом
    вызова своего субагента, а его прогон вживую — событиями прогресса
    раннера (тиком экрана). Смена модели живым путём — выбор между ходами
    (`/model`): экран ставит черту и сам пишет запись в тред."""
    writer = _ThreadWriter(
        screen.app._threads_dir,
        records,
        list(sidecar),
        on_model_choice=lambda rec: screen._mark_model_choice(
            rec["model"]["choice"]
        ),
        on_subagent=lambda recs: _live_subagent(screen, recs),
    )
    turn_ends = [
        i + 1
        for i, rec in enumerate(records)
        if rec.get("type") == "turn_outcome"
    ]
    cuts = {*turn_ends, *(s for s in splits if 0 < s < len(records))}
    bounds = [0, *sorted(cuts | {len(records)})]
    for start, end in pairwise(bounds):
        _feed_batch(screen, writer, records[start:end])
        if end in turn_ends:
            # Ход кончился: поток хода вышел — тик без живого хода.
            screen._poll_active()


def _live_subagent(screen: TraceScreen, records: list[dict]) -> None:
    """Прогон субагента вживую: строка его вызова уже в ленте (опрос
    хвоста), события прогресса раннера — в очередь приложения и тиком в
    ленту."""
    app = screen.app
    if app._subagent_progress_sink is None:
        app._subagent_progress_sink = QueueSubagentProgressSink()
    screen._poll_trace_tail()
    root, injected = screen._current_cwd(), screen._injected_fields()
    names = {
        rec["payload"]["call_id"]: rec["payload"]["name"]
        for rec in records
        if rec["kind"] == "call"
    }
    for rec in records:
        app._subagent_progress_sink.emit(
            progress_event(rec, names, root=root, injected=injected)
        )
    screen._drain_subagent_progress()


def progress_event(
    rec: dict,
    names: dict[str, str],
    *,
    root: str,
    injected: frozenset[str],
) -> SubagentProgressEvent:
    """Событие прогресса, которое раннер шлёт вместе с записью трассы
    субагента (`processing_responses._aexecute_tool_call`); ``names`` —
    имена вызовов по call_id. Команда и вывод сверх потолков события — None
    (экран читает их из трассы)."""
    payload = rec["payload"]
    base = {
        "parent_call_id": rec["parent_call_id"],
        "child_run_id": rec["child_run_id"],
        "step": 0,
        "max_steps": 0,
        "elapsed_ms": 0,
    }
    kind = rec["kind"]
    if kind == "call":
        name, args = payload["name"], payload.get("arguments") or ""
        return SubagentProgressEvent(
            kind="tool_start",
            tool_name=name,
            tool_call_id=payload["call_id"],
            args_summary=tool_args_summary(
                name, args, root=root, nested=True, injected=injected
            ),
            command=_within(
                run_command_text(name, args), TOOL_START_COMMAND_MAX
            ),
            **base,
        )
    if kind in ("result", "error"):
        output = payload["output"]
        ok, brief = parse_tool_result_status(output)
        status, fields = result_facts(output) if ok is False else (None, None)
        return SubagentProgressEvent(
            kind="tool_end",
            tool_name=names.get(payload["call_id"]),
            tool_call_id=payload["call_id"],
            ok=ok if ok is not None else True,
            error_brief=brief,
            result_status=status,
            result_error=fields,
            output=_within(output, TOOL_END_OUTPUT_MAX),
            **base,
        )
    if kind == "finished":
        return SubagentProgressEvent(kind="finished", **base)
    raise AssertionError(f"событие прогресса для {kind!r} не описано")


def _within(text: str | None, limit: int) -> str | None:
    return text if text is not None and len(text) <= limit else None


def _feed_batch(
    screen: TraceScreen, writer: _ThreadWriter, batch: Sequence[dict]
) -> None:
    i = 0
    while i < len(batch):
        if writer.is_compaction(batch[i]):
            screen._poll_trace_tail()
            i += writer.compact(batch[i:])
            screen._poll_trace_tail()
            continue
        j = i + 1
        while j < len(batch) and not writer.is_compaction(batch[j]):
            j += 1
        writer.write(batch[i:j])
        i = j
    screen._poll_trace_tail()
