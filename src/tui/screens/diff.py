"""DiffScreen — список pending-диффов, approve/undo, Enter → viewer."""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from diff_tui.feedback import format_approve_ack, format_undo_ack
from diff_tui.service import DiffFileEntry, DiffService
from tui.services.json_payload import render_payload

_FOOTER = (
    "[↑↓] выбрать  [Enter] diff  [a] approve  [r] undo  "
    "[Ctrl+a] all  [Ctrl+r] undo all  [Esc] назад"
)
_VIEWER_FOOTER = (
    "[a] approve  [r] undo  [Ctrl+a] all  [Ctrl+r] undo all  "
    "[↑↓ / PgUp PgDn] прокрутка  [Esc] назад"
)

UndoFeedbackFn = Callable[[Sequence[str]], None]
ActionFn = Callable[[], None]

# ЙЦУКЕН / Caps Lock: регистр не должен менять смысл a/r.
# «all» — только через Ctrl (иначе CapsLock+ф → approve all).
_KEY_ALIASES = {
    "a": "a",
    "A": "a",
    "ф": "a",
    "Ф": "a",
    "r": "r",
    "R": "r",
    "к": "r",
    "К": "r",
    "ctrl+a": "ctrl+a",
    "ctrl+A": "ctrl+a",
    "ctrl+ф": "ctrl+a",
    "ctrl+Ф": "ctrl+a",
    "ctrl+r": "ctrl+r",
    "ctrl+R": "ctrl+r",
    "ctrl+к": "ctrl+r",
    "ctrl+К": "ctrl+r",
}


def _norm_key(key: str) -> str:
    return _KEY_ALIASES.get(key, key)


def _service_from_config(launch_root: str, config: dict) -> DiffService:
    wg = config.get("write_guard") or {}
    dt = config.get("diff_tui") or {}
    protected = config.get("protected_paths") or {}
    return DiffService(
        launch_root,
        extra_paths=list(wg.get("extra_paths") or []),
        protected_extra=list(protected.get("extra") or []),
        cache_max_orig=int(dt.get("cache_max_orig", 500)),
        preview_context_lines=int(dt.get("preview_context_lines", 3)),
        max_diff_chars=int(dt.get("max_diff_chars", 8000)),
        emit_notices=True,
    )


class DiffViewerScreen(Screen):
    """Просмотр pending-диффа одного файла: a/r/Ctrl+a/r работают здесь же."""

    def __init__(
        self,
        *,
        title: str,
        content: Any,
        on_approve: ActionFn,
        on_undo: ActionFn,
        on_approve_all: ActionFn,
        on_undo_all: ActionFn,
    ) -> None:
        super().__init__()
        self._title = title
        self._content = content
        self._on_approve = on_approve
        self._on_undo = on_undo
        self._on_approve_all = on_approve_all
        self._on_undo_all = on_undo_all

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(self._title, id="header-left", markup=False)
            with VerticalScroll(id="trace"):
                body = render_payload(self._content)
                body.add_class("viewer-body")
                yield body
            yield Static(_VIEWER_FOOTER, id="footer", markup=False)

    def on_key(self, event: events.Key) -> None:
        key = _norm_key(event.key)
        if key == "a":
            self._on_approve()
            event.stop()
        elif key == "r":
            self._on_undo()
            event.stop()
        elif key == "ctrl+a":
            self._on_approve_all()
            event.stop()
        elif key == "ctrl+r":
            self._on_undo_all()
            event.stop()
        elif key == "escape":
            event.stop()
            event.prevent_default()
            self.app.pop_screen()


