"""SessionsScreen — picker сессий текущего проекта (Ctrl+R / /sessions)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from message_policy.user_message_policy import extract_real_user_message_text
from tui.palette import (
    BRIGHT, CODE, DEEP_DIM, DIM, ERR, INK, MUTED, OK, RUN_COLOR,
)
from tui.services.git_info import current_branch
from tui.services.relative_time import format_relative_local
from tui.services.skill_view import truncate_path_left
from tui.services.stores.file_store import FileTaskStore

_PAGE_SIZE = 25
_PATH_MAXLEN = 48

_FOOTER = ("↑↓/Home/End · PgUp/PgDn страница · Enter открыть · Ctrl+P превью"
           " · Ctrl+R переименовать · Ctrl+B ветка · Del удалить · Ctrl+D массовое · Esc")
_FOOTER_NOGIT = ("↑↓/Home/End · PgUp/PgDn страница · Enter открыть · Ctrl+P превью"
                 " · Ctrl+R переименовать · Del удалить · Ctrl+D массовое · Esc")

_STATUS_COLOR = {
    "running": RUN_COLOR, "stopped": DIM, "completed": OK, "failed": ERR,
}


def _short_id(thread_id: str) -> str:
    return (thread_id or "")[:8]


class SessionsScreen(Screen):
    """Picker сессий текущего проекта."""

    DEFAULT_CSS = ""

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
        self._prompt_cache: dict[str, str] = {}  # thread_id → первый промпт (immutable)

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

    def on_mount(self) -> None:
        self._current_branch = current_branch(str(Path.cwd()))
        # Ctrl+B осмыслен только в git-проекте — иначе не показываем эту подсказку.
        footer = _FOOTER if self._current_branch else _FOOTER_NOGIT
        self.query_one("#sessions-footer-left", Static).update(footer)
        self._reload()
        self.query_one("#sessions-filter", Input).focus()

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
        custom = (task.tui_meta or {}).get("title")
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
        return self._first_prompt(task.thread_id)

    def _first_prompt(self, thread_id: str) -> str:
        # Кэш: первый промпт неизменен; иначе файл читался бы на каждый кадр.
        if thread_id in self._prompt_cache:
            return self._prompt_cache[thread_id]
        base = Path(self._threads_dir)
        # Исходный промпт может жить в самом старом __pre_summary__ архиве:
        # после суммаризации live-файл начинается с dialog_summary. Идём по
        # файлам в хронологии (архивы по суффиксу → live) и берём первое
        # РЕАЛЬНОЕ user-сообщение, пропуская технические (dialog_summary и пр.).
        archives = sorted(
            base.glob(f"data_{self._system}_{thread_id}__pre_summary__*.jsonl")
        )
        files = [*archives, base / f"data_{self._system}_{thread_id}.jsonl"]
        result = ""
        for path in files:
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        rec = json.loads(line)
                        text = extract_real_user_message_text(rec.get("payload", {}))
                        if text:
                            result = " ".join(text.split())
                            break
            except (OSError, json.JSONDecodeError):
                continue
            if result:
                break
        self._prompt_cache[thread_id] = result
        return result

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

    def _title_line(self, task, *, selected: bool) -> Text:
        title = self._title_of(task) or "(без заголовка)"
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(title, style=BRIGHT if selected else INK)
        return t

    def _meta_line(self, task) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(format_relative_local(task.updated_at), style=DIM)
        branch = (task.tui_meta or {}).get("branch")
        if isinstance(branch, str) and branch:
            t.append(" · ", style=MUTED)
            t.append(branch, style=CODE)
        t.append(" · ", style=MUTED)
        t.append(f"{task.messages_count} сообщ.", style=DIM)
        if task.status:   # None — исход не записан → бейдж не показываем (без пустых «[]»)
            t.append(" · ", style=MUTED)
            t.append(f"[{task.status}]", style=_STATUS_COLOR.get(task.status, DEEP_DIM))
        t.append(" · ", style=MUTED)
        t.append(_short_id(task.thread_id), style=MUTED)
        return t

    def _render_list(self) -> None:
        listview = self.query_one("#sessions-list", VerticalScroll)
        listview.remove_children()
        page = self._page_items()
        if not page:
            listview.mount(Static("(нет сессий)", classes="line-meta", markup=False))
            return
        selected_widget: Optional[Static] = None
        for idx, task in enumerate(page):
            suffix = " selected" if idx == self._selected_idx else ""
            title_w = Static(self._title_line(task, selected=bool(suffix)),
                             classes="card-title" + suffix, markup=False)
            meta_w = Static(self._meta_line(task),
                            classes="card-meta" + suffix, markup=False)
            if suffix:
                selected_widget = title_w
            listview.mount(title_w)
            listview.mount(meta_w)
        if selected_widget is not None:
            self.call_after_refresh(selected_widget.scroll_visible, animate=False)

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

    def on_key(self, event: events.Key) -> None:
        page_len = len(self._page_items())
        if event.key == "up":
            if page_len and self._selected_idx > 0:
                self._selected_idx -= 1
                self._render_list()
            event.stop()
        elif event.key == "down":
            if page_len and self._selected_idx < page_len - 1:
                self._selected_idx += 1
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
        elif event.key == "ctrl+r":
            self._rename_selected()
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
        self.app.tui_state.active_task_id = thread_id
        self.app.pop_screen()
        from tui.screens.main import TraceScreen
        scr = self.app.screen
        if isinstance(scr, TraceScreen):
            scr.attach_active_task(thread_id, is_resume=True)   # промпт заголовка — из стора

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
        from datetime import datetime
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
        # пустое значение → откат к промпту (title="" falsy в _title_of)
        self._store.update_tui_meta(thread_id, {"title": value})
        self._reload()

    def _preview_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        tid = task.thread_id
        segments = self._store.read_segments(tid)
        from tui.screens.preview import PreviewScreen

        def _on_result(resume: bool) -> None:
            if resume:
                self._resume_thread(tid)

        self.app.push_screen(
            PreviewScreen(segments=segments, thread_id=tid,
                          project_path=self._project_path()),
            _on_result,
        )
