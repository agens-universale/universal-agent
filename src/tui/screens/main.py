"""TraceScreen — главный экран: трейс событий, инпут с палитрой команд и
пикерами (skill/arg/file), ask_human-баннер, pending-реплики и polling треда."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from tui.widgets.ask_human_banner import AskHumanBanner
    from tui.widgets.trace_row import TraceRow

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static, TextArea

from intent_log.record import ACTION_USER_ASK
from intent_log.serialize import deserialize_list
from tui.commands import build_default_registry, closest_command
from tui.models import TaskCursor
from tui.palette import DEEP_DIM as _ERR_HINT
from tui.palette import ERR as _ERR_RED
from tui.services.export import export_thread
from tui.services.git_info import current_branch
from tui.services.stores.file_store import FileTaskStore
from tui.services.trace_mapper import map_message
from tui.widgets.command_input import CommandTextArea
from tui.widgets.command_palette import CommandPalette
from tui.widgets.tool_call_row import ToolCallRow
from tui import branding


# Один набор команд в футере; /stop добавляется только при активном run.
# (/skills, /export доступны через палитру — в футере лишние.)
_FOOTER_LEFT = "/sessions  /help  /config  /mcp"
_FOOTER_RUNNING_LEFT = "/stop  /sessions  /help  /config  /mcp"
_HEADER_EMPTY = branding.HEADER_LEFT_EMPTY
_HEADER_RIGHT = branding.HEADER_RIGHT
_PLACEHOLDER_READY = "опишите задачу или /команда"
_HINT_TEXT = {
    "clear": "Esc ещё раз, чтобы очистить",
    "stop":  "Esc ещё раз, чтобы остановить",
}
_PASTE_HINT_TEXT = "вставьте ещё раз, чтобы развернуть"
# Префикс preview-строк под ToolCallRow (5 пробелов + `| ` чтобы выровняться
# под `  |- ` префиксом самой row'и и визуально продолжить её column).
_PREVIEW_PREFIX = "     | "
# Модель и backend-режим показываются в шапке экрана (_compose_header_segments).


class TraceScreen(Screen):
    """Главный экран: трейс, ввод пользователя, команды и сопровождение run'а."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._registry = build_default_registry()
        self._palette: CommandPalette | None = None
        self._brand: Vertical | None = None
        self._task_events: list = []  # list[TraceEvent]; пополняется в _render_record/_render_segments
        self._last_agent_summary: Optional[str] = None  # A1: дедуп равных user-facing рядов
        self._store: Optional[FileTaskStore] = None
        self._cursor: Optional[TaskCursor] = None
        self._next_no: int = 1
        self._active_task_id: Optional[str] = None
        self._error_shown: bool = False
        self._mcp_status_row = None   # Optional[Static] — live MCP-loading индикатор
        self._mcp_status_done = False
        # Phase 2: tool-row mutation state
        self._active_calls: dict[str, ToolCallRow] = {}
        self._submit_solution_call_ids: set[str] = set()
        # Track submit_solution agent-line widgets, чтобы при повторе свернуть предыдущие.
        # list of (event_no, main_static, note_widget_or_None)
        # spec 2026-05-26 (code-block-highlight): теперь храним СПИСОК виджетов
        # предыдущей submit_solution (multi-widget рендер: prose + code-panels).
        self._submit_solution_widgets: list[tuple[list[Widget], Optional["TraceRow"]]] = []
        # Mapping call_id → preview-Static (показывается при focus row, скрывается при blur).
        # spec 2026-05-27 (tool-output-detail).
        self._tool_call_preview_widgets: dict[str, Static] = {}
        # Mapping event_no → summary-body TraceRow (скрыт; раскрывается при фокусе маркера).
        self._summary_body_widgets: dict[int, "TraceRow"] = {}
        # Phase 5: ask_human banner state
        self._ask_human_banner = None  # type: Optional[AskHumanBanner]
        self._ask_human_call_id: Optional[str] = None
        # confirm banner state (T2)
        self._confirm_banner = None   # type: Optional["ConfirmBanner"]
        self._confirm_active: bool = False
        # Скрытая трейс-строка вопроса; раскрывается при схлопывании баннера.
        self._ask_human_question_row = None  # type: Optional[TraceRow]
        # Раннер, задавший текущий ask_human (владелец вопроса). None, если плашка
        # воспроизведена из истории — тогда ответ идёт новым ходом, а не в stdin.
        self._ask_human_owner_runner = None
        # Spec 2026-05-26: pending user-replies (in-memory dict by client_id, FIFO ordered).
        self._pending_user_widgets: dict[str, "UserReplyContainer"] = {}
        # Кэш wire_content для reconciliation — независим от состояния meta-файла,
        # который executor может перезаписывать в своём потоке.
        self._pending_wire_cache: dict[str, str] = {}
        self._skill_picker = None  # type: Optional["SkillPicker"]
        self._arg_picker = None  # type: Optional["ArgPicker"]
        self._file_picker = None  # FilePicker | None
        self._skills_cache: Optional[list[dict]] = None
        # После Tab-completion в skill picker гасим один Changed-цикл, чтобы
        # on_text_area_changed не переоткрыл только что закрытый picker.
        self._suppress_picker_reopen: bool = False
        # Spec 2026-05-26: ThinkingIndicator state.
        self._thinking = None  # type: Optional["ThinkingIndicator"]
        # Эфемерный StreamBlock — live-вывод текущего chat_completions-шага;
        # снимается, когда приходит каноничная assistant-строка / run завершён.
        self._stream_block = None  # type: Optional["StreamBlock"]
        # Кумулятивный таймер «думаю…» — стартует на каждом user-send,
        # передаётся в виджет, чтобы elapsed не сбрасывался при show/hide циклах.
        self._thinking_started_at: Optional[float] = None
        # Эвристика «скрыть после видимого ответа агента»: после event kind=agent
        # с паузой ≥1.5с прячем индикатор (intent_guard / policy ещё работают,
        # но юзер уже видит ответ). Следующий event снимет .hidden.
        import time as _time
        self._last_event_at: float = _time.monotonic()
        self._last_event_kind: Optional[str] = None
        # Cancel-with-undo: счётчик не-user trace-событий с момента последнего user-send.
        # Если == 0 при Esc-stop → агент ещё ничего не сгенерировал → rollback.
        self._events_since_last_user_send: int = 0
        self._last_user_send_text: str = ""
        # client_id последней pending-реплики — для полного отката в cancel-with-undo.
        self._last_pending_client_id: Optional[str] = None
        # id последнего user-сообщения (пробрасывается в run) — чтобы точечно
        # удалить осиротевшую реплику из треда при cancel-with-undo.
        self._last_user_message_id: Optional[str] = None
        # Заявки на отложенное удаление осиротевших реплик: список
        # (runner, task_id, message_id). Исполняются в poll, когда тред idle.
        self._pending_orphan_cleanups: list[tuple] = []
        # Транзиентная подсказка в footer-left (esc-confirm и paste-hint делят слот).
        self._esc_confirm_kind: Optional[str] = None  # "clear" | "stop" | None
        self._footer_hint_timer = None                # textual.Timer
        self._footer_hint_active: bool = False
        # Input history: per-project ↑/↓ навигация.
        # _history создаётся в on_mount (project_dir = threads_dir.parent).
        self._history = None  # type: Optional["InputHistory"]
        self._history_index: Optional[int] = None
        self._history_draft: Optional[str] = None
        # Защита от self-reset: history-навигация программно меняет input.text,
        # что триггерит on_text_area_changed → reset_history_navigation.
        # Храним последний программно выставленный текст; Changed, у которого
        # текущее значение совпадает с ним, — не user-edit, не сбрасываем state.
        self._history_last_programmatic_text: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            left_text, right_text = self._compose_header_segments()
            with Horizontal(id="header"):
                yield Static(left_text, id="header-left", markup=False)
                yield Static(right_text, id="header-right", markup=False)
            yield VerticalScroll(id="trace")
            with Horizontal(id="input-row"):
                yield Static("> ", id="input-prompt", markup=False)
                yield CommandTextArea(placeholder=_PLACEHOLDER_READY, id="cmdinput")
            with Horizontal(id="footer"):
                yield Static(_FOOTER_LEFT, id="footer-left", markup=False)
                yield Static(
                    self._cwd_chip_text(), id="footer-right", markup=False,
                )

    def _compose_header_segments(self) -> tuple[str, str]:
        """Return (left_text, right_text) for header.

        Empty state:
          left = "GigaCode   модель: <model> · <backend>"
          right = "F1 help · gc-ua"

        Active task:
          left = "Сессия: <8ch>  [status]  модель: <model> · <backend>"
          right = "F1 help · gc-ua"
        """
        cfg = getattr(self.app, "_config", None) or {}
        backend = (
            "chat" if cfg.get("backend_api_mode") == "chat_completions" else "resp"
        )
        model = cfg.get("model", "?")
        model_chip = f"модель: {model} · {backend}"
        if self._active_task_id is None:
            return f"{_HEADER_EMPTY}   {model_chip}", _HEADER_RIGHT
        short_id = self._active_task_id[:8]
        status = self._task_status_badge()
        left = f"Сессия: {short_id}  {status}  {model_chip}"
        return left, _HEADER_RIGHT

    def _task_status_badge(self) -> str:
        # ask_human pending — агент ждёт ответа пользователя (приоритет над running/stopped).
        tui_state = getattr(self.app, "tui_state", None)
        if tui_state is not None and tui_state.ask_human_state is not None:
            return "[asking]"
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return "[running]"
        # Нет активного runner — смотрим meta.status треда.
        if self._store is not None and self._active_task_id is not None:
            meta = self._store.get_task_meta(self._active_task_id)
            if meta.get("status") == "running":
                return "[running]"
        return "[stopped]"

    def _refresh_header(self) -> None:
        left, right = self._compose_header_segments()
        try:
            self.query_one("#header-left", Static).update(left)
            self.query_one("#header-right", Static).update(right)
        except Exception:
            pass

    def _refresh_footer(self) -> None:
        """Switch footer text between empty-state and main-screen based on active task.

        Left-side update пропускается, если активен Esc-confirm hint — иначе
        тиковый poll затирал бы подсказку до её 2-секундного таймера.
        Right-side (cwd-чип) обновляется всегда.
        """
        try:
            left = self.query_one("#footer-left", Static)
            right = self.query_one("#footer-right", Static)
        except Exception:
            return
        suppress_left = self._footer_hint_active
        if self.app.tui_state.ask_human_state is not None:
            if not suppress_left:
                left.update("[Esc] /stop  [Enter] ответить")
            right.update(self._cwd_chip_text())
        elif self.app.tui_state.active_task_id is None:
            if not suppress_left:
                left.update(_FOOTER_LEFT)
            right.update(self._cwd_chip_text())
        else:
            runner = getattr(self.app, "active_runner", None)
            is_running = runner is not None and runner.is_running()
            if not suppress_left:
                left.update(_FOOTER_RUNNING_LEFT if is_running else _FOOTER_LEFT)
            right.update(self._cwd_chip_text())

    def _current_cwd(self) -> str:
        """Рабочая папка сессии — фиксирована = папка запуска процесса."""
        return str(Path.cwd())

    def _cwd_chip_text(self) -> str:
        cwd = self._current_cwd()
        # Длинный путь усекаем слева — хвост (имя проекта) важнее начала.
        if len(cwd) > 48:
            cwd = "…" + cwd[-47:]
        return cwd

    def _show_footer_hint(self, text: str, seconds: float = 2.0) -> None:
        """Короткая подсказка в footer-left (esc-confirm — 2с, paste-hint — 5с)."""
        self._footer_hint_active = True
        self.query_one("#footer-left").update(text)
        if self._footer_hint_timer is not None:
            self._footer_hint_timer.stop()
        self._footer_hint_timer = self.set_timer(seconds, self._clear_footer_hint)

    def _show_esc_hint(self, kind: str) -> None:
        """Esc-confirm hint: запоминает kind (для 2-го Esc) + показывает подсказку."""
        self._esc_confirm_kind = kind
        self._show_footer_hint(_HINT_TEXT[kind])

    def _clear_footer_hint(self) -> None:
        """Сбросить hint-state (esc + paste) + восстановить footer-left (команды)."""
        self._esc_confirm_kind = None
        self._footer_hint_active = False
        if self._footer_hint_timer is not None:
            self._footer_hint_timer.stop()
            self._footer_hint_timer = None
        self._refresh_footer()

    def on_mount(self) -> None:
        self.query_one("#cmdinput", CommandTextArea).focus()
        self._show_brand()
        # Per-project история input-реплик: project_dir известен со старта
        # (threads_dir.parent), attach не нужен — ↑/↓ работают уже в empty-state.
        from tui.services.input_history import InputHistory
        threads_dir = Path(self.app._threads_dir or "")
        project_dir = threads_dir.parent if threads_dir.parts else Path.cwd()
        self._history = InputHistory(project_dir)
        self._history.load()
        self.set_interval(0.25, self._poll_active)   # 250 ms — single source of truth
        # Прогрев executor'а (incl. MCP-дискавери) в фоне на старте — чтобы MCP
        # грузились при запуске gc-ua, а не на первом сообщении. Индикатор —
        # из _poll_active. Build-once: первый start() переиспользует собранный.
        # warm — опциональная оптимизация: прод-runner (AgentRunner) её имеет,
        # тест-дабли раннера могут не иметь — тогда просто пропускаем.
        runner = self.app.get_runner()
        warm = getattr(runner, "warm", None)
        if callable(warm):
            warm()

    def _reset_render_state(self) -> None:
        """Clear all per-task rendering state. Called on task switch and stream_reset."""
        self._task_events.clear()
        self._last_agent_summary = None
        self._next_no = 1
        self._active_calls.clear()
        self._submit_solution_call_ids.clear()
        self._submit_solution_widgets.clear()
        self._tool_call_preview_widgets.clear()
        self._summary_body_widgets.clear()
        self._pending_user_widgets.clear()  # spec 2026-05-26
        self._pending_wire_cache.clear()
        # clear ask_human state (banner lives in #root, not #trace)
        if self._ask_human_banner is not None:
            try:
                self._ask_human_banner.remove()
            except Exception:
                pass
            self._ask_human_banner = None
            self._ask_human_call_id = None
            if self.app.tui_state.ask_human_state is not None:
                self.app.tui_state.ask_human_state = None
        self._ask_human_question_row = None  # виджет ушёл вместе с trace-очисткой
        self._ask_human_owner_runner = None
        if self._thinking is not None:
            try:
                self._thinking.remove()
            except Exception:
                pass
            self._thinking = None
        self._clear_stream_block()
        self._thinking_started_at = None
        # Сброс эвристики «последнее событие — agent»: на смене задачи / stream_reset
        # история событий обнуляется, индикатор должен начать с чистого состояния.
        import time as _time
        self._last_event_at = _time.monotonic()
        self._last_event_kind = None

    def attach_active_task(self, task_id: str) -> None:
        """Attach a thread for polling. Initial state loads via read_all_events."""
        self._active_task_id = task_id
        self.app.tui_state.active_task_id = task_id
        self._reset_render_state()
        self._store = FileTaskStore(
            threads_dir=self.app._threads_dir or "",
            system=self.app._system,
        )
        # read_all_events — атомарный снимок live-файла + cursor (как раньше).
        # Была ли компакция, определяем по голове live-снимка (после turn_end
        # первая запись = сводка) — без доп. I/O. Тред без компакции рендерим
        # старым путём из ЭТОГО снимка (consistent с cursor, без лишних чтений —
        # иначе перетасовка тайминга ломает гонку optimistic-reply). read_segments
        # (glob архивов + повторное чтение) — только для сжатых тредов.
        records, cursor = self._store.read_all_events(task_id)
        compacted = bool(records) and self._store.is_compaction_summary(records[0])
        self._cursor = cursor
        # Очистить #trace от предыдущей сессии — иначе при переходе
        # session A → session B (через /sessions) трейсы склеиваются
        # (виджеты A остаются, B дорисовывается поверх).
        self._clear_trace()
        if compacted:
            self._render_segments(self._store.read_segments(task_id))
        else:
            self._render_records(records)
        self._refresh_header()
        self._refresh_footer()
        self._mount_failed_from_meta(task_id)

        # Свести ask_human-баннер с реальностью: replay/компакция могли оставить
        # баннер для уже отвеченного вопроса (его output суммаризован) — см. helper.
        # Висячий вопрос (реально последний, без живого агента) → remnant с меткой.
        self._reconcile_ask_human_after_render(allow_remnant=True)

    def _mount_failed_from_meta(self, task_id: str) -> None:
        """После рестарта: смонтировать виджеты failed-реплик из
        meta.tui.pending_user_messages — но только truly-orphaned (не нашли
        соответствующего user-сообщения ни в meta.user_intent_log,
        ни в self._task_events).

        Source of truth — meta.user_intent_log (persisted across
        summarizations; user_ask-записи); legacy meta.user_messages_log и
        _task_events — safety net на случай пустого/старого log."""
        if self._store is None:
            return
        from tui.services.relative_time import parse_iso_utc
        meta = self._store.get_task_meta(task_id)
        records = (meta.get("tui") or {}).get("pending_user_messages", [])
        if not records:
            return
        # Primary source: user_intent_log в meta (intent-модель master) — user_ask записи.
        delivered_wires = {
            rec.content.strip()
            for rec in deserialize_list(meta.get("user_intent_log"))
            if rec.action == ACTION_USER_ASK
            and isinstance(rec.content, str)
            and rec.content.strip()
        }
        # Legacy fallback: старый формат meta.user_messages_log (плоские строки).
        delivered_wires.update(
            (s or "").strip()
            for s in (meta.get("user_messages_log") or [])
            if isinstance(s, str)
        )
        # Safety net: live trace (на случай если оба log пусты —
        # старый формат meta, повреждение и т.п.).
        delivered_wires.update(
            ev.summary for ev in self._task_events if ev.kind == "user"
        )
        # Устаревание: самый поздний ts доставленного user_ask. Pending/failed
        # реплику старше него пользователь уже оставил позади (есть доставленные
        # позже) → она устарела, показывать/хранить её не нужно.
        latest_delivered_ts = None
        for rec in deserialize_list(meta.get("user_intent_log")):
            if rec.action != ACTION_USER_ASK:
                continue
            t = parse_iso_utc(getattr(rec, "ts", None))
            if t is not None and (latest_delivered_ts is None or t > latest_delivered_ts):
                latest_delivered_ts = t
        truly_orphaned: list[dict] = []
        kept_records: list[dict] = []
        for r in records:
            wire = r.get("wire_content") or r.get("content") or ""
            if wire and wire in delivered_wires:
                continue
            # Устарело: есть доставленный user_ask с ts позже этой реплики →
            # пользователь ушёл дальше; не монтируем и вычищаем из meta.
            if latest_delivered_ts is not None:
                r_ts = parse_iso_utc(r.get("ts_utc_iso"))
                if r_ts is not None and r_ts < latest_delivered_ts:
                    continue
            truly_orphaned.append(r)
            kept_records.append(
                {**r, "status": "failed"} if r.get("status") == "pending" else r
            )
        for r in truly_orphaned:
            content = r.get("content") or ""
            if content:
                self._mount_user_reply(content, state="failed")
        if len(kept_records) != len(records) or any(
            r.get("status") == "pending" for r in records
        ):
            self._store.update_tui_meta(
                task_id, {"pending_user_messages": kept_records}
            )

    def _render_segments(self, segments: list[dict]) -> None:
        """Рендер ВСЕХ сегментов сырьём — полный трейс, без схлопывания старых
        сегментов в маркер. Маркер сжатия сегмента i = его compacted_by (ставится
        между сегментами, чтобы было видно, где прошла компакция).

        Дедуп по id: последнее user-сообщение переносится в новый компакт-файл
        (build_preserved_batch), поэтому лежит и в архивном сегменте, и в live —
        в соседних сегментах это дало бы дубль реплики вокруг маркера."""
        from tui.models import TraceEvent
        seen_ids: set[str] = set()
        for i, seg in enumerate(segments):
            for rec in seg["messages"]:
                payload = rec.get("payload")
                mid = payload.get("id") if isinstance(payload, dict) else None
                if mid is not None:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                self._render_record(rec)
            comp = seg["compacted_by"]
            if comp is not None:
                ev = TraceEvent(
                    no=self._next_no, event_ts_utc_iso=comp.get("ts", ""),
                    kind="summary", title="summary",
                    summary=f"контекст сжат #{i + 1} · {comp['messages_in_segment']}→1",
                    summary_content=comp["synthesis"], full_payload={},
                )
                self._next_no += 1
                self._task_events.append(ev)
                self._render_event(ev)

    def _render_records(self, records: list[dict], *, live: bool = False) -> None:
        for rec in records:
            self._render_record(rec, live=live)

    def _render_record(self, rec: dict, *, live: bool = False) -> None:
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        ev = map_message(payload, no=self._next_no, ts=rec.get("ts", ""))
        if ev is None:
            return
        self._next_no += 1
        self._task_events.append(ev)
        self._render_event(ev, live=live)
        # Spec 2026-05-26 + 2026-05-28: запоминаем kind + время последнего
        # события. Для submit_solution финального ответа — sticky sentinel
        # "_finalized" вместо "agent": блокирует повторный mount в
        # _poll_active, пока не придёт новое значимое событие (gate-reject
        # [policy], новый submit_solution и т.п.).
        import time as _time
        self._last_event_at = _time.monotonic()
        if ev.kind == "agent" and ev.tool_name == "submit_solution":
            self._last_event_kind = "_finalized"
        else:
            self._last_event_kind = ev.kind

    def _show_thinking_indicator(self, *, state: str = "thinking") -> None:
        """Показать индикатор: смонтировать (если ещё нет) или снять `.hidden`.

        Виджет персистентный на всю длительность run'а — анимация не сбрасывается
        при переключениях show/hide во время потока событий. Удаляется только
        в `_destroy_thinking_indicator` (по завершении run'а / reset).

        `state` — "thinking" (активная работа), "validating" (пост-валидация
        после видимого финального ответа) или "compressing" (суммаризация
        истории). Переключается без размонтирования для непрерывного визуала.
        """
        if self._thinking is not None:
            # Разворот из `.hidden` (height 0 → видимый) растит контент. Если юзер
            # у низа, доводим вид до индикатора, иначе «✓ завершаю…» остаётся под
            # сгибом после финального ответа (ответы скроллятся, плашка — нет).
            # Только при реальном unhide: на обычных тиках «думаю» (уже видим)
            # высота не меняется — вид не трогаем.
            was_hidden = self._thinking.has_class("hidden")
            following = was_hidden and self._trace_is_at_bottom()
            self._thinking.remove_class("hidden")
            self._thinking.set_state(state)
            if following:
                self.query_one("#trace", VerticalScroll).scroll_end(animate=False)
            return
        _cfg = getattr(self.app, "_config", None) or {}
        from tui.widgets.thinking_indicator import ThinkingIndicator
        indicator = ThinkingIndicator(
            started_at=self._thinking_started_at, state=state,
            sink=getattr(self.app, "_stream_sink", None),
            reasoning_tag=not bool(_cfg.get("tui_show_reasoning_line", True)),
        )
        self._thinking = indicator
        trace = self.query_one("#trace", VerticalScroll)
        following = self._trace_is_at_bottom()
        trace.mount(indicator)
        if following:
            trace.scroll_end(animate=False)

    def _hide_thinking_indicator(self) -> None:
        """Скрыть индикатор (CSS `.hidden`), не удаляя — анимация продолжает идти."""
        if self._thinking is None:
            return
        self._thinking.add_class("hidden")

    def _destroy_thinking_indicator(self) -> None:
        """Полностью убрать индикатор. Вызывается когда run завершён."""
        if self._thinking is None:
            return
        self._thinking.remove()
        self._thinking = None

    def _clear_stream_block(self, *, reset_sink: bool = True) -> None:
        """Снять эфемерный StreamBlock и (по умолчанию) сбросить sink.

        reset_sink=False: виджет убирается, но sink не трогается — счётчик и
        reasoning-состояние индикатора продолжают жить (нужно для
        tui.show_*=False: действие в drain без сброса буфера)."""
        if self._stream_block is not None:
            self._stream_block.remove()
            self._stream_block = None
        if reset_sink:
            sink = getattr(self.app, "_stream_sink", None)
            if sink is not None:
                sink.reset()

    def _trace_is_at_bottom(self) -> bool:
        """True, если трейс прокручен в самый низ (с допуском). Используется для
        «прилипания к низу»: авто-скролл на новые события — только когда юзер уже
        внизу; если он отскроллил вверх читать — не дёргаем вид."""
        try:
            trace = self.query_one("#trace", VerticalScroll)
        except Exception:
            return True
        return trace.scroll_offset.y >= trace.max_scroll_y - 2

    def _trace_mount(self, widget, *, scroll: bool = True,
                     force_scroll: bool = False) -> None:
        """Смонтировать widget в #trace перед ThinkingIndicator (если есть),
        чтобы индикатор всегда оставался последним.

        scroll=True: прокрутить вниз, но только если юзер уже внизу
        (`_trace_is_at_bottom`) — иначе не дёргаем его прокрутку. force_scroll=True
        прокручивает безусловно (отправка своего сообщения = re-engage)."""
        trace = self.query_one("#trace", VerticalScroll)
        following = force_scroll or self._trace_is_at_bottom()
        if self._thinking is not None and self._thinking.is_mounted:
            trace.mount(widget, before=self._thinking)
        else:
            trace.mount(widget)
        if scroll and following:
            trace.scroll_end(animate=False)

    def _on_user_text_submitted(self, text: str) -> None:
        """Обработка отправки user query: обнулить счётчик событий с последнего
        user-send и запомнить текст для cancel-with-undo (Tasks 9-10).
        Дополнительно: append текста в input-history и выход из history-навигации."""
        from tui.services.terminal_title import format_terminal_title, set_terminal_title
        set_terminal_title(format_terminal_title(text))
        self._events_since_last_user_send = 0
        self._last_user_send_text = text
        if self._history is not None:
            self._history.append(text)
        self._reset_history_navigation()

    def _reset_thinking_timer(self) -> None:
        """Сбросить таймер «думаю…»: новая точка отсчёта = монотоника сейчас,
        текущий виджет удалён (следующий show создаст свежий с актуальным started_at).
        Также сбрасываем эвристику «agent был последним» — после send юзер ждёт
        ответ заново, индикатор должен показаться без задержки.
        Вызывается на каждом user-send (3 пути отправки)."""
        import time as _time
        now = _time.monotonic()
        self._thinking_started_at = now
        self._last_event_at = now
        self._last_event_kind = None
        self._destroy_thinking_indicator()

    def _render_event(self, ev, *, live: bool = False) -> None:
        if ev.hidden:
            return
        if ev.kind != "user":
            self._events_since_last_user_send += 1

        # Any committed canonical row supersedes the ephemeral reasoning/answer
        # preview for the current step — clear it so it can't drift above new rows.
        # (It re-mounts at the bottom on the next poll tick if streaming continues.)
        self._clear_stream_block()

        # ask_human-output banner cleanup — Фикс B-2.
        # Mapper эмитит для function_call_output name=ask_human либо kind='user'
        # (непустой response), либо kind='system' (пустой/cancel/broken). В обоих
        # случаях снимаем banner ДО передачи в kind-handler, чтобы kind='user'
        # затем нормально реконсилил pending через _pending_wire_match,
        # а kind='system' прошёл в meta-ветку и отрисовал "[ask_human] (без ответа)".
        if (
            self._ask_human_call_id is not None
            and ev.tool_name == "ask_human"
            and ev.kind in ("user", "system")
        ):
            ah_match = (not ev.call_id) or ev.call_id == self._ask_human_call_id
            if ah_match:
                self._unmount_ask_human_banner()
                # НЕ return — событие должно дойти до своего обработчика.

        if ev.kind == "user":
            # New conversation turn — clear retry-tracking from previous turn(s).
            # Previous submit_solutions are now considered completed answers,
            # not retry candidates for collapse.
            self._submit_solution_widgets.clear()

            # Reconcile с pending: матч по wire_content в FIFO-порядке.
            for cid, widget in list(self._pending_user_widgets.items()):
                # Wire_content хранится в meta; сравним с ev.summary, который и есть
                # содержимое user-сообщения, записанное executor'ом в .jsonl.
                if self._pending_wire_match(cid, ev.summary):
                    widget.set_state("normal")
                    self._pending_user_widgets.pop(cid, None)
                    self._remove_pending_record(cid)
                    return
            # Без pending-матча — обычный mount.
            self._mount_user_reply(ev.summary, event_no=ev.no)
            return

        if ev.kind == "agent":
            # A1: дубль контента (проза → тот же финализатор / повтор) — молча
            # опускаем второй ряд (контент уже показан первым). Exact-match (strip),
            # без маркера: завершение хода и так видно по готовому инпуту.
            norm = (ev.summary or "").strip()
            if norm and norm == self._last_agent_summary:
                return
            self._last_agent_summary = norm
            if ev.tool_name == "intermediate_reply":
                from tui.services.trace_render import build_intermediate_widgets
                # «Прилипание к низу»: фиксируем ДО монтирования (как у прозы).
                following = self._trace_is_at_bottom()
                for w in build_intermediate_widgets(ev.summary, event_no=ev.no):
                    self._trace_mount(w, scroll=False)
                if following:
                    self.query_one("#trace", VerticalScroll).scroll_end(animate=False)
                return
            from tui.services.trace_render import build_agent_widgets

            # «Прилипание к низу»: фиксируем ДО монтирования (mount растит max_scroll_y).
            following = self._trace_is_at_bottom()
            mounted_widgets = build_agent_widgets(ev.summary, event_no=ev.no)
            for w in mounted_widgets:
                self._trace_mount(w, scroll=False)

            # Submit_solution comment как note — после всех виджетов.
            note_widget: Optional["TraceRow"] = None
            if isinstance(ev.full_payload, dict) and ev.full_payload.get("_submit_comment"):
                from tui.widgets.trace_row import TraceRow
                from tui.services.trace_render import note_text
                note_widget = TraceRow(
                    prefix="* ",
                    content=note_text(ev.full_payload["_submit_comment"]),
                    kind_class="line-note",
                    event_no=ev.no,
                    focusable=False,
                )
                self._trace_mount(note_widget, scroll=False)
            if following:
                self.query_one("#trace", VerticalScroll).scroll_end(animate=False)

            if ev.tool_name == "submit_solution" and ev.call_id:
                self._submit_solution_call_ids.add(ev.call_id)
                if self._submit_solution_widgets:
                    # Отклонённые intent_guard'ом попытки убираем молча (как
                    # regenerate в чатах): внутренняя кухня качества юзеру не
                    # показывается, обе попытки сохранены в jsonl треда.
                    for prev_widgets, prev_note in self._submit_solution_widgets:
                        for w in prev_widgets:
                            w.remove()
                        if prev_note is not None:
                            prev_note.remove()
                # Трекаем ВСЕ смонтированные виджеты этой submit_solution
                # (prose-блоки + code-block-panels) — нужно для коллапса при retry.
                self._submit_solution_widgets.append((list(mounted_widgets), note_widget))
                # Финальный ответ виден — переключаем индикатор в режим
                # «✓ завершаю…» немедленно (без 250мс лага на следующий тик
                # _poll_active). Sticky-маркер sentinel выставляется в
                # _render_records, чтобы пережить _last_event_kind = ev.kind ниже.
                if self._thinking is not None:
                    self._thinking.set_state("validating")
            return

        if ev.kind == "ask_human":
            self._on_ask_human_event(ev, live=live)
            return

        if ev.kind == "call":
            from tui.services.trace_render import args_portion
            row = ToolCallRow(
                call_id=ev.call_id or f"_anon_{ev.no}",
                tool_name=ev.tool_name or "<tool>",
                args_summary=args_portion(ev.summary),
                event_no=ev.no,
            )
            self._active_calls[row.call_id] = row
            self._trace_mount(row)
            return

        if ev.kind in ("result", "error"):
            # Suppress output of submit_solution (no `|- ok` after final answer)
            if ev.call_id and ev.call_id in self._submit_solution_call_ids:
                self._submit_solution_call_ids.discard(ev.call_id)
                return
            # Mutate existing row in place
            row = self._active_calls.get(ev.call_id) if ev.call_id else None
            if row is not None:
                row.set_result(
                    summary=ev.summary,
                    error=(ev.kind == "error"),
                )
                # Если есть полезный preview-контент — смонтировать preview-widget после row (.hidden).
                self._maybe_mount_tool_preview(row, ev)
                self._active_calls.pop(ev.call_id, None)
                return
            # Fallback (call_id unknown or no matching row) — append separate line
            prefix = "  |- " if ev.kind == "result" else "  |- [!] "
            cls = "line-ok" if ev.kind == "result" else "line-error"
            self._mount_trace_row(
                prefix=prefix, content=ev.summary, kind_class=cls,
                event_no=ev.no,
            )
            return

        if ev.kind == "summary":
            from tui.widgets.trace_row import SummaryMarker
            marker_label = ev.summary or "контекст сжат"
            marker = SummaryMarker(
                prefix="▸ ", content=f"──── {marker_label} ────",
                kind_class="summary-marker", event_no=ev.no, focusable=True,
            )
            self._trace_mount(marker)
            body_text = (ev.summary_content or "").strip()
            if body_text:
                # Свёрнут по умолчанию (.hidden); toggle по клику маркера ИЛИ тела.
                body = SummaryMarker(
                    prefix="   ", content=f"Контекст сохранён:\n{body_text}",
                    kind_class="summary-body hidden", event_no=ev.no, focusable=False,
                )
                self._trace_mount(body)
                self._summary_body_widgets[ev.no] = body
            return

        if ev.kind == "error_msg":
            self._mount_trace_row(
                prefix="[!] ", content=ev.summary,
                kind_class="line-error", event_no=ev.no,
            )
            return

        # default: meta. summary вида "[tag] описание" — отрезаем prefix для TraceRow.
        from tui.services.trace_render import TAG_RE
        m = TAG_RE.match(ev.summary)
        if m:
            self._mount_trace_row(
                prefix=f"[{m.group('tag')}] ",
                content=m.group("rest"),
                kind_class="line-meta",
                event_no=ev.no,
            )
        else:
            self._mount_trace_row(
                prefix="", content=ev.summary, kind_class="line-meta", event_no=ev.no,
            )

    def _maybe_mount_tool_preview(self, row, ev) -> None:
        """При наличии полезного preview-контента — смонтировать Static после row.
        Изначально с .hidden; on_tool_call_focus_changed toggle'ит видимость."""
        if ev.kind == "error":
            return  # error виден inline в row, preview не нужен
        from tui.services.trace_render import raw_output
        raw = raw_output(ev)
        if not raw:
            return
        from tui.services.tool_preview import extract_preview_lines
        lines, remaining = extract_preview_lines(raw)
        if not lines:
            return
        # Формат preview: каждая строка с префиксом `_PREVIEW_PREFIX`, маркер `... +N more` в конце.
        body = "\n".join(f"{_PREVIEW_PREFIX}{ln}" for ln in lines)
        if remaining > 0:
            body += f"\n{_PREVIEW_PREFIX}... +{remaining} more"
        preview = Static(body, classes="tool-result-preview hidden", markup=False)
        trace = self.query_one("#trace", VerticalScroll)
        trace.mount(preview, after=row)
        self._tool_call_preview_widgets[ev.call_id] = preview

    def _on_ask_human_event(self, ev, *, live: bool = False) -> None:
        from tui.widgets.ask_human_banner import AskHumanBanner
        if self._ask_human_banner is not None:
            if self._ask_human_call_id == ev.call_id:
                return  # тот же вопрос уже смонтирован (повторный poll)
            # Новый вопрос замещает предыдущий: старый вопрос стал историей —
            # снимаем его баннер (его строка раскрывается), баннер переезжает
            # на новый. Иначе цепочка вопросов без ответа (переформулировки)
            # застряла бы на первом баннере и проглатывала все последующие.
            self._unmount_ask_human_banner()
        # Вопрос всегда оседает в трейсе dim-строкой — история диалога полная
        # и одинаковая в live и replay. Пока баннер виден, строка скрыта (нет
        # дубля на экране); монтируем её сразу, чтобы зафиксировать позицию
        # ВЫШЕ будущего ответа юзера — раскроется при схлопывании баннера.
        self._ask_human_question_row = self._mount_trace_row(
            prefix="  ? ",
            content=f"ask_human: {ev.summary}",
            kind_class="line-meta ask-human-question hidden",
            event_no=ev.no,
        )
        banner = AskHumanBanner(question=ev.summary)
        self._ask_human_banner = banner
        self._ask_human_call_id = ev.call_id
        # Владелец — только если плашка пришла из live-поллинга активного run'а
        # (он реально висит в input()). При воспроизведении истории — не трогаем.
        if live:
            self._ask_human_owner_runner = self.app.active_runner
        # update tui_state for header [asking] badge
        from tui.state import AskHumanState
        self.app.tui_state.ask_human_state = AskHumanState(
            call_id=ev.call_id or "", question=ev.summary,
        )
        # mount banner before #input-row
        inp_row = self.query_one("#input-row", Horizontal)
        self.query_one("#root", Vertical).mount(banner, before=inp_row)
        # switch input to ask-human mode
        inp = self.query_one("#cmdinput", CommandTextArea)
        inp.placeholder = "ваш ответ агенту…"
        inp.add_class("ask-human-mode")
        self._refresh_header()
        self._refresh_footer()
        if self._trace_is_at_bottom():
            self.query_one("#trace", VerticalScroll).scroll_end(animate=False)

    def _unmount_ask_human_banner(self) -> None:
        self._ask_human_owner_runner = None
        if self._ask_human_banner is None:
            return
        # Плашка схлопывается → раскрываем трейс-строку вопроса (история).
        if self._ask_human_question_row is not None:
            try:
                self._ask_human_question_row.remove_class("hidden")
            except Exception:
                pass
            self._ask_human_question_row = None
        self._ask_human_banner.remove()
        self._ask_human_banner = None
        self._ask_human_call_id = None
        self.app.tui_state.ask_human_state = None
        inp = self.query_one("#cmdinput", CommandTextArea)
        inp.placeholder = _PLACEHOLDER_READY
        inp.remove_class("ask-human-mode")
        self._refresh_header()
        self._refresh_footer()

    # ──────────────────────── confirm banner (T2) ───────────────────────────

    def show_confirm_banner(self, title: str, question: str) -> None:
        """Смонтировать ConfirmBanner и переключить в режим одиночной клавиши.

        Вызывается на UI-потоке (напр. через App.call_from_thread из T3).
        Скрывает #input-row, убирает фокус с TextArea — «y»/«n» доходят до
        Screen.on_key и не поглощаются TextArea."""
        from tui.widgets.confirm_banner import ConfirmBanner
        if self._confirm_active:
            self._hide_confirm_banner()
        banner = ConfirmBanner(banner_title=title, question=question)
        self._confirm_banner = banner
        self._confirm_active = True
        inp_row = self.query_one("#input-row", Horizontal)
        self.query_one("#root", Vertical).mount(banner, before=inp_row)
        inp_row.add_class("hidden")
        # Убираем фокус с TextArea — без этого она потребляет printable-клавиши
        # и «y»/«n» не достигают Screen.on_key.
        self.set_focus(None)
        if self._trace_is_at_bottom():
            self.query_one("#trace", VerticalScroll).scroll_end(animate=False)

    def _hide_confirm_banner(self) -> None:
        """Снять ConfirmBanner и восстановить поле ввода."""
        self._confirm_active = False
        if self._confirm_banner is not None:
            try:
                self._confirm_banner.remove()
            except Exception:
                pass
            self._confirm_banner = None
        try:
            self.query_one("#input-row", Horizontal).remove_class("hidden")
        except Exception:
            pass
        try:
            self.query_one("#cmdinput", CommandTextArea).focus()
        except Exception:
            pass

    def _answer_confirm(self, ok: bool) -> None:
        """Отправить ответ в confirm-канал раннера и скрыть баннер."""
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and hasattr(runner, "submit_confirm_reply"):
            runner.submit_confirm_reply(ok)
        self._hide_confirm_banner()

    # ──────────────────────────────────────────────────────────────────────────

    def _ask_human_target_runner(self):
        """Раннер-владелец текущего ask_human, если он жив и совпадает с активным
        (значит реально висит в input() на этом вопросе) — иначе None (ответ
        пойдёт новым ходом)."""
        owner = self._ask_human_owner_runner
        runner = getattr(self.app, "active_runner", None)
        if owner is not None and owner is runner and runner.is_running():
            return owner
        return None

    def _ask_human_dialog_continued(self) -> bool:
        """True, если после смонтированного ask_human-вопроса в трассе есть
        осмысленные события (ответ / 'контекст сжат' / продолжение) — значит вопрос
        НЕ висячий (его output мог быть суммаризован при компакции), remnant не нужен.
        False — ask_human реально последнее осмысленное событие (висячий вопрос)."""
        cid = self._ask_human_call_id
        ah_idx = None
        for i, ev in enumerate(self._task_events):
            if ev.kind == "ask_human" and (not cid or ev.call_id == cid):
                ah_idx = i
        if ah_idx is None:
            return False
        meaningful = {"user", "agent", "call", "result", "error", "error_msg", "summary"}
        return any(ev.kind in meaningful for ev in self._task_events[ah_idx + 1:])

    def _reconcile_ask_human_after_render(self, *, allow_remnant: bool) -> None:
        """Свести ask_human-баннер с реальностью после replay / re-render.

        Баннер мог остаться смонтирован, хотя вопрос уже отвечен, а его output —
        в заархивированном/суммаризированном сегменте (компакция), поэтому B-2
        cleanup не сработал. Логика:
        - диалог продолжился после вопроса → тихо снять баннер (без remnant);
        - вопрос реально последний и живого агента нет → висячий: снять баннер и
          (в attach-контексте, allow_remnant) показать человекочитаемую метку;
        - живой агент ждёт ответа → баннер валиден, не трогаем."""
        if self._ask_human_banner is None:
            return
        if self._ask_human_dialog_continued():
            self._unmount_ask_human_banner()
            return
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return  # живой агент действительно ждёт ответа
        self._unmount_ask_human_banner()
        if allow_remnant:
            # Текст вопроса не дублируем: он уже в трейсе dim-строкой
            # «? ask_human: …» (её монтирует _on_ask_human_event).
            self._mount_trace_row(
                prefix="Агент спрашивал (прервано) — ",
                content="продолжите сообщением или Esc для выхода",
                kind_class="line-meta",
            )

    def _mount_user_reply(
        self,
        text: str,
        *,
        event_no: int = 0,
        state: str = "normal",
        client_id: str | None = None,
    ):
        from tui.widgets.user_reply import UserReplyContainer
        widget = UserReplyContainer(text, event_no=event_no, state=state)
        # pending = свежая отправка юзера → форсим скролл вниз (re-engage);
        # normal/failed = воспроизведение из истории → обычное прилипание.
        self._trace_mount(widget, force_scroll=(state == "pending"))
        if state == "pending" and client_id is not None:
            self._record_pending_widget(client_id, widget)
        return widget

    _PENDING_CAP = 20

    def _track_pending(self, display_text: str, wire_content: str) -> str:
        """Записать pending-реплику в meta.tui + положить в `_pending_user_widgets`.

        display_text — то, что показывает виджет (как ввёл юзер).
        wire_content — то, что executor запишет в .jsonl (может отличаться при
        наличии pending skill/cwd prefix). Используется для reconciliation.
        Возвращает client_id.
        """
        tid = self.app.tui_state.active_task_id
        if tid is None or self._store is None:
            # Под фолбэк — генерим client_id, но в meta не пишем (нет ни задачи, ни стора).
            cid = uuid.uuid4().hex
            self._last_pending_client_id = cid
            return cid

        client_id = uuid.uuid4().hex
        record = {
            "client_id": client_id,
            "content": display_text,
            "wire_content": wire_content,
            "ts_utc_iso": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        existing = (self._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        new_list = (existing + [record])[-self._PENDING_CAP:]
        self._store.update_tui_meta(tid, {"pending_user_messages": new_list})
        # In-memory кэш для reconciliation: meta может быть перетёрт executor'ом.
        self._pending_wire_cache[client_id] = wire_content
        self._last_pending_client_id = client_id
        return client_id

    def _record_pending_widget(self, client_id: str, widget) -> None:
        """Связать только что смонтированный pending-виджет с его client_id."""
        self._pending_user_widgets[client_id] = widget

    def _remove_pending_record(self, client_id: str) -> None:
        """Удалить запись из meta.tui.pending_user_messages по client_id."""
        self._pending_wire_cache.pop(client_id, None)
        tid = self.app.tui_state.active_task_id
        if tid is None or self._store is None:
            return
        existing = (self._store.get_task_meta(tid).get("tui") or {}).get(
            "pending_user_messages", []
        )
        new_list = [r for r in existing if r.get("client_id") != client_id]
        if len(new_list) != len(existing):
            self._store.update_tui_meta(tid, {"pending_user_messages": new_list})

    def _mark_pending_failed(self) -> None:
        """При фейле run'а: все текущие pending-виджеты → state=failed,
        записи в meta.tui.pending_user_messages → status=failed.
        Очищает self._pending_user_widgets (виджеты остаются на экране как failed)."""
        if not self._pending_user_widgets:
            return
        tid = self.app.tui_state.active_task_id
        for cid, widget in self._pending_user_widgets.items():
            widget.set_state("failed")
        if tid is not None and self._store is not None:
            records = (self._store.get_task_meta(tid).get("tui") or {}).get(
                "pending_user_messages", []
            )
            failed_cids = set(self._pending_user_widgets.keys())
            new_list = [
                {**r, "status": "failed"} if r.get("client_id") in failed_cids else r
                for r in records
            ]
            self._store.update_tui_meta(tid, {"pending_user_messages": new_list})
        self._pending_user_widgets.clear()

    def _prepare_for_new_run(self) -> None:
        """Подготовить TUI к новому запуску runner.start:
        сбросить error-флаг, очистить last_runner_error.

        _mcp_status_row НЕ обнуляем: строка — часть шапки трейса, переживает
        пересборку (_clear_trace); потерянная ссылка при живом виджете дала бы
        дубль #mcp-status-row при следующем монтировании."""
        self._error_shown = False
        self._mcp_status_done = False
        self.app.last_runner_error = None

    def _update_mcp_indicator(self) -> None:
        if self._mcp_status_done:
            return
        progress = getattr(self.app, "_mcp_progress", None)
        if progress is None:
            return
        servers, _active = progress.snapshot()
        if not servers:
            return
        from textual.widgets import Static
        from tui.services.mcp_progress import format_mcp_progress, format_mcp_settle
        if self._mcp_status_row is None:
            self._mcp_status_row = Static(format_mcp_progress(servers),
                                          id="mcp-status-row",
                                          classes="line-meta", markup=False)
            self._trace_mount(self._mcp_status_row, scroll=False)
        else:
            self._mcp_status_row.update(format_mcp_progress(servers))
        if progress.all_reported():
            self._mcp_status_done = True
            # Гасим sink — показ ровно один раз за сессию (на последующих
            # сообщениях executor кэширован, дискавери нет → индикатор не мелькает).
            self.app._mcp_progress = None
            # Финальный блок с итоговыми глифами — лингер 1с (видно, что все
            # серверы проверены), затем схлопывается в итог-запись: статус MCP
            # + «готов к работе». Ref захватываем, чтобы не задеть строку нового run.
            self._mcp_status_row.update(format_mcp_progress(servers))
            row = self._mcp_status_row
            self.set_timer(
                1.0, lambda: self._settle_mcp_row(row, format_mcp_settle(servers))
            )

    def _settle_mcp_row(self, row, final: str) -> None:
        """Схлопнуть финальный блок в итог-запись (статус + готовность)."""
        try:
            row.update(final)
        except Exception:
            pass

    def _mcp_screen_snapshot(self):
        """Снапшот для экрана /mcp: предпочитаем boot-результат (app._mcp_snapshot,
        выставлен при сборке executor на старте), иначе — из meta активного треда."""
        snap = getattr(self.app, "_mcp_snapshot", None)
        if snap is not None:
            return snap
        if self._store is not None and self._active_task_id is not None:
            meta = self._store.get_task_meta(self._active_task_id)
            return (meta.get("tui") or {}).get("mcp_tools")
        return None

    def _pending_wire_match(self, client_id: str, polled_content: str) -> bool:
        """Проверить, что pending-запись с этим client_id имеет wire_content,
        равный содержимому, пришедшему из polling-rendered .jsonl записи.

        Источник истины — in-memory кэш `_pending_wire_cache`, т.к. meta-файл
        может быть перезаписан executor'ом между submit'ом и polling-тиком."""
        return self._pending_wire_cache.get(client_id) == polled_content

    def _poll_active(self) -> None:
        # Отложенное удаление осиротевших реплик (cancel-with-undo), когда idle.
        self._run_orphan_cleanups()
        # Индикатор загрузки MCP — на старте (до появления активной задачи) тоже.
        self._update_mcp_indicator()
        err = getattr(self.app, "last_runner_error", None)
        if err and not self._error_shown:
            self._mount_trace_row(
                prefix="[!] ", content=f"runtime: {err}",
                kind_class="line-error",
            )
            self._error_shown = True
            self._mark_pending_failed()
        # Инвариант: running tool-строки живут, только пока задача жива по тому
        # же критерию, что и статус в шапке (_task_status_badge: локальный
        # runner / ask_human / внешний writer с meta.status=running). Стоп,
        # обрыв runner'а или resume брошенного треда → осиротевшие вызовы
        # финализируются «прервано».
        if self._active_calls and self._task_status_badge() == "[stopped]":
            for row in self._active_calls.values():
                row.mark_interrupted()
            self._active_calls.clear()
        # Тикающий elapsed на running tool-строках (running · Nс).
        for row in self._active_calls.values():
            row.refresh_running_elapsed()
        # ThinkingIndicator — пока runner работает, обычно виден внизу trace.
        # Эвристика «видимый ответ дан»: если последнее event это agent-текст
        # ИЛИ system-событие (think/policy — агент работает с текстом, не с
        # инструментами) и прошло ≥1.5с без новых событий → скрываем (hide-only,
        # не destroy). Если агент возьмёт инструмент (kind=call) — следующее
        # событие сбросит kind, индикатор вернётся с той же анимацией.
        _SOFT_HIDE_KINDS = {"agent", "system"}
        runner = getattr(self.app, "active_runner", None)
        is_running = runner is not None and runner.is_running()
        # Активный ask_human — агент НЕ работает, он ждёт ответ юзера.
        # Индикатор активности здесь обманывает; банер ask_human уже
        # говорит «нужен ответ». Скрываем (не destroy — анимация
        # вернётся когда юзер ответит и агент продолжит).
        ask_human_active = (
            getattr(self.app, "tui_state", None) is not None
            and self.app.tui_state.ask_human_state is not None
        )
        summarizing = False
        if self._store is not None and self._active_task_id is not None:
            try:
                summarizing = bool(
                    self._store.get_task_meta(self._active_task_id).get("tui_summarizing")
                )
            except Exception:
                summarizing = False

        if is_running and not ask_human_active:
            import time as _time
            since_last = _time.monotonic() - self._last_event_at
            if summarizing:
                self._show_thinking_indicator(state="compressing")
            elif self._last_event_kind == "_finalized":
                # Финальный submit_solution виден — индикатор переключается
                # в режим «✓ завершаю…» (статичный dim) и держится пока
                # runner.is_running() (intent_guard / MCP cleanup). Sentinel
                # сбрасывается в _render_records / _reset_thinking_timer /
                # _reset_render_state, после чего возвращается «думаю…».
                self._show_thinking_indicator(state="validating")
            elif self._last_event_kind in _SOFT_HIDE_KINDS and since_last >= 1.5:
                self._hide_thinking_indicator()
            else:
                self._show_thinking_indicator(state="thinking")
            # Эфемерный StreamBlock — live-вывод текущего шага. Источник —
            # app._stream_sink (кормится из worker-потока через on_stream_delta);
            # монтаж/обновление строго здесь, на UI-потоке (set_interval-колбэк).
            # StreamBlock — только в активной фазе «thinking». В validating
            # (финальный ответ виден, идёт finish-gate) и compressing reasoning
            # не показываем — иначе строка-мысль висит после финального ответа (баг B).
            _thinking_phase = (not summarizing) and (self._last_event_kind != "_finalized")
            sink = getattr(self.app, "_stream_sink", None)
            if _thinking_phase and sink is not None and sink.has_content():
                from tui.services.stream_sink import (
                    format_stream_block, format_reasoning_line,
                )
                from tui.widgets.stream_block import StreamBlock
                think, answer = sink.snapshot()
                ans = format_stream_block(answer=answer)
                _cfg = getattr(self.app, "_config", None) or {}
                if ans:
                    show = bool(_cfg.get("tui_show_stream_answer", False))
                    text, thinking = ans, False
                else:
                    show = bool(_cfg.get("tui_show_reasoning_line", True))
                    text, thinking = format_reasoning_line(think=think), True
                if show and text:
                    if self._stream_block is None:
                        self._stream_block = StreamBlock()
                        self._trace_mount(self._stream_block, scroll=True)
                    self._stream_block.set_text(text, thinking=thinking)
                else:
                    # render disabled by config (or nothing to show) — drop the
                    # widget but KEEP the sink (counter/indicator unaffected).
                    self._clear_stream_block(reset_sink=False)
            elif not _thinking_phase:
                self._clear_stream_block()
        elif ask_human_active:
            self._hide_thinking_indicator()
        else:
            self._destroy_thinking_indicator()
            self._clear_stream_block()
        # Footer-left зависит от is_running — обновляем каждый тик, чтобы
        # /stop появлялся/исчезал без отдельных вызовов из start/stop путей.
        self._refresh_footer()
        if self._store is None or self._active_task_id is None or self._cursor is None:
            return
        tail = self._store.tail_events(self._active_task_id, self._cursor)
        if tail.stream_reset:
            self._clear_trace()
            self._reset_render_state()
            records, cur = self._store.read_all_events(self._active_task_id)
            self._cursor = cur
            if records and self._store.is_compaction_summary(records[0]):
                self._render_segments(self._store.read_segments(self._active_task_id))
            else:
                self._render_records(records)
            # После компакции (stream_reset) баннер мог пере-монтироваться для уже
            # отвеченного вопроса — снять, иначе ввод уйдёт в stdin «вникуда».
            self._reconcile_ask_human_after_render(allow_remnant=False)
            self._refresh_header()
            self._refresh_footer()
            return
        self._cursor = tail.next_cursor
        self._render_records(tail.new_records, live=True)
        self._refresh_header()

    def _show_brand(self) -> None:
        if self._brand is not None and self._brand.parent is not None:
            return
        trace = self.query_one("#trace", VerticalScroll)
        # Тег-стабы тянем до ширины логотипа: `rule · TAGLINE · rule` ≈ ширине лого.
        logo_width = max(
            (len(line) for line in branding.LOGO.split("\n")), default=0,
        )
        tag_label = f" {branding.tagline_display()} "
        rule = "─" * max(0, (logo_width - len(tag_label)) // 2)
        tagline_row = Horizontal(
            Static(rule, classes="brand-rule", markup=False),
            Static(tag_label, classes="brand-tagline", markup=False),
            Static(rule, classes="brand-rule", markup=False),
            classes="brand-tagline-row",
        )
        brand = Vertical(
            Static(branding.logo_text(), classes="brand-logo"),
            tagline_row,
            id="brand-block",
        )
        trace.mount(brand)
        self._brand = brand

    # Шапка трейса: переживает пересборку (#brand-block — бренд,
    # #mcp-status-row — boot-статус MCP: app-уровневый, остаётся верным всю сессию).
    _TRACE_HEADER_IDS = frozenset({"brand-block", "mcp-status-row"})

    def _clear_trace(self) -> None:
        """Очистить #trace, сохранив шапку (инвариант: бренд — первый ребёнок).

        remove_children() в Textual асинхронный (отложенный prune), поэтому
        «снести всё + ремоунт» не работает: guard видит ещё живой parent,
        а немедленный ремоунт с тем же id дал бы DuplicateIds. Виджеты шапки
        просто не удаляем."""
        trace = self.query_one("#trace", VerticalScroll)
        trace.remove_children(
            [c for c in trace.children if c.id not in self._TRACE_HEADER_IDS]
        )
        self._show_brand()  # defensive: монтирует, только если шапки ещё нет

    @property
    def _any_picker_open(self) -> bool:
        """Открыт ли input-оверлей (палитра / skill / arg / file picker).

        Единственный источник истины для Esc-приоритета: сначала закрыть
        оверлей (их блоки в on_key), потом ask_human, потом Esc-state-machine.
        Новый оверлей — добавить сюда, guard'ы по веткам не дублировать."""
        return any(w is not None for w in (
            self._palette, self._skill_picker, self._arg_picker, self._file_picker,
        ))

    def on_key(self, event: events.Key) -> None:
        # Confirm-баннер имеет наивысший приоритет: одиночный y/N перехватываем
        # до истории, Esc-лестницы и любых пикеров. TextArea скрыта, клавиши
        # не потребляются ею и доходят сюда.
        if self._confirm_active:
            key = event.key
            if key in ("y", "Y", "enter"):
                self._answer_confirm(True)
            elif key in ("n", "N", "escape"):
                self._answer_confirm(False)
            # иначе — проглотить без действия (нет случайного ввода)
            event.stop()
            event.prevent_default()
            return

        # Любая не-Esc клавиша сбрасывает pending hint (без прерывания обработки).
        if event.key != "escape" and self._esc_confirm_kind is not None:
            self._clear_footer_hint()

        # Input history: ↑/↓ для навигации (smart line-position check).
        # ↑ → history prev только если курсор на 1-й строке.
        # ↓ → history next только если курсор на последней строке.
        # Иначе пропускаем — multi-line cursor-навигация работает как обычно.
        # Palette/picker имеют свои ↑/↓ ветки ниже, поэтому отключаем history-mode,
        # когда они открыты.
        if event.key == "up" and not self._any_picker_open:
            try:
                input_field = self.query_one("#cmdinput")
                if input_field.cursor_location[0] == 0:
                    self._history_prev()
                    event.stop()
                    event.prevent_default()
                    return
            except Exception:
                pass

        if event.key == "down" and not self._any_picker_open:
            try:
                input_field = self.query_one("#cmdinput")
                last_row = input_field.document.line_count - 1
                if input_field.cursor_location[0] == last_row:
                    self._history_next()
                    event.stop()
                    event.prevent_default()
                    return
            except Exception:
                pass

        # Esc — единая лестница приоритетов: оверлей (Esc-блоки пикеров ниже) →
        # 2-й-Esc-confirm → текст в инпуте (двухшаговая очистка) → ask_human →
        # running (undo-cancel / stop-hint) → выход. Esc всегда разбирает самый
        # локальный контекст; безусловный стоп — /stop или Ctrl+C.
        if event.key == "escape" and len(self.app.screen_stack) == 1 \
                and not self._any_picker_open:
            # 2-й Esc в окне 2s — выполнить отложенный action.
            if self._esc_confirm_kind is not None:
                kind = self._esc_confirm_kind
                self._clear_footer_hint()
                if kind == "clear":
                    try:
                        self.query_one("#cmdinput").text = ""
                    except Exception:
                        pass
                elif kind == "stop":
                    self._cmd_stop()
                event.stop()
                event.prevent_default()
                return

            input_text = ""
            try:
                input_text = self.query_one("#cmdinput").text
            except Exception:
                pass

            # Текст в инпуте → сначала двухшаговая очистка, при любом состоянии
            # (running / ask_human): расплата за случайный Esc — набранный
            # текст, а не убитый run или отменённый вопрос.
            if input_text:
                self._show_esc_hint("clear")
                event.stop()
                event.prevent_default()
                return

            # Пустое поле + live ask_human → clean stop треда (а не пустой ответ
            # агенту): юзер ожидает «выйти из вопроса», агент в петлю ask_human
            # не зацикливается. Zombie ask_human (no live runner) — снимаем
            # banner и продолжаем лестницу ниже (stop/exit).
            if self.app.tui_state.ask_human_state is not None:
                runner = getattr(self.app, "active_runner", None)
                if runner is not None and runner.is_running():
                    self._stop_for_ask_human()
                    event.stop()
                    return
                self._unmount_ask_human_banner()

            runner = getattr(self.app, "active_runner", None)
            is_running = runner is not None and runner.is_running()
            if is_running:
                # Cancel-with-undo case: 0 событий после отправки → сразу, без hint
                # (пустота инпута гарантирована веткой очистки выше).
                if self._events_since_last_user_send == 0:
                    self._cancel_with_undo()
                    event.stop()
                    event.prevent_default()
                    return
                # Иначе — показать stop hint.
                self._show_esc_hint("stop")
                event.stop()
                event.prevent_default()
                return

            # Пусто, run не идёт → модалка выхода.
            from tui.screens.confirm import ConfirmScreen

            def _on_exit(confirmed: bool) -> None:
                if confirmed:
                    self.app.exit()

            self.app.push_screen(
                ConfirmScreen(
                    title="Выход",
                    message="Завершить сессию?",
                    hint="[Y] да, выйти   [N] нет, остаться",
                ),
                _on_exit,
            )
            event.stop()
            event.prevent_default()
            return

        # Skill picker имеет приоритет над командной палитрой (mutex — но защитимся).
        if self._skill_picker is not None:
            if event.key == "escape":
                self._close_skill_picker()
                event.stop()
                event.prevent_default()
                return
            if event.key in ("up", "down"):
                self._skill_picker.move_selection(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                skill = self._skill_picker.selected_skill()
                if skill is not None:
                    inp = self.query_one("#cmdinput", CommandTextArea)
                    parts = inp.text.split(None, 1)
                    first = parts[0] if parts else "/skill"
                    self._suppress_picker_reopen = True
                    inp.text = f"{first} {skill['id']}"
                    inp.move_cursor(inp.document.end)
                    self._close_skill_picker()
                event.stop()
                event.prevent_default()
                return
            # Enter обрабатывается в on_command_text_area_submitted
            return

        # Arg picker (выбор значения аргумента, напр. /export txt|json) —
        # тот же key-контракт, что у skill picker.
        if self._arg_picker is not None:
            if event.key == "escape":
                self._close_arg_picker()
                event.stop()
                event.prevent_default()
                return
            if event.key in ("up", "down"):
                self._arg_picker.move_selection(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                value = self._arg_picker.selected_value()
                if value is not None:
                    inp = self.query_one("#cmdinput", CommandTextArea)
                    parts = inp.text.split(None, 1)
                    first = parts[0] if parts else "/export"
                    self._suppress_picker_reopen = True
                    inp.text = f"{first} {value}"
                    inp.move_cursor(inp.document.end)
                    self._close_arg_picker()
                event.stop()
                event.prevent_default()
                return
            # Enter обрабатывается в on_command_text_area_submitted
            return

        # File picker (@-аттач) — тот же key-контракт, что у skill/arg picker.
        if self._file_picker is not None:
            if event.key == "escape":
                self._close_file_picker()
                event.stop()
                event.prevent_default()
                return
            if event.key in ("up", "down"):
                self._file_picker.move_selection(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "tab":
                self._complete_file_picker()
                event.stop()
                event.prevent_default()
                return
            # Enter обрабатывается в on_command_text_area_submitted
            return

        if self._palette is None:
            return
        if event.key == "escape":
            self._close_palette()
            inp = self.query_one("#cmdinput", CommandTextArea)
            inp.text = ""
            event.stop()
            event.prevent_default()
        elif event.key in ("up", "down"):
            self._palette.move_selection(-1 if event.key == "up" else 1)
            event.stop()
            event.prevent_default()
        elif event.key == "tab":
            cmd = self._palette.selected_command()
            if cmd is not None:
                inp = self.query_one("#cmdinput", CommandTextArea)
                inp.text = f"/{cmd.name} "
                inp.move_cursor(inp.document.end)
                self._close_palette()
            event.stop()
            event.prevent_default()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "cmdinput":
            return
        # Печать в input после 1-го Esc сбрасывает pending hint — клавиша уже
        # «не Esc», подтверждать нечего. Screen.on_key для печатных не вызывается
        # (TextArea их потребляет), поэтому ловим здесь.
        if self._esc_confirm_kind is not None:
            self._clear_footer_hint()
        # Сброс history-navigation state при user-правке input.
        # Программные правки из _history_prev/_next запоминают последний text;
        # если current совпадает — это Changed от навигации, не сбрасываем.
        if self._history_index is not None:
            current_text = event.text_area.text
            if current_text != self._history_last_programmatic_text:
                self._reset_history_navigation()
        # Для палитры/пикера важна только первая строка (команды без переносов).
        val = event.text_area.text.split("\n", 1)[0]

        # Состояние 1: /<prefix> без пробела → командная палитра.
        if val.startswith("/") and " " not in val:
            if self._skill_picker is not None:
                self._close_skill_picker()
            if self._arg_picker is not None:
                self._close_arg_picker()
            if self._palette is None:
                self._open_palette(prefix=val[1:])
            else:
                self._palette.set_filter(val[1:])
            return

        # Состояние 2: /skill <фильтр> → skill-picker.
        if val.startswith("/") and " " in val:
            first, _, rest = val.partition(" ")
            cmd = first[1:]
            if cmd == "skill":
                # /skill <валидный_id> <текст> → скилл зафиксирован, дальше промпт:
                # закрываем picker (его фильтр по 'id текст' бессмыслен).
                if self._skills_cache is None:
                    self._skills_cache = self._load_skills_safe()
                skill_first, _, skill_rest = rest.partition(" ")
                if skill_rest.strip() and any(
                    str(s.get("id")) == skill_first for s in self._skills_cache
                ):
                    if self._skill_picker is not None:
                        self._close_skill_picker()
                    return
                if self._suppress_picker_reopen:
                    self._suppress_picker_reopen = False
                    return
                if self._palette is not None:
                    self._close_palette()
                if self._skill_picker is None:
                    self._open_skill_picker(prefix=rest)
                else:
                    self._skill_picker.set_filter(rest)
                return

            # Состояние 3: /<cmd> <фильтр>, где у команды есть arg_options → arg-picker.
            cmd_obj = self._registry.lookup(cmd)
            if cmd_obj is not None and cmd_obj.arg_options:
                if self._suppress_picker_reopen:
                    self._suppress_picker_reopen = False
                    return
                if self._palette is not None:
                    self._close_palette()
                if self._skill_picker is not None:
                    self._close_skill_picker()
                if self._arg_picker is None:
                    self._open_arg_picker(options=list(cmd_obj.arg_options), prefix=rest)
                else:
                    self._arg_picker.set_filter(rest)
                return

        # Состояние 4: активный @-сегмент под курсором → file-picker.
        inp = event.text_area
        try:
            row, col = inp.cursor_location
            line = inp.text.split("\n")[row]
        except Exception:
            line, col = val, len(val)
        from tui.services.file_index import parse_at_segment
        seg = parse_at_segment(line, col)
        if seg is not None:
            if self._palette is not None:
                self._close_palette()
            if self._skill_picker is not None:
                self._close_skill_picker()
            if self._arg_picker is not None:
                self._close_arg_picker()
            _, query = seg
            if self._file_picker is None:
                self._open_file_picker(query=query)
            else:
                self._refresh_file_picker(query=query)
            return
        if self._file_picker is not None:
            self._close_file_picker()

        # Иначе: все закрыты.
        if self._palette is not None:
            self._close_palette()
        if self._skill_picker is not None:
            self._close_skill_picker()
        if self._arg_picker is not None:
            self._close_arg_picker()

    def _open_palette(self, *, prefix: str) -> None:
        if self._palette is not None:
            return
        palette = CommandPalette(self._registry, initial_filter=prefix)
        self._palette = palette
        self.query_one("#root", Vertical).mount(
            palette, before=self.query_one("#input-row", Horizontal)
        )

    def _close_palette(self) -> None:
        if self._palette is None:
            return
        self._palette.remove()
        self._palette = None

    def _open_skill_picker(self, *, prefix: str) -> None:
        if self._skill_picker is not None:
            return
        if self._skills_cache is None:
            self._skills_cache = self._load_skills_safe()
        from tui.widgets.skill_picker import SkillPicker
        picker = SkillPicker(self._skills_cache, initial_filter=prefix)
        self._skill_picker = picker
        self.query_one("#root", Vertical).mount(
            picker, before=self.query_one("#input-row", Horizontal)
        )

    def _close_skill_picker(self) -> None:
        if self._skill_picker is None:
            return
        self._skill_picker.remove()
        self._skill_picker = None

    def _open_arg_picker(self, *, options: list[tuple[str, str]], prefix: str) -> None:
        if self._arg_picker is not None:
            return
        from tui.widgets.arg_picker import ArgPicker
        picker = ArgPicker(options, initial_filter=prefix)
        self._arg_picker = picker
        self.query_one("#root", Vertical).mount(
            picker, before=self.query_one("#input-row", Horizontal)
        )

    def _close_arg_picker(self) -> None:
        if self._arg_picker is None:
            return
        self._arg_picker.remove()
        self._arg_picker = None

    def _project_root(self) -> str:
        """Корень обзора @-аттача = текущий cwd проекта (корень FS-тулов агента)."""
        return self._current_cwd()

    def _file_picker_entries(self, query: str) -> list:
        """Entries для file-picker: query с хвостовым '/' — листинг каталога,
        иначе fuzzy-поиск по корню проекта."""
        from tui.services.file_index import fuzzy_search, list_dir
        root = self._project_root()
        return (list_dir(root, query[:-1]) if query.endswith("/")
                else fuzzy_search(root, query))

    def _open_file_picker(self, *, query: str) -> None:
        if self._file_picker is not None:
            self._refresh_file_picker(query=query)
            return
        from tui.widgets.file_picker import FilePicker
        picker = FilePicker(self._file_picker_entries(query))
        self._file_picker = picker
        self.query_one("#root", Vertical).mount(
            picker, before=self.query_one("#input-row", Horizontal)
        )

    def _refresh_file_picker(self, *, query: str) -> None:
        if self._file_picker is None:
            return
        self._file_picker.set_entries(self._file_picker_entries(query))

    def _close_file_picker(self) -> None:
        if self._file_picker is None:
            return
        self._file_picker.remove()
        self._file_picker = None

    def _complete_file_picker(self) -> None:
        """Достроить выбранный путь в @-сегмент. Папка → '@dir/' + остаёмся;
        файл → '@path ' + закрыть."""
        if self._file_picker is None:
            return
        entry = self._file_picker.selected_entry()
        if entry is None:
            return
        from tui.services.file_index import parse_at_segment
        inp = self.query_one("#cmdinput", CommandTextArea)
        row, col = inp.cursor_location
        lines = inp.text.split("\n")
        line = lines[row]
        seg = parse_at_segment(line, col)
        if seg is None:
            return
        start, _query = seg
        is_dir = entry.endswith("/")
        replacement = "@" + entry + ("" if is_dir else " ")
        new_line = line[:start] + replacement + line[col:]
        lines[row] = new_line
        inp.text = "\n".join(lines)
        inp.cursor_location = (row, start + len(replacement))
        if is_dir:
            self._refresh_file_picker(query=entry)
        else:
            self._close_file_picker()

    def on_command_text_area_submitted(
        self, event: CommandTextArea.Submitted
    ) -> None:
        inp = self.query_one("#cmdinput", CommandTextArea)
        # Skill picker открыт → выполнить выбранный skill (или ввод как есть)
        if self._skill_picker is not None:
            skill = self._skill_picker.selected_skill()
            self._close_skill_picker()
            text = event.value.strip()
            inp.text = ""
            if skill is not None and text:
                parts = text.split(None, 1)
                first = parts[0] if parts else "/skill"
                cmd_name = first[1:] if first.startswith("/") else first
                self._handle_command(f"{cmd_name} {skill['id']}")
            elif text.startswith("/"):
                # Пустой результат фильтра — выполнить ввод как обычную команду.
                self._handle_command(text[1:])
            return

        # File picker открыт → Enter достраивает путь (не сабмитит сообщение).
        if self._file_picker is not None:
            self._complete_file_picker()
            return

        # Arg picker открыт → Enter выполняет команду с выбранным/набранным значением.
        if self._arg_picker is not None:
            value = self._arg_picker.selected_value()
            self._close_arg_picker()
            typed = event.value.strip()
            inp.text = ""
            if typed.startswith("/"):
                parts = typed.split(None, 1)
                cmd_name = parts[0][1:]
                typed_arg = parts[1].strip() if len(parts) > 1 else ""
                arg = value if value is not None else typed_arg
                self._handle_command(f"{cmd_name} {arg}".strip())
            return

        # Если открыта палитра — Enter выполняет выбранную/набранную команду
        # (логика перенесена из on_key, т.к. TextArea перехватывает Enter).
        if self._palette is not None:
            cmd = self._palette.selected_command()
            typed = event.value.strip()
            has_args = len(typed.split()) > 1
            self._close_palette()
            inp.text = ""
            if cmd is not None and not has_args:
                self._handle_command(cmd.name)
            elif typed.startswith("/"):
                self._handle_command(typed[1:])
            return

        text = event.value.strip()
        inp.text = ""
        if not text:
            return

        # Ask-human mode: input is the answer to the agent's question.
        if self.app.tui_state.ask_human_state is not None:
            target = self._ask_human_target_runner()
            if target is not None:
                # Живой in-run диалог: владелец вопроса тот же и работает → stdin.
                client_id = self._track_pending(display_text=text, wire_content=text)
                self._mount_user_reply(text, state="pending", client_id=client_id)
                self._reset_thinking_timer()
                self._on_user_text_submitted(text)
                target.submit_ask_human_reply(text)
                return
            # Владельца нет / сменился / завис → отвечаем новым ходом.
            self._unmount_ask_human_banner()
            runner = self.app.active_runner
            if runner is not None and runner.is_running():
                # Чужой/зависший раннер не читает этот вопрос — кооперативно стопаем,
                # чтобы get_runner ниже создал свежий и start не отбился.
                self._stop_and_replace_runner()
            # fall-through ↓ к "continue thread": тот же active_task_id, start(answer).

        if text.startswith("/"):
            self._handle_command(text[1:])
            return

        runner = self.app.get_runner()

        # id user-сообщения этого хода: пробрасываем в run, чтобы при быстрой
        # отмене (до вывода агента) точечно удалить осиротевшую реплику из треда.
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        if self.app.tui_state.active_task_id is None:
            # AUTO-CREATE: первый свободный текст → новая задача.
            # Применяем pre-stage: pending_skill_id встраиваем в первое
            # сообщение и фиксируем в meta.tui.
            tid = uuid.uuid4().hex
            self.app.tui_state.active_task_id = tid
            self._active_task_id = tid
            # Открываем store сейчас, чтобы _track_pending мог писать meta.
            self._store = FileTaskStore(
                threads_dir=self.app._threads_dir or "",
                system=self.app._system,
            )
            full_query, patch = self._consume_pending_prefix(text)
            technical = self._build_attachment_technical(text)
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(text)
            runner.start(query=full_query, thread_id=tid, system=self.app._system,
                         message_id=msg_id, technical=technical)
            self.attach_active_task(tid)
            if patch:
                self._store.update_tui_meta(tid, patch)
            branch = current_branch(self._current_cwd())
            if branch:
                self._store.update_tui_meta(tid, {"branch": branch})
            # _track_pending — после attach_active_task (которое чистит
            # _pending_user_widgets через _reset_render_state) и после
            # потенциальной первой записи meta исполнителем.
            client_id = self._track_pending(display_text=text, wire_content=full_query)
            self._mount_user_reply(text, state="pending", client_id=client_id)
            return

        if runner.is_running():
            client_id = self._track_pending(display_text=text, wire_content=text)
            self._mount_user_reply(text, state="pending", client_id=client_id)
            self._reset_thinking_timer()
            self._on_user_text_submitted(text)
            runner.add_message({"role": "user", "content": text, "id": msg_id})
            technical = self._build_attachment_technical(text)
            if technical is not None:
                runner.add_message({"role": "user", "content": technical})
            return

        # Задача есть, но run завершился (или открыта через /sessions) — продолжаем тот же
        # thread, применяя отложенные skill/cwd к этому сообщению.
        full_query, patch = self._consume_pending_prefix(text)
        if patch and self._store is not None:
            self._store.update_tui_meta(self.app.tui_state.active_task_id, patch)
        client_id = self._track_pending(display_text=text, wire_content=full_query)
        self._mount_user_reply(text, state="pending", client_id=client_id)
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(text)
        technical = self._build_attachment_technical(text)
        runner.start(query=full_query, thread_id=self.app.tui_state.active_task_id,
                     system=self.app._system, message_id=msg_id, technical=technical)

    def on_command_text_area_paste_collapsed(
        self, event: CommandTextArea.PasteCollapsed
    ) -> None:
        self._show_footer_hint(_PASTE_HINT_TEXT, seconds=5.0)

    def _consume_pending_prefix(self, text: str) -> tuple[str, dict]:
        """Встроить отложенный skill в сообщение; вернуть (query, meta-patch).

        Сбрасывает pending_skill_id. Используется и при auto-create,
        и при продолжении существующего thread'а.
        """
        pending_skill = self.app.tui_state.pending_skill_id
        self.app.tui_state.pending_skill_id = None
        prefix: list[str] = []
        patch: dict = {}
        if pending_skill:
            prefix.append(f"Используй skill `{pending_skill}`.")
            patch["skills"] = [pending_skill]
        return ("\n".join(prefix + [text]) if prefix else text), patch

    def _build_attachment_technical(self, text: str) -> Optional[str]:
        """Технический футер для @-вложений в тексте (резолв на сабмите).
        None — если в тексте нет существующих @-путей."""
        from tui.services.file_index import (
            extract_at_mentions, resolve_attachment, build_attachment_footer,
        )
        from message_policy.user_message_policy import wrap_technical_user_content
        root = self._project_root()
        resolved = []
        for rel in extract_at_mentions(text):
            r = resolve_attachment(root, rel)
            if r is not None:
                resolved.append(r)
        body = build_attachment_footer(resolved)
        if not body:
            return None
        return wrap_technical_user_content(body, technical_type="technical")

    def _cmd_config(self) -> None:
        from tui.screens.config import ConfigScreen
        self.app.push_screen(ConfigScreen(
            config=getattr(self.app, "_config", None) or {},
            config_path=getattr(self.app, "_config_path", None),
            project_local=getattr(self.app, "_config_project_local", None),
        ))

    def _cmd_mcp(self) -> None:
        from tui.screens.mcp import McpScreen
        self.app.push_screen(McpScreen(
            config=getattr(self.app, "_config", None) or {},
            tools_snapshot=self._mcp_screen_snapshot(),
        ))

    def _cmd_new(self) -> None:
        runner = getattr(self.app, "active_runner", None)
        is_running = runner is not None and runner.is_running()
        if not is_running:
            self._switch_to_empty_state()
            return
        from tui.screens.confirm import ConfirmScreen

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._stop_and_replace_runner()
                self._switch_to_empty_state()

        self.app.push_screen(
            ConfirmScreen(
                title="Новая задача",
                message="Активная задача в процессе.\nОстановить и начать новую?",
                hint="[Y] да, прервать   [N] нет, продолжить",
            ),
            _on_result,
        )

    def _cmd_stop(self) -> None:
        runner = self.app.active_runner
        if runner is None or not runner.is_running():
            self._mount_trace_row(
                prefix="[stopped] ", content="нет активного запуска",
                kind_class="line-meta",
            )
            return

        # Cancel-with-undo (агент не успел ничего выдать + input пустой) — отдельно.
        input_text = ""
        try:
            input_text = self.query_one("#cmdinput").text
        except Exception:
            pass
        if self._events_since_last_user_send == 0 and not input_text:
            self._cancel_with_undo()
            return

        # Normal interrupt: immediate [stopped] + destroy + replace.
        self._mount_trace_row(prefix="[stopped]", content="", kind_class="line-meta")
        self._destroy_thinking_indicator()
        self._stop_and_replace_runner()

    def _stop_and_replace_runner(self) -> None:
        """Кооперативный stop + mark + обнуление active_runner.

        Следующий self.app.get_runner() создаст свежий — clean restart для
        нового user-запроса. Старый runner может ещё досидеть HTTP в фоне;
        благодаря discard_writes_after_stop его output в thread_store не уйдёт.
        """
        runner = self.app.active_runner
        if runner is not None:
            runner.stop()
            runner._marked_stopped = True
        self.app.active_runner = None

    def _stop_for_ask_human(self) -> None:
        """Esc в live ask_human → clean stop треда.

        Шаги:
        1. Разблокировать agent-thread, заблокированный в input() внутри
           ask_human-тула: положить пустую строку в stdin-queue через
           runner.cancel_ask_human() (контракт по сути «unblock stdin»,
           а не «cancel» в семантике юзера).
        2. Снять banner + сбросить tui_state.ask_human_state.
        3. Смонтировать [stopped]-метку в трейсе.
        4. Погасить ThinkingIndicator.
        5. runner.stop() + active_runner = None (clean-restart pattern).

        После этого state.stop_requested=True; agent-loop выйдет на следующей
        проверке. discard_writes_after_stop+stop_requested блокирует persist
        синтетического function_call_output от тула → в .jsonl ничего не
        запишется (нет повторного re-mount banner-а через mapper).

        Следующий submit юзера: get_runner() создаст свежий runner,
        runner.start(query=text, thread_id=<existing>) продолжит тот же тред."""
        runner = self.app.active_runner
        if runner is None or not runner.is_running():
            return
        runner.cancel_ask_human()
        self._unmount_ask_human_banner()
        self._mount_trace_row(prefix="[stopped]", content="", kind_class="line-meta")
        self._destroy_thinking_indicator()
        self._stop_and_replace_runner()

    def _cancel_with_undo(self) -> None:
        """Откатить последний user-send: убрать user-widget, восстановить input.

        Условие срабатывания (проверено в _cmd_stop):
        - 0 trace-событий от агента с момента последнего user-send.
        - input пуст.

        Реплика к этому моменту уже могла быть записана в тред (persist user-
        сообщения происходит до ответа бэкенда). Поэтому, помимо визуального
        отката, ставим заявку на удаление этой записи по id — она исполнится в
        poll, когда тред станет idle (см. _run_orphan_cleanups): без гонок с
        дозаписью worker'а.
        """
        from tui.widgets.user_reply import UserReplyContainer

        # Заявка на удаление осиротевшей реплики (по id, когда тред idle).
        old_runner = getattr(self.app, "active_runner", None)
        tid = self.app.tui_state.active_task_id
        msg_id = self._last_user_message_id
        if tid is not None and msg_id and self._store is not None:
            self._pending_orphan_cleanups.append((old_runner, tid, msg_id))

        # Удалить последний UserReplyContainer
        user_widgets = list(self.query(UserReplyContainer))
        if user_widgets:
            user_widgets[-1].remove()

        # Полный откат: убрать pending-запись из meta (иначе на re-entry
        # _mount_failed_from_meta покажет её как фантомную [!] failed-реплику).
        cid = self._last_pending_client_id
        if cid is not None:
            self._remove_pending_record(cid)
            self._pending_user_widgets.pop(cid, None)
            self._last_pending_client_id = None

        # Восстановить текст в input + поставить курсор в конец, чтобы
        # юзер мог сразу продолжить печатать (а не правил с начала строки).
        try:
            input_field = self.query_one("#cmdinput")
            input_field.text = self._last_user_send_text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

        # Убить индикатор + заменить runner — как в normal stop
        self._destroy_thinking_indicator()
        self._stop_and_replace_runner()

        # Немедленно удалить осиротевшую реплику из треда: worker уже остановлен
        # флагом и после stop в data_*.jsonl не пишет (запись реплики, если была,
        # уже на диске; вывод агента — под discard-guard). Так после re-entry её
        # не будет, даже если HTTP-вызов worker'а ещё висит на флапающем бэкенде.
        # Заявка _pending_orphan_cleanups (см. выше) — backstop на микро-гонку
        # «persist ровно в момент удаления».
        if tid is not None and msg_id:
            self._discard_orphan_and_resync(tid, msg_id)
        # НЕ монтируем [stopped] — это и есть "undo".

    def _discard_orphan_and_resync(self, tid: str, msg_id: str) -> bool:
        """Удалить запись реплики по id и пересинхронизировать курсор трейса.

        Без ресинка poll увидит ужавшийся data_*.jsonl как «file truncated» →
        stream_reset → полная перерисовка трейса (моргание). Здесь курсор
        сдвигается на новый конец файла, поэтому тик ничего не перерисовывает —
        виджет уже снят точечно."""
        if self._store is None:
            return False
        try:
            removed = self._store.discard_message_by_id(tid, msg_id)
        except Exception:
            return False
        if removed and tid == self._active_task_id and self._cursor is not None:
            try:
                _records, cur = self._store.read_all_events(tid)
                self._cursor = cur
            except Exception:
                pass
        return removed

    def _run_orphan_cleanups(self) -> None:
        """Удалить осиротевшие реплики из треда, когда тред idle (нет активного
        run'а, пишущего этот файл) и worker отменённого run'а уже остановлен.
        Вызывается из poll как backstop. id-based → точечно и без гонок."""
        if not self._pending_orphan_cleanups:
            return
        active = getattr(self.app, "active_runner", None)
        active_tid = self.app.tui_state.active_task_id
        active_running = active is not None and active.is_running()
        still_pending: list[tuple] = []
        for old_runner, tid, msg_id in self._pending_orphan_cleanups:
            old_busy = old_runner is not None and old_runner.is_running()
            thread_busy = active_running and active_tid == tid
            if old_busy or thread_busy:
                still_pending.append((old_runner, tid, msg_id))
                continue
            self._discard_orphan_and_resync(tid, msg_id)
        self._pending_orphan_cleanups = still_pending

    def _reset_history_navigation(self) -> None:
        """Выход из режима history-навигации. Зовётся при правке input или send."""
        self._history_index = None
        self._history_draft = None
        self._history_last_programmatic_text = None

    def _history_prev(self) -> None:
        """Шаг назад по истории. На первый вызов — сохранить draft."""
        if self._history is None or self._history.size == 0:
            return
        if self._history_index is None:
            try:
                input_field = self.query_one("#cmdinput")
                self._history_draft = input_field.text
            except Exception:
                self._history_draft = ""
            self._history_index = self._history.size
        result = self._history.prev(self._history_index)
        if result is None:
            return
        new_index, text = result
        self._history_index = new_index
        self._history_last_programmatic_text = text
        try:
            input_field = self.query_one("#cmdinput")
            input_field.text = text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _history_next(self) -> None:
        """Шаг вперёд по истории. За newest — вернуть draft."""
        if self._history is None or self._history_index is None:
            return
        result = self._history.next(self._history_index)
        if result is None:
            draft_text = self._history_draft or ""
            # Возврат к draft: технически это «выход» из навигации,
            # но Changed для draft-текста придёт ПОСЛЕ обнуления state
            # → ничего не нужно ловить, элиф ниже его игнорирует.
            try:
                input_field = self.query_one("#cmdinput")
                input_field.text = draft_text
                input_field.cursor_location = input_field.document.end
            except Exception:
                pass
            self._history_index = None
            self._history_draft = None
            self._history_last_programmatic_text = None
            return
        new_index, text = result
        self._history_index = new_index
        self._history_last_programmatic_text = text
        try:
            input_field = self.query_one("#cmdinput")
            input_field.text = text
            input_field.cursor_location = input_field.document.end
        except Exception:
            pass

    def _cmd_use_skill(self, skill_id: str, *, prompt: str = "") -> None:
        if not skill_id:
            self._mount_trace_row(
                prefix="[!] ", content="/skill: укажите id навыка",
                kind_class="line-error",
            )
            return
        if prompt:
            self._deliver_skill_with_prompt(skill_id, prompt)
            return
        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            # Идёт run → подкидываем как сообщение и фиксируем в meta.
            runner.add_message({
                "role": "user",
                "content": f"Загрузи skill `{skill_id}` и продолжай работать с его инструментами.",
            })
            self._record_skill_meta(skill_id)
            self._mount_trace_row(
                prefix="[skill] ",
                content=f"`{skill_id}` отправлен агенту",
                kind_class="line-meta",
            )
            return
        # Run не идёт (нет задачи / завершена / открыта через /sessions) — отложенно
        # к следующему сообщению.
        self.app.tui_state.pending_skill_id = skill_id
        self._record_skill_meta(skill_id)
        self._mount_trace_row(
            prefix="[skill] ",
            content=f"`{skill_id}` применится к следующему сообщению",
            kind_class="line-meta",
        )

    def _deliver_skill_with_prompt(self, skill_id: str, prompt: str) -> None:
        """Inline /skill <id> <prompt>: префикс «Используй skill …» встраивается в
        реплику тем же _consume_pending_prefix, что и pending-путь — единая форма
        доставки. Technical-обёртка не используется: системный промпт трактует
        technical-блоки как контекст, не команды, и слабые модели вправе их
        игнорировать. Дисплей: маркер [skill] + реплика с чистым промптом."""
        self.app.tui_state.pending_skill_id = skill_id
        full_query, _ = self._consume_pending_prefix(prompt)
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        self._mount_trace_row(
            prefix="[skill] ", content=f"`{skill_id}` отправлен агенту",
            kind_class="line-meta",
        )

        runner = self.app.active_runner
        if runner is not None and runner.is_running():
            runner.add_message({"role": "user", "content": full_query, "id": msg_id})
            client_id = self._track_pending(display_text=prompt, wire_content=full_query)
            self._mount_user_reply(prompt, state="pending", client_id=client_id)
            self._reset_thinking_timer()
            self._on_user_text_submitted(prompt)
            self._record_skill_meta(skill_id)
            return

        if self.app.tui_state.active_task_id is None:
            # AUTO-CREATE: зеркалим порядок auto-create в on_command_text_area_submitted.
            tid = uuid.uuid4().hex
            self.app.tui_state.active_task_id = tid
            self._active_task_id = tid
            # Открываем store сейчас, чтобы _track_pending мог писать meta.
            self._store = FileTaskStore(
                threads_dir=self.app._threads_dir or "",
                system=self.app._system,
            )
            runner = self.app.get_runner()
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(prompt)
            runner.start(query=full_query, thread_id=tid, system=self.app._system,
                         message_id=msg_id)
            # attach_active_task — после start, как в реальном auto-create;
            # сбрасывает _pending_user_widgets через _reset_render_state.
            self.attach_active_task(tid)
            # _track_pending и _mount_user_reply — ПОСЛЕ attach_active_task,
            # иначе _reset_render_state вычистит только что добавленный widget.
            self._record_skill_meta(skill_id)
            client_id = self._track_pending(display_text=prompt, wire_content=full_query)
            self._mount_user_reply(prompt, state="pending", client_id=client_id)
            return

        # Задача есть, но run не идёт — продолжаем тот же thread.
        # Зеркалим порядок continue-thread в on_command_text_area_submitted.
        runner = self.app.get_runner()
        client_id = self._track_pending(display_text=prompt, wire_content=full_query)
        self._mount_user_reply(prompt, state="pending", client_id=client_id)
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(prompt)
        runner.start(query=full_query, thread_id=self.app.tui_state.active_task_id,
                     system=self.app._system, message_id=msg_id)
        self._record_skill_meta(skill_id)

    def _record_skill_meta(self, skill_id: str) -> None:
        """Зафиксировать skill_id в meta.tui.skills активной задачи."""
        tid = self.app.tui_state.active_task_id
        if tid is not None and self._store is not None:
            current = (self._store.get_task_meta(tid).get("tui") or {}).get("skills", [])
            if skill_id not in current:
                self._store.update_tui_meta(tid, {"skills": current + [skill_id]})

    def _switch_to_empty_state(self) -> None:
        """Очистить trace и перейти в empty-state. pending_skill_id/cwd сохраняются."""
        self._active_task_id = None
        self.app.tui_state.active_task_id = None
        self._store = None
        self._cursor = None
        self._reset_render_state()
        self._clear_trace()
        self._refresh_header()
        self._refresh_footer()

    def _handle_command(self, raw: str) -> None:
        """Диспетчеризовать команду и, если она что-то дописала в трейс,
        форсировать скролл вниз. Команда — прямое действие пользователя, поэтому
        её результат показываем безусловно, даже если юзер отскроллен вверх
        (в отличие от агентских событий). Команды, открывающие экран, трейс не
        растят → счётчик не меняется → позиция чтения истории не сбивается."""
        trace = self.query_one("#trace", VerticalScroll)
        before = len(trace.children)
        self._dispatch_command(raw)
        if len(trace.children) > before:
            trace.scroll_end(animate=False)

    def _dispatch_command(self, raw: str) -> None:
        # Имя команды + остаток строки как единственный аргумент, дословно.
        # Без shlex: путь (D:\dev\...) берётся как есть, без escape/кавычек.
        raw = raw.strip()
        if not raw:
            return
        parts = raw.split(None, 1)
        name = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        cmd = self._registry.lookup(name)
        if cmd is None:
            self._render_command_error(name, closest_command(name, self._registry))
            return
        # Нормализуем alias → canonical, чтобы диспетчер видел только канон.
        name = cmd.name
        if name == "quit":
            self.app.exit()
            return
        if name == "sessions":
            self.app.action_sessions()
            return
        if name == "new":
            self._cmd_new()
            return
        if name == "skill":
            if self._skills_cache is None:
                self._skills_cache = self._load_skills_safe()
            first, _, rest = arg.partition(" ")
            rest = rest.strip()
            if rest and any(str(s.get("id")) == first for s in self._skills_cache):
                # /skill <валидный_id> <промпт> → немедленная пара
                self._cmd_use_skill(first, prompt=rest)
            else:
                # первый токен не валидный id → весь arg = id (может содержать
                # пробелы/слэши), промпта нет → штатный pending/running путь
                self._cmd_use_skill(arg.strip(), prompt="")
            return
        if name == "help":
            from tui.screens.help import HelpScreen
            self.app.push_screen(HelpScreen())
            return
        if name == "skills":
            from tui.screens.skills import SkillsScreen
            self.app.push_screen(SkillsScreen(
                skills=self._load_skills_safe(),
                config=getattr(self.app, "_config", None) or {},
            ))
            return
        if name == "config":
            self._cmd_config()
            return
        if name == "mcp":
            self._cmd_mcp()
            return
        if name == "stop":
            self._cmd_stop()
            return
        if name == "export":
            self._cmd_export(arg)
            return
        if name == "status":
            self._cmd_status(arg)
            return

    def _open_event_by_no(self, no: int) -> None:
        """Открыть полный payload события №no в Viewer.
        Триггер — двойной клик по строке tool-вызова / агентской реплики."""
        for ev in self._task_events:
            if ev.no == no:
                from tui.screens.viewer import ViewerScreen
                # ViewerScreen сам делает render_payload — pretty-print JSON
                # с solarized-dark подсветкой для dict/list или plain Static
                # для остального. Передаём raw payload без предварительного dumps.
                self.app.push_screen(ViewerScreen(
                    title=f"event {no} · {ev.tool_name or ev.kind}",
                    content=ev.full_payload,
                ))
                return
        self._mount_trace_row(
            prefix="[!] ", content=f"нет события {no}",
            kind_class="line-error",
        )

    def on_open_event_request(self, message) -> None:
        """Двойной клик по строке tool-вызова/реплики → открыть payload."""
        self._open_event_by_no(message.event_no)

    def on_tool_call_focus_changed(self, message) -> None:
        """ToolCallRow получила/потеряла фокус → toggle preview-widget."""
        preview = self._tool_call_preview_widgets.get(message.call_id)
        if preview is None:
            return
        if message.focused:
            preview.remove_class("hidden")
        else:
            preview.add_class("hidden")

    def on_summary_toggle_request(self, message) -> None:
        """Клик/Enter по маркеру или телу → раскрыть/свернуть summary-body."""
        body = self._summary_body_widgets.get(message.event_no)
        if body is None:
            return
        if body.has_class("hidden"):
            body.remove_class("hidden")
        else:
            body.add_class("hidden")

    def on_open_tool_call_detail_request(self, message) -> None:
        """Двойной клик / Enter по row → детальный экран call + output."""
        call_ev = None
        result_ev = None
        for ev in self._task_events:
            if ev.call_id != message.call_id:
                continue
            if ev.kind == "call":
                call_ev = ev
            elif ev.kind in ("result", "error"):
                result_ev = ev
        if call_ev is None:
            return
        from tui.screens.tool_call_detail import ToolCallDetailScreen
        self.app.push_screen(ToolCallDetailScreen(
            call_event=call_ev, result_event=result_ev,
        ))

    def _load_skills_safe(self) -> list[dict]:
        """Загрузить список скиллов из core-реестра; на ошибке вернуть []."""
        try:
            from tools.skills.registry import get_skills_registry
            reg = get_skills_registry(getattr(self.app, "_config", None) or {})
            # include_all=True — показываем и внутренние скиллы паков, не только bootstrap.
            return reg.list_skills(include_all=True)
        except Exception as exc:
            self._mount_trace_row(
                prefix="[!] ", content=f"/skills: не удалось загрузить ({exc})",
                kind_class="line-error",
            )
            return []

    def _cmd_export(self, arg: str) -> None:
        fmt = (arg or "").strip() or "txt"
        if fmt not in ("txt", "json"):
            self._mount_trace_row(
                prefix="[!] ", content=f"/export: формат {fmt!r} не поддерживается",
                kind_class="line-error",
            )
            return
        tid = self._active_task_id
        if not tid or self._store is None:
            self._mount_trace_row(
                prefix="[!] ", content="/export: нет активной задачи",
                kind_class="line-error",
            )
            return
        segments = self._store.read_segments(tid)
        meta = self._store.get_task_meta(tid)
        text = export_thread(segments, fmt=fmt, meta=meta,
                             thread_id=tid, system=self.app._system)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = Path.cwd() / f"export-{tid}-{stamp}.{fmt}"
        path.write_text(text, encoding="utf-8")
        self._mount_trace_row(
            prefix="[exported] ", content=str(path), kind_class="line-meta",
        )

    def _cmd_status(self, arg: str) -> None:
        """Дамп meta активной задачи + runtime-состояние TUI в Viewer.

        Флаг `--raw` принимается в `arg`, но MVP-формат один (JSON с отступами);
        семантика raw vs formatted зарезервирована за будущим.
        """
        import json
        from tui.screens.viewer import ViewerScreen

        tid = self.app.tui_state.active_task_id
        payload: dict = {}

        if tid is not None and self._store is not None:
            payload["meta"] = self._store.get_task_meta(tid)
            title_id = tid[:8]
        else:
            payload["meta"] = None
            title_id = "нет задачи"

        runner = self.app.active_runner
        ah = self.app.tui_state.ask_human_state
        payload["runtime"] = {
            "active_task_id": tid,
            "runner_is_running": bool(runner and runner.is_running()),
            "pending_skill_id": self.app.tui_state.pending_skill_id,
            "ask_human": (
                {"call_id": ah.call_id, "question": ah.question}
                if ah is not None else None
            ),
            "last_runner_error": getattr(self.app, "last_runner_error", None),
        }

        self.app.push_screen(ViewerScreen(
            title=f"status · {title_id}",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
        ))

    def _render_command_error(self, name: str, suggestion: str | None) -> None:
        """Команда-ошибка по единому правилу (§3.3). Одна строка одним Static;
        подсказка closest-match — deep-dim в той же строке."""
        from rich.text import Text
        line = Text()
        line.append(f"[!] неизвестная команда: /{name}", style=_ERR_RED)
        if suggestion:
            line.append(f" (вы имели в виду /{suggestion}?)", style=_ERR_HINT)
        self._trace_mount(Static(line, classes="line-error", markup=False))

    def _mount_trace_row(
        self,
        *,
        prefix: str,
        content: str,
        kind_class: str,
        event_no: int = 0,
        focusable: bool = True,
    ) -> "TraceRow":
        """Смонтировать строку трассы через TraceRow (Horizontal prefix + content).

        focusable=True (дефолт) — для самостоятельных одно-строчных событий.
        focusable=False — для continuation/декорация-строк, уже фокусируемых
        главным виджетом события (например, `* note:` под submit_solution)."""
        from tui.widgets.trace_row import TraceRow
        row = TraceRow(
            prefix=prefix, content=content, kind_class=kind_class, event_no=event_no,
            focusable=focusable,
        )
        self._trace_mount(row)
        return row
