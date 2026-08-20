"""SessionsScreen — picker сессий текущего проекта (Ctrl+R / /sessions)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from tui.palette import (
    BRIGHT, CODE, DEEP_DIM, DIM, ERR, INK, MUTED, OK, RUN_COLOR,
)
from tui.services.git_info import current_branch
from tui.services.notices_ui import format_notices_badge
from tui.services.relative_time import format_relative_local
from tui.services.session_lock import BUSY_MESSAGE, is_held_by_other
from tui.services.session_title import display_name
from tui.services.skill_view import truncate_path_left
from tui.services.stores.file_store import FileTaskStore
from tui.services.usage_meter import format_nonzero_cost

_PAGE_SIZE = 25
_PATH_MAXLEN = 48

_FOOTER = ("↑↓/Home/End · PgUp/PgDn страница · Enter открыть · Ctrl+P превью"
           " · Ctrl+R переименовать · Ctrl+B ветка · Del удалить · Ctrl+D массовое · Esc")
_FOOTER_NOGIT = ("↑↓/Home/End · PgUp/PgDn страница · Enter открыть · Ctrl+P превью"
                 " · Ctrl+R переименовать · Del удалить · Ctrl+D массовое · Esc")

_STATUS_COLOR = {
    "running": RUN_COLOR, "asking": RUN_COLOR,
    "stopped": DIM, "completed": OK, "failed": ERR,
}


def _short_id(thread_id: str) -> str:
    return (thread_id or "")[:8]


class SessionsScreen(Screen):
    """Picker сессий текущего проекта."""

    DEFAULT_CSS = ""

    # Input: ctrl+a = home. Нужен select-all в фильтре (ЙЦУКЕН / Caps).
    BINDINGS = [
        Binding("ctrl+a", "filter_select_all", "выделить всё", show=False, priority=True),
        Binding("ctrl+ф", "filter_select_all", "выделить всё", show=False, priority=True),
        Binding("ctrl+A", "filter_select_all", "выделить всё", show=False, priority=True),
        Binding("ctrl+Ф", "filter_select_all", "выделить всё", show=False, priority=True),
    ]

    def __init__(self, *, threads_dir: str, system: str = "cli", **kwargs) -> None:
        super().__init__(**kwargs)
        self._threads_dir = threads_dir
        self._system = system
        self._store = FileTaskStore(threads_dir=threads_dir, system=system)
        self._tasks: list = []          # отфильтрованные
        self._selected_idx: int = 0     # индекс внутри текущей страницы
        self._filter: str = ""
        self._page: int = 0
        self._branch_filter: str = "all"   # "all" | "current"
        self._current_branch: Optional[str] = None

    # ---- compose / mount ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="sessions-root"):
            with Horizontal(id="sessions-header"):
                yield Static("", id="sessions-header-left", markup=False)
                yield Static("", id="sessions-header-right", markup=False)
            yield VerticalScroll(id="sessions-list")
            with Horizontal(id="sessions-filter-row"):
                yield Static("> ", id="sessions-filter-prompt", markup=False)
                yield Input(placeholder="поиск (id или текст)", id="sessions-filter")
            with Horizontal(id="sessions-footer"):
                yield Static("", id="sessions-footer-left", markup=False)
                yield Static("", id="sessions-footer-right", markup=False)

    def on_mount(self) -> None:
        self._current_branch = current_branch(str(Path.cwd()))
        # Ctrl+B осмыслен только в git-проекте — иначе не показываем эту подсказку.
        footer = _FOOTER if self._current_branch else _FOOTER_NOGIT
        self.query_one("#sessions-footer-left", Static).update(footer)
        self._reload()
        self._refresh_notices_footer()
        self.query_one("#sessions-filter", Input).focus()

    def _refresh_notices_footer(self) -> None:
        """Счётчик уведомлений в footer-right (только unread runtime)."""
        collect_unread = getattr(self.app, "collect_unread_notices", None)
        collect_all = getattr(self.app, "collect_notices", None)
        collect = collect_unread if callable(collect_unread) else collect_all
        badge = format_notices_badge(collect()) if callable(collect) else ""
        try:
            self.query_one("#sessions-footer-right", Static).update(badge)
        except Exception:
            pass

    def on_screen_resume(self) -> None:
        self._refresh_notices_footer()

    # ---- data ---------------------------------------------------------

    def _all_tasks(self) -> list:
        # Кап высокий (не 200) — чтобы счётчик «из N» показывал реальное число
        # сессий, а не вводил в заблуждение. Дефолтный вид читает только мету (дёшево)
        # + first-prompt лишь для 25 строк страницы; фильтр-по-названию ограничен этим
        # же капом. 10000 — как в mass-delete ниже.
        return self._store.list_tasks(limit=10000)

    def _visible_tasks(self) -> list:
        f = self._filter.lower()
        out: list = []
        for t in self._all_tasks():
            if self._branch_filter == "current" and self._current_branch is not None:
                if (t.tui_meta or {}).get("branch") != self._current_branch:
                    continue
            if f and f not in (t.thread_id or "").lower() and f not in self._title_of(t).lower():
                continue
            out.append(t)
        return out

    def _title_of(self, task) -> str:
        # R7: единый источник имён — display_name(meta.tui): title > last_actual
        # > «Без названия». Сканирование логов архивов/live удалено (бэкфил
        # last_actual вне scope спеки §4).
        return display_name(task.tui_meta or {})

    def _page_items(self) -> list:
        start = self._page * _PAGE_SIZE
        return self._tasks[start:start + _PAGE_SIZE]

    def _page_count(self) -> int:
        return max(1, (len(self._tasks) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _reload(self) -> None:
        self._tasks = self._visible_tasks()
        self._page = max(0, min(self._page, self._page_count() - 1))
        self._selected_idx = 0 if self._page_items() else -1
        self._render_list()
        self._refresh_header()

    # ---- render --------------------------------------------------------

    def _project_path(self) -> str:
        return str(Path.cwd())

    def _refresh_header(self) -> None:
        total = len(self._tasks)
        if total:
            start = self._page * _PAGE_SIZE + 1
            end = min(self._page * _PAGE_SIZE + _PAGE_SIZE, total)
            left = f"сессии · {start}–{end} из {total}"
        else:
            left = "сессии · 0"
        self.query_one("#sessions-header-left", Static).update(left)
        self.query_one("#sessions-header-right", Static).update(
            truncate_path_left(self._project_path(), _PATH_MAXLEN))

    def _task_display_status(self, task) -> Optional[str]:
        """Статус для карточки: live-состояние активной сессии важнее meta.tui.status.

        Как TraceScreen._session_status_word — иначе после повторного старта в том же
        треде meta ещё «completed», хотя runner уже работает."""
        tui_state = getattr(self.app, "tui_state", None)
        active_tid = getattr(tui_state, "active_task_id", None) if tui_state else None
        if not active_tid or task.thread_id != active_tid:
            return task.status
        if tui_state.ask_human_state is not None:
            return "asking"
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return "running"
        return task.status

    def _title_line(self, task, *, selected: bool) -> Text:
        title = self._title_of(task)
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(title, style=BRIGHT if selected else INK)
        return t

    def _session_cost_label(self, task) -> Optional[str]:
        """Ненулевая стоимость сессии из meta.token_usage, или None."""
        usage = getattr(task, "token_usage", None)
        if not usage:
            return None
        cfg = getattr(self.app, "_config", None) or {}
        return format_nonzero_cost(usage, cfg.get("pricing"), cfg.get("model"),
                                   cfg.get("completions_url"))

    def _session_is_busy(self, thread_id: str) -> bool:
        """Чужой свежий lock → сессия занята другим TUI."""
        tui_id = getattr(self.app, "tui_id", "") or ""
        if not tui_id:
            return False
        return is_held_by_other(self._threads_dir, self._system, thread_id, tui_id)

    def _meta_line(self, task) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(format_relative_local(task.updated_at), style=DIM)
        branch = (task.tui_meta or {}).get("branch")
        if isinstance(branch, str) and branch:
            t.append(" · ", style=MUTED)
            t.append(branch, style=CODE)
        t.append(" · ", style=MUTED)
        t.append(f"{task.messages_count} сообщ.", style=DIM)
        cost = self._session_cost_label(task)
        if cost:
            t.append(" · ", style=MUTED)
            t.append(cost, style=DIM)
        status = self._task_display_status(task)
        if status:   # None — исход не записан → бейдж не показываем (без пустых «[]»)
            t.append(" · ", style=MUTED)
            t.append(f"[{status}]", style=_STATUS_COLOR.get(status, DEEP_DIM))
        if self._session_is_busy(task.thread_id):
            t.append(" · ", style=MUTED)
            t.append("🔒 занято", style=ERR)
        t.append(" · ", style=MUTED)
        t.append(_short_id(task.thread_id), style=MUTED)
        # Путь проекта сессии (абсолютный, обрезанный слева) — в самом конце,
        # после short_id. Старые сессии без project_path → не показываем.
        ppath = (task.tui_meta or {}).get("project_path")
        if isinstance(ppath, str) and ppath.strip():
            t.append(" · ", style=MUTED)
            t.append(truncate_path_left(ppath, _PATH_MAXLEN), style=DEEP_DIM)
        return t

    def _card_text(self, task, *, selected: bool) -> Text:
        """Карточка сессии одним 2-строчным виджетом: заголовок + мета."""
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append_text(self._title_line(task, selected=selected))
        t.append("\n")
        t.append_text(self._meta_line(task))
        return t

    def _render_list(self) -> None:
        listview = self.query_one("#sessions-list", VerticalScroll)
        listview.remove_children()
        page = self._page_items()
        if not page:
            listview.mount(Static("(нет сессий)", classes="line-meta", markup=False))
            return
        selected_card: Optional[Static] = None
        for idx, task in enumerate(page):
            suffix = " selected" if idx == self._selected_idx else ""
            card = Static(self._card_text(task, selected=bool(suffix)),
                          classes="card" + suffix, markup=False)
            if suffix:
                selected_card = card
            listview.mount(card)
        if selected_card is not None:
            # Элемент — ОДИН виджет (2 строки): scroll_visible тащит его целиком в
            # обе стороны кругового перехода (как карточка MCP).
            self.call_after_refresh(selected_card.scroll_visible, animate=False)

    def _change_page(self, delta: int) -> None:
        new_page = max(0, min(self._page + delta, self._page_count() - 1))
        if new_page == self._page:
            return
        self._page = new_page
        self._selected_idx = 0
        self._render_list()
        self._refresh_header()

    # ---- input / keys --------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "sessions-filter":
            return
        new = event.value.strip()
        if new == self._filter:
            return  # эффективный фильтр не изменился (напр. одиночный/ведущий пробел)
        self._filter = new
        self._page = 0
        self._reload()

    def action_filter_select_all(self) -> None:
        """Ctrl+A в поле фильтра: выделить весь текст (не approve диффов)."""
        try:
            inp = self.query_one("#sessions-filter", Input)
        except Exception:
            return
        if not inp.has_focus:
            return
        inp.action_select_all()

    # R10: алиасы Ctrl+R для переименования выбранной сессии (паритет с
    # TraceScreen/diff). ctrl+к / ctrl+К — русская раскладка (физ. кнопка «R»).
    # Локальный маппинг (не тащим diff-модуль).
    _RENAME_KEYS = frozenset({"ctrl+r", "ctrl+R", "ctrl+к", "ctrl+К"})

    def on_key(self, event: events.Key) -> None:
        # R10: Ctrl+R и алиасы раскладки → переименование (до навигации).
        if event.key in self._RENAME_KEYS:
            self._rename_selected()
            event.stop()
            return
        page_len = len(self._page_items())
        if event.key == "up":
            if page_len:   # круговой в пределах страницы: с первой ↑ → на последнюю
                self._selected_idx = (self._selected_idx - 1) % page_len
                self._render_list()
            event.stop()
        elif event.key == "down":
            if page_len:   # круговой: с последней ↓ → на первую
                self._selected_idx = (self._selected_idx + 1) % page_len
                self._render_list()
            event.stop()
        elif event.key in ("home", "end"):
            if page_len:
                self._selected_idx = 0 if event.key == "home" else page_len - 1
                self._render_list()
            event.stop()
        elif event.key in ("pageup", "pagedown"):
            self._change_page(-1 if event.key == "pageup" else 1)
            event.stop()
        elif event.key == "enter":
            task = self._selected_task()
            if task is not None:
                self._resume_thread(task.thread_id)
            event.stop()
        elif event.key == "ctrl+p":
            self._preview_selected()
            event.stop()
        elif event.key == "ctrl+b":
            self._toggle_branch_filter()
            event.stop()
        elif event.key == "delete":
            self._delete_selected()
            event.stop()
        elif event.key == "ctrl+d":
            self._open_mass_delete()
            event.stop()
        elif event.key == "escape":
            self.app.pop_screen()
            event.stop()

    def _selected_task(self):
        page = self._page_items()
        if 0 <= self._selected_idx < len(page):
            return page[self._selected_idx]
        return None

    def _toggle_branch_filter(self) -> None:
        if self._current_branch is None:
            return  # не-git → no-op
        self._branch_filter = "current" if self._branch_filter == "all" else "all"
        self._page = 0
        self._reload()

    # ---- actions -------------------------------------------------------

    def _resume_thread(self, thread_id: str) -> None:
        if self._session_is_busy(thread_id):
            self.app.notify(BUSY_MESSAGE, severity="warning")
            return
        self.app.pop_screen()
        from tui.screens.main import TraceScreen
        scr = self.app.screen
        if isinstance(scr, TraceScreen):
            # acquire(B) → release(A) → attach внутри attach_active_task
            if not scr.attach_active_task(thread_id, is_resume=True):
                return
        else:
            self.app.tui_state.active_task_id = thread_id

    def _delete_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        short = _short_id(task.thread_id)
        from tui.screens.confirm import ConfirmScreen

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._delete_task(task.thread_id)

        self.app.push_screen(
            ConfirmScreen(title="Удаление",
                          message=f"Удалить сессию {short}?\nЭто действие необратимо."),
            _on_result,
        )

    def _delete_task(self, thread_id: str) -> None:
        self._store.delete_task(thread_id)
        self._reload()

    def _open_mass_delete(self) -> None:
        tasks = self._store.list_tasks(limit=10000)  # весь проект, не страница
        if not tasks:
            return
        now = datetime.now().astimezone()
        from tui.screens.mass_delete import MassDeleteScreen

        def _on_criterion(criterion: Optional[dict]) -> None:
            if criterion:
                self._confirm_mass_delete(tasks, criterion, now)

        self.app.push_screen(MassDeleteScreen(tasks=tasks, now=now), _on_criterion)

    def _confirm_mass_delete(self, tasks: list, criterion: dict, now) -> None:
        from tui.services.session_delete_select import select_for_deletion
        ids = select_for_deletion(tasks, now=now, **criterion)
        if not ids:
            return
        total = len(tasks)
        from tui.screens.confirm import ConfirmScreen

        def _on_confirm(ok: bool) -> None:
            if not ok:
                return
            for tid in ids:
                self._store.delete_task(tid)
            self._reload()

        self.app.push_screen(
            ConfirmScreen(title="Массовое удаление",
                          message=f"Удалить {len(ids)} из {total} сессий?\n"
                                  "Это действие необратимо."),
            _on_confirm,
        )

    def _rename_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        tid = task.thread_id
        from tui.screens.rename import RenameScreen

        def _on_result(value: Optional[str]) -> None:
            if value is None:
                return
            self._apply_rename(tid, value)

        self.app.push_screen(RenameScreen(current=self._title_of(task)), _on_result)

    def _apply_rename(self, thread_id: str, value: str) -> None:
        # пустое значение → откат к авто-имени: title="" сброшен, display_name
        # отдаёт last_actual (R7), а при его отсутствии — «Без названия».
        self._store.update_tui_meta(thread_id, {"title": value})
        self._reload()

    def _preview_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        tid = task.thread_id
        segments = self._store.read_segments(tid)
        subagent_records = self._store.read_subagent_trace(tid)
        from tui.screens.preview import PreviewScreen

        def _on_result(resume: bool) -> None:
            if resume:
                self._resume_thread(tid)

        self.app.push_screen(
            PreviewScreen(segments=segments, thread_id=tid,
                          project_path=self._project_path(),
                          subagent_records=subagent_records),
            _on_result,
        )
