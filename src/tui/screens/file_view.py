"""FileViewerScreen — постраничный read-only просмотр текстового файла (`/view`)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static, TextArea

from tui.services.file_view import (
    DEFAULT_PAGE_LINES_FALLBACK,
    MSG_EMPTY,
    PAGE_LINES_MAX,
    PAGE_LINES_MIN,
    FileViewSettings,
    OpenErr,
    OpenOk,
    ViewPositionStore,
    ViewWindow,
    apply_pygments_highlights,
    clamp_page_lines,
    clamp_start_line,
    count_lines,
    format_range_label,
    lexer_name_for_path,
    open_for_view,
    read_window,
    textarea_language_for_lexer,
)
from tui.services.skill_view import truncate_path_left

_PATH_MAXLEN = 56
# Схлопываем key-repeat: не чаще ~10 Hz один read_window + load_text.
# Одиночный тик — сразу (leading-edge); hold на больших файлах важнее «30 fps».
_MOVE_THROTTLE_SEC = 1.0 / 10.0
_FOOTER_OK = "[Esc] назад  ·  [↑↓] строка  ·  [PgUp/PgDn] стр."
_FOOTER_ERR = "[Esc] назад"


class FileViewerScreen(Screen):
    """Просмотр файла: TextArea(read_only) — скролл, подсветка, выделение мышью.

    Ctrl+C: app._clipboard_payload → selected_text / get_selected_text
    (вся текущая страница как fallback) + суффикс ``[file: path]``.
    """

    BINDINGS = [
        Binding("escape", "close", "назад", priority=True),
        Binding("up", "line_up", "строка вверх", show=False, priority=True),
        Binding("down", "line_down", "строка вниз", show=False, priority=True),
        Binding("pageup", "page_up", "стр. вверх", show=False, priority=True),
        Binding("pagedown", "page_down", "стр. вниз", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        raw_path: str,
        cwd: str,
        store: ViewPositionStore,
        settings: Optional[FileViewSettings] = None,
    ) -> None:
        super().__init__()
        self._raw_path = raw_path or ""
        self._cwd = cwd or "."
        self._store = store
        self._settings = settings or FileViewSettings()
        self._path: Optional[Path] = None
        self._error: Optional[str] = None
        self._start_line = 1
        self._page = self._initial_page()
        self._total_lines: Optional[int] = None
        self._window: Optional[ViewWindow] = None
        self._lexer: Optional[str] = None
        self._ta_language: Optional[str] = None
        self._display_path = (raw_path or "").strip() or "view"
        self._pending_delta = 0
        self._move_timer: Optional[Timer] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("", id="header-left", markup=False)
                yield Static("", id="header-right", markup=False)
            yield TextArea(
                "",
                id="file-view-body",
                classes="viewer-body",
                read_only=True,
                soft_wrap=True,
                show_line_numbers=False,
                show_cursor=True,
                highlight_cursor_line=False,
                tab_behavior="focus",
                theme="vscode_dark",
            )
            yield Static(_FOOTER_ERR, id="footer", markup=False)

    def on_mount(self) -> None:
        self._bootstrap()
        try:
            self.query_one("#file-view-body", TextArea).focus()
        except Exception:
            pass
        if self._settings.page_lines is None:
            self.call_after_refresh(self._sync_page_from_layout)

    def on_resize(self) -> None:
        if self._settings.page_lines is not None:
            return
        self._sync_page_from_layout()

    def on_unmount(self) -> None:
        self._flush_move_queue(stop_timer=True)
        self._persist_position()

    def action_close(self) -> None:
        self._flush_move_queue(stop_timer=True)
        self._persist_position()
        self.dismiss()

    def action_line_up(self) -> None:
        self._queue_move(-1)

    def action_line_down(self) -> None:
        self._queue_move(1)

    def action_page_up(self) -> None:
        self._queue_move(-(max(1, self._page - 1)))

    def action_page_down(self) -> None:
        self._queue_move(max(1, self._page - 1))

    def _initial_page(self) -> int:
        if self._settings.page_lines is not None:
            return clamp_page_lines(self._settings.page_lines)
        return DEFAULT_PAGE_LINES_FALLBACK

    def _bootstrap(self) -> None:
        result = open_for_view(
            self._raw_path, cwd=self._cwd, settings=self._settings,
        )
        if isinstance(result, OpenErr):
            self._error = result.message
            self._display_path = result.display_path or self._display_path
            self._show_error()
            return
        assert isinstance(result, OpenOk)
        self._path = result.path
        self._display_path = str(result.path)
        self._total_lines = result.total_lines
        self._lexer = lexer_name_for_path(result.path)
        self._ta_language = textarea_language_for_lexer(self._lexer)
        self._page = self._resolve_page()
        saved = self._store.get(result.path)
        self._start_line = saved if saved is not None else 1
        if self._total_lines is not None:
            self._start_line = clamp_start_line(
                self._start_line, total_lines=self._total_lines, page=self._page,
            )
        self._reload_window()

    def _resolve_page(self) -> int:
        fixed = self._settings.page_lines
        if fixed is not None:
            return clamp_page_lines(fixed)
        return self._page_from_body()

    def _page_from_body(self) -> int:
        try:
            body = self.query_one("#file-view-body", TextArea)
            h = body.size.height
        except Exception:
            h = 0
        if h <= 0:
            return self._page if self._page > 0 else DEFAULT_PAGE_LINES_FALLBACK
        return max(PAGE_LINES_MIN, min(PAGE_LINES_MAX, h))

    def _sync_page_from_layout(self) -> None:
        if self._error is not None or self._path is None:
            return
        if self._settings.page_lines is not None:
            return
        new_page = self._page_from_body()
        if new_page == self._page:
            return
        self._page = new_page
        if self._total_lines is not None:
            self._start_line = clamp_start_line(
                self._start_line, total_lines=self._total_lines, page=self._page,
            )
        self._reload_window()

    def _queue_move(self, delta: int) -> None:
        """Leading-edge throttle: первый тик сразу, дальше не чаще _MOVE_THROTTLE_SEC."""
        if self._error is not None or self._path is None or delta == 0:
            return
        self._pending_delta += delta
        if self._move_timer is not None:
            return
        self._flush_pending_delta()
        self._move_timer = self.set_timer(
            _MOVE_THROTTLE_SEC, self._on_move_throttle_tick,
        )

    def _on_move_throttle_tick(self) -> None:
        self._move_timer = None
        if self._pending_delta:
            self._flush_pending_delta()
            self._move_timer = self.set_timer(
                _MOVE_THROTTLE_SEC, self._on_move_throttle_tick,
            )

    def _flush_pending_delta(self) -> None:
        delta = self._pending_delta
        self._pending_delta = 0
        if delta:
            self._move(delta)

    def _flush_move_queue(self, *, stop_timer: bool) -> None:
        if self._move_timer is not None:
            if stop_timer:
                self._move_timer.stop()
            self._move_timer = None
        self._flush_pending_delta()

    def _move(self, delta: int) -> None:
        if self._error is not None or self._path is None or delta == 0:
            return
        new_start = max(1, self._start_line + delta)
        if self._total_lines is not None:
            new_start = clamp_start_line(
                new_start, total_lines=self._total_lines, page=self._page,
            )
        if new_start == self._start_line and delta > 0:
            return
        window = read_window(
            self._path,
            start_line=new_start,
            line_count=self._page,
            total_lines=self._total_lines,
        )
        if not window.lines and new_start > 1:
            return
        self._start_line = new_start
        self._apply_window(window)

    def _reload_window(self) -> None:
        if self._path is None:
            return
        window = read_window(
            self._path,
            start_line=self._start_line,
            line_count=self._page,
            total_lines=self._total_lines,
        )
        if not window.lines and self._start_line > 1:
            if self._total_lines is None:
                try:
                    self._total_lines = count_lines(self._path)
                except OSError as exc:
                    self._error = str(exc) or exc.__class__.__name__
                    self._show_error()
                    return
            self._start_line = clamp_start_line(
                self._start_line, total_lines=self._total_lines, page=self._page,
            )
            window = read_window(
                self._path,
                start_line=self._start_line,
                line_count=self._page,
                total_lines=self._total_lines,
            )
        self._apply_window(window)

    def _apply_window(self, window: ViewWindow) -> None:
        self._window = window
        empty = not window.lines
        plain = MSG_EMPTY if empty else "\n".join(window.lines)
        body = self.query_one("#file-view-body", TextArea)
        # TextArea.language красит только при textual[syntax]/tree-sitter).
        # Иначе — pygments → _highlights (та же тема vscode_dark).
        try:
            from textual._tree_sitter import TREE_SITTER
        except ImportError:
            TREE_SITTER = False
        if TREE_SITTER and self._ta_language:
            body.language = self._ta_language
            body.load_text(plain)
        else:
            body.language = None
            body.load_text(plain)
            if not empty:
                apply_pygments_highlights(body, plain, lexer=self._lexer)
        self.query_one("#footer", Static).update(_FOOTER_OK)
        self._refresh_header(
            empty=empty or (self._total_lines == 0),
            start=window.start_line,
            end=window.end_line,
        )

    def _show_error(self) -> None:
        body = self.query_one("#file-view-body", TextArea)
        body.language = None
        body.load_text(self._error or "")
        apply_pygments_highlights(body, "", lexer=None)
        self.query_one("#footer", Static).update(_FOOTER_ERR)
        left = f"view · {truncate_path_left(self._display_path, _PATH_MAXLEN)}"
        if not self._display_path or self._display_path == "view":
            left = "view"
        self.query_one("#header-left", Static).update(left)
        self.query_one("#header-right", Static).update("—")

    def _refresh_header(self, *, empty: bool, start: int, end: int) -> None:
        left = f"view · {truncate_path_left(self._display_path, _PATH_MAXLEN)}"
        right = format_range_label(
            start_line=start,
            end_line=end,
            total_lines=self._total_lines,
            empty=empty,
        )
        self.query_one("#header-left", Static).update(left)
        self.query_one("#header-right", Static).update(right)

    def _persist_position(self) -> None:
        if self._error is not None or self._path is None:
            return
        self._store.set(self._path, self._start_line)

    def clipboard_source_cite(self) -> Optional[tuple[str, str]]:
        """Источник для суффикса буфера: ``("file", path)`` или None.

        path — относительно cwd TUI, если файл внутри; иначе абсолютный.
        """
        if self._error is not None:
            return None
        if self._path is None:
            return None
        try:
            cwd = Path(self._cwd).resolve()
            rel = self._path.resolve().relative_to(cwd)
            return ("file", rel.as_posix())
        except (OSError, ValueError):
            path = (self._display_path or "").strip()
            if not path or path == "view":
                return None
            return ("file", path)

    def get_selected_text(self) -> str:
        """Выделение TextArea или, если пусто, текст текущей страницы."""
        try:
            body = self.query_one("#file-view-body", TextArea)
            sel = body.selected_text
            if sel:
                return sel
        except Exception:
            pass
        if self._window and self._window.lines:
            return "\n".join(self._window.lines)
        return ""