class DiffScreen(Screen):
    """Review pending правок агента: list → approve/undo / viewer."""

    def __init__(
        self,
        *,
        launch_root: str,
        config: Optional[dict] = None,
        service: Optional[DiffService] = None,
        on_undo_feedback: Optional[UndoFeedbackFn] = None,
    ) -> None:
        super().__init__()
        self._config = config or {}
        self._svc = service or _service_from_config(launch_root, self._config)
        self._on_undo_feedback = on_undo_feedback
        self._entries: list[DiffFileEntry] = []
        self._selected_idx = -1
        self._status: str = ""

    @property
    def service(self) -> DiffService:
        return self._svc

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("diff", id="header-left", markup=False)
            yield VerticalScroll(id="trace")
            yield Static("", id="diff-status", markup=False)
            yield Static(_FOOTER, id="footer", markup=False)

    def on_mount(self) -> None:
        self._svc.ensure_mode_ready()
        self._reload()

    def _set_status(self, text: str) -> None:
        self._status = text or ""
        try:
            self.query_one("#diff-status", Static).update(self._status)
        except Exception:
            pass

    def _reload(self) -> None:
        self._entries = self._svc.list_entries(pending_only=True)
        if self._entries:
            if self._selected_idx < 0 or self._selected_idx >= len(self._entries):
                self._selected_idx = 0
        else:
            self._selected_idx = -1
        self._render_list()

    def _header_text(self) -> str:
        n = len(self._entries)
        mode = self._svc.mode
        return f"diff · {n} pending · {mode}"

    def _row_label(self, entry: DiffFileEntry, *, selected: bool) -> str:
        cursor = ">" if selected else " "
        return f"{cursor} {entry.mark} {entry.key}  {entry.state}"

    def _render_list(self) -> None:
        self.query_one("#header-left", Static).update(self._header_text())
        try:
            self.query_one("#diff-status", Static).update(self._status)
        except Exception:
            pass
        trace = self.query_one("#trace", VerticalScroll)
        trace.remove_children()
        if not self._entries:
            trace.mount(
                Static("Нет pending-изменений", classes="line-meta", markup=False)
            )
            return
        selected_row: Optional[Static] = None
        for idx, entry in enumerate(self._entries):
            selected = idx == self._selected_idx
            suffix = " selected" if selected else ""
            row = Static(
                self._row_label(entry, selected=selected),
                classes="list-row" + suffix,
                markup=False,
            )
            if selected:
                selected_row = row
            trace.mount(row)
        if selected_row is not None:
            self.call_after_refresh(selected_row.scroll_visible, animate=False)

    def on_key(self, event: events.Key) -> None:
        key = _norm_key(event.key)
        if key == "up":
            if self._entries:
                self._selected_idx = (self._selected_idx - 1) % len(self._entries)
                self._render_list()
            event.stop()
        elif key == "down":
            if self._entries:
                self._selected_idx = (self._selected_idx + 1) % len(self._entries)
                self._render_list()
            event.stop()
        elif key in ("home", "end"):
            if self._entries:
                self._selected_idx = 0 if key == "home" else len(self._entries) - 1
                self._render_list()
            event.stop()
        elif key == "enter":
            self._open_viewer()
            event.stop()
        elif key == "a":
            self._approve_selected()
            event.stop()
        elif key == "r":
            self._undo_selected()
            event.stop()
        elif key == "ctrl+a":
            self._approve_all()
            event.stop()
        elif key == "ctrl+r":
            self._undo_all()
            event.stop()
        elif key == "escape":
            event.stop()
            event.prevent_default()
            self.app.pop_screen()

    def _selected(self) -> Optional[DiffFileEntry]:
        if not self._entries or not (0 <= self._selected_idx < len(self._entries)):
            return None
        return self._entries[self._selected_idx]

    def _emit_undo_feedback(self, undone_keys: Sequence[str]) -> None:
        ack = format_undo_ack(undone_keys)
        if ack:
            self._set_status(ack)
        if undone_keys and self._on_undo_feedback is not None:
            self._on_undo_feedback(undone_keys)

    def _approve_selected(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        key = entry.key
        self._svc.approve(key)
        ack = format_approve_ack(1, key)
        if ack:
            self._set_status(ack)
        self._reload()

    def _undo_selected(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        result = self._svc.undo(entry.key)
        self._emit_undo_feedback(result.undone_keys)
        self._reload()

    def _approve_all(self) -> None:
        n = len(self._entries)
        self._svc.approve_all()
        ack = format_approve_ack(n)
        if ack:
            self._set_status(ack)
        self._reload()

    def _undo_all(self) -> None:
        result = self._svc.undo_all()
        self._emit_undo_feedback(result.undone_keys)
        self._reload()

    def _run_from_viewer(self, fn: ActionFn) -> None:
        """Approve/undo из viewer: выполнить, закрыть viewer, обновить список."""
        fn()
        if isinstance(self.app.screen, DiffViewerScreen):
            self.app.pop_screen()
        self._reload()

    def _open_viewer(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        key = entry.key
        body = self._svc.visual_diff(key)
        if not body.strip():
            body = "(нет pending-диффа)"

        self.app.push_screen(
            DiffViewerScreen(
                title=f"diff · {entry.mark} {entry.key}",
                content=body,
                on_approve=lambda: self._run_from_viewer(
                    lambda: self._approve_key(key)
                ),
                on_undo=lambda: self._run_from_viewer(
                    lambda: self._emit_undo_feedback(self._svc.undo(key).undone_keys)
                ),
                on_approve_all=lambda: self._run_from_viewer(self._approve_all),
                on_undo_all=lambda: self._run_from_viewer(self._undo_all),
            )
        )

    def _approve_key(self, key: str) -> None:
        self._svc.approve(key)
        ack = format_approve_ack(1, key)
        if ack:
            self._set_status(ack)
