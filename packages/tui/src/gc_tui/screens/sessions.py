"""SessionsScreen — picker сессий текущего проекта (Ctrl+R / /sessions)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from gc_core.threads.errors import OwnershipError
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from gc_tui.palette import (
    BRIGHT,
    CODE,
    DEEP_DIM,
    DIM,
    ERR,
    INK,
    MUTED,
    OK,
    RUN_COLOR,
)
from gc_tui.screens.main import trace_screen_of
from gc_tui.services.git_info import current_branch
from gc_tui.services.history_loader import (
    HistoryLoader,
    LoadedHistory,
    history_inputs_for,
)
from gc_tui.services.history_model import HistoryInputs
from gc_tui.services.notices_ui import format_notices_badge
from gc_tui.services.relative_time import format_relative_local
from gc_tui.services.session_lock import BUSY_MESSAGE, is_held_by_other
from gc_tui.services.session_title import display_name, task_subtitle
from gc_tui.services.skill_view import truncate_path_left
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.trace_glyphs import PROMPT
from gc_tui.services.turn_outcome import outcome_of, settled_status
from gc_tui.services.usage_meter import format_nonzero_cost
from gc_tui.widgets.open_indicator import OpenIndicator

# Константы вместо повторяющихся литералов (Sonar S1192).
_ACT_SELECT_ALL = "выделить всё"


_PAGE_SIZE = 25
_PATH_MAXLEN = 48

# Что открывается из списка (§3.3): сессия (Enter) или превью (Ctrl+P).
_SESSION = "session"
_PREVIEW = "preview"
_OPEN_LABELS = {_SESSION: "открываю сессию", _PREVIEW: "открываю превью"}

_FOOTER = (
    "↑↓/Home/End · PgUp/PgDn страница · Enter открыть · Ctrl+P превью"
    " · Ctrl+R переименовать · Ctrl+B ветка · Del удалить · Ctrl+D массовое · Esc"
)
_FOOTER_NOGIT = (
    "↑↓/Home/End · PgUp/PgDn страница · Enter открыть · Ctrl+P превью"
    " · Ctrl+R переименовать · Del удалить · Ctrl+D массовое · Esc"
)

_STATUS_COLOR = {
    "running": RUN_COLOR,
    "asking": RUN_COLOR,
    "stopped": DIM,
    "cancelled": DIM,
    "interrupted": DIM,
    "completed": OK,
    "failed": ERR,
    "unknown_failed": ERR,
}


def _status_badge(status: str) -> Text:
    """`✓ выполнено` словарём итогов хода; незнакомое слово — как есть, DIM."""
    outcome = outcome_of(status)
    if outcome is None or outcome.list_label is None:
        return Text(status, style=DIM)
    return Text(
        f"{outcome.glyph} {outcome.list_label}",
        style=_STATUS_COLOR.get(status, DIM),
    )


def _short_id(thread_id: str) -> str:
    return (thread_id or "")[:8]


class SessionsScreen(Screen):
    """Picker сессий текущего проекта."""

    DEFAULT_CSS = ""

    # Input: ctrl+a = home. Нужен select-all в фильтре (ЙЦУКЕН / Caps).
    BINDINGS = [
        Binding(
            "ctrl+a",
            "filter_select_all",
            _ACT_SELECT_ALL,
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+ф",
            "filter_select_all",
            _ACT_SELECT_ALL,
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+A",
            "filter_select_all",
            _ACT_SELECT_ALL,
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+Ф",
            "filter_select_all",
            _ACT_SELECT_ALL,
            show=False,
            priority=True,
        ),
    ]

    def __init__(
        self, *, threads_dir: str, system: str = "cli", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._threads_dir = threads_dir
        self._system = system
        self._store = FileTaskStore(threads_dir=threads_dir, system=system)
        self._tasks: list = []  # отфильтрованные
        self._selected_idx: int = 0  # индекс внутри текущей страницы
        self._filter: str = ""
        self._page: int = 0
        self._branch_filter: str = "all"  # "all" | "current"
        self._current_branch: Optional[str] = None
        # Открытие из списка (§3.3): (тред, что открываем) и индикатор у
        # его карточки. Загрузчик — на экране: мост в UI — приложение.
        self._loader: Optional[HistoryLoader] = None
        self._opening: Optional[tuple[str, str]] = None
        self._indicator: Optional[OpenIndicator] = None

    # ---- compose / mount ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="sessions-root"):
            with Horizontal(id="sessions-header"):
                yield Static("", id="sessions-header-left", markup=False)
                yield Static("", id="sessions-header-right", markup=False)
            yield VerticalScroll(id="sessions-list")
            with Horizontal(id="sessions-filter-row"):
                yield Static(PROMPT, id="sessions-filter-prompt", markup=False)
                yield Input(
                    placeholder="поиск (id или текст)", id="sessions-filter"
                )
            with Horizontal(id="sessions-footer"):
                yield Static("", id="sessions-footer-left", markup=False)
                yield Static("", id="sessions-footer-right", markup=False)

    def on_mount(self) -> None:
        self._loader = HistoryLoader(self._store, self.app.call_later)
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

    def on_unmount(self) -> None:
        self._cancel_open()

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
            if (
                self._branch_filter == "current"
                and self._current_branch is not None
                and (t.tui_meta or {}).get("branch") != self._current_branch
            ):
                continue
            if (
                f
                and f not in (t.thread_id or "").lower()
                and f not in self._title_of(t).lower()
                and f not in task_subtitle(t.tui_meta or {}).lower()
            ):
                continue
            out.append(t)
        return out

    def _title_of(self, task) -> str:
        # R7: единый источник имён — display_name(meta.tui): title > task >
        # last_actual > «Без названия». Сканирование логов архивов/live удалено.
        return display_name(task.tui_meta or {})

    def _page_items(self) -> list:
        start = self._page * _PAGE_SIZE
        return self._tasks[start : start + _PAGE_SIZE]

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
            truncate_path_left(self._project_path(), _PATH_MAXLEN)
        )

    def _task_display_status(self, task) -> Optional[str]:
        """Статус для карточки: live-состояние активной сессии важнее meta.tui.status.

        Как TraceScreen._session_status_word — иначе после повторного старта в том же
        треде meta ещё «completed», хотя runner уже работает."""
        tui_state = getattr(self.app, "tui_state", None)
        active_tid = (
            getattr(tui_state, "active_task_id", None) if tui_state else None
        )
        if active_tid and task.thread_id == active_tid:
            if tui_state.ask_human_state is not None:
                return "asking"
            runner = getattr(self.app, "active_runner", None)
            if runner is not None and runner.is_running():
                return "running"
        # Правило «нет живого хода — прервано» — только для статуса TUI:
        # его ход держит процесс с замком сессии. Верхний статус — как есть.
        if (task.tui_meta or {}).get("status") != "running":
            return task.status
        return settled_status(
            task.status, live=self.app.session_run_is_live(task.thread_id)
        )

    def _title_line(
        self, task, *, selected: bool, opening: Optional[Text] = None
    ) -> Text:
        title = self._title_of(task)
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(title, style=BRIGHT if selected else INK)
        if opening is not None:
            t.append(" ")
            t.append_text(opening)
        return t

    def _session_cost_label(self, task) -> Optional[str]:
        """Ненулевая стоимость сессии из meta.token_usage, или None."""
        usage = getattr(task, "token_usage", None)
        if not usage:
            return None
        settings = self.app.base_settings().settings
        return format_nonzero_cost(
            usage,
            settings.pricing,
            settings.agent.model,
            settings.backend.completions_url,
        )

    def _session_is_busy(self, thread_id: str) -> bool:
        """Чужой свежий lock → сессия занята другим TUI."""
        tui_id = getattr(self.app, "tui_id", "") or ""
        if not tui_id:
            return False
        return is_held_by_other(
            self._threads_dir, self._system, thread_id, tui_id
        )

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
        if status:  # None — исход не записан → статус не показываем
            t.append(" · ", style=MUTED)
            t.append_text(_status_badge(status))
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

    def _card_text(
        self, task, *, selected: bool, opening: Optional[Text] = None
    ) -> Text:
        """Карточка сессии: заголовок (у открываемой — индикатор после
        него) + опциональный подзаголовок + мета."""
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append_text(
            self._title_line(task, selected=selected, opening=opening)
        )
        subtitle = task_subtitle(task.tui_meta or {})
        if subtitle:
            t.append("\n")
            t.append(subtitle, style=DIM)
        t.append("\n")
        t.append_text(self._meta_line(task))
        return t

    def _render_list(self) -> None:
        listview = self.query_one("#sessions-list", VerticalScroll)
        listview.remove_children()
        page = self._page_items()
        if not page:
            listview.mount(
                Static("(нет сессий)", classes="line-meta", markup=False)
            )
            return
        selected_card: Optional[Static] = None
        for idx, task in enumerate(page):
            suffix = " selected" if idx == self._selected_idx else ""
            card = Static(
                self._card_text(task, selected=bool(suffix)),
                classes="card" + suffix,
                markup=False,
            )
            if suffix:
                selected_card = card
            listview.mount(card)
        if selected_card is not None:
            # Элемент — ОДИН виджет (2–3 строки): scroll_visible тащит его целиком в
            # обе стороны кругового перехода (как карточка MCP).
            self.call_after_refresh(
                selected_card.scroll_visible, animate=False
            )

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
        self._cancel_open()
        new = event.value.strip()
        if new == self._filter:
            return  # эффективный фильтр не изменился (напр. одиночный/ведущий пробел)
        self._filter = new
        self._page = 0
        self._reload()

    def action_filter_select_all(self) -> None:
        """Ctrl+A в поле фильтра: выделить весь текст (не git add на экране правок)."""
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

    # Клавиши, которые меняют выбор или уводят из списка: идущее открытие
    # сначала отменяется (§3.3). Enter, Ctrl+P и Esc решают сами.
    _CANCEL_KEYS = frozenset(
        {"up", "down", "home", "end", "pageup", "pagedown"}
    ) | frozenset({"ctrl+b", "delete", "ctrl+d"})

    def on_key(self, event: events.Key) -> None:
        if event.key in self._CANCEL_KEYS or event.key in self._RENAME_KEYS:
            self._cancel_open()
        # R10: Ctrl+R и алиасы раскладки → переименование (до навигации).
        if event.key in self._RENAME_KEYS:
            self._rename_selected()
            event.stop()
            return
        page_len = len(self._page_items())
        if event.key == "up":
            if (
                page_len
            ):  # круговой в пределах страницы: с первой ↑ → на последнюю
                self._selected_idx = (self._selected_idx - 1) % page_len
                self._render_list()
            event.stop()
        elif event.key == "down":
            if page_len:  # круговой: с последней ↓ → на первую
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
            # Идёт открытие — Esc его отменяет, список остаётся.
            if self._opening is not None:
                self._cancel_open()
            else:
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
        self._branch_filter = (
            "current" if self._branch_filter == "all" else "all"
        )
        self._page = 0
        self._reload()

    # ---- actions -------------------------------------------------------

    def _resume_thread(self, thread_id: str) -> None:
        """Enter: сессия грузится в фоне, список ждёт с индикатором у
        карточки; идёт ход — сначала вопрос о его стопе (§3.3)."""
        if self._opening == (thread_id, _SESSION):
            return  # повторный Enter на той же сессии
        self._cancel_open()
        if self._session_is_busy(thread_id):
            self.app.notify(BUSY_MESSAGE, severity="warning")
            return
        main = trace_screen_of(self.app)
        if main is None:
            self.app.pop_screen()
            self.app.tui_state.active_task_id = thread_id
            return
        main.confirm_switch(
            thread_id, lambda: self._open_session(main, thread_id)
        )

    def _open_session(self, main, thread_id: str) -> None:
        """Путь 1: модель — со входами целевой сессии и её отозванными
        репликами; готово — переход."""
        self._start_open(
            thread_id,
            _SESSION,
            history_inputs_for(self.app, thread_id),
            withdrawn=main.withdrawn_in(thread_id),
            on_ready=lambda loaded: self._open_loaded_session(
                thread_id, loaded
            ),
        )

    def _open_loaded_session(
        self, thread_id: str, loaded: LoadedHistory
    ) -> None:
        """Сессия готова: замок — до стопа хода и закрытия списка; занята —
        список остаётся, ход идёт. Иначе список закрывается, экран сессии
        получает готовую модель."""
        self._stop_indicator()
        main = trace_screen_of(self.app)
        if not main.claim_session(thread_id):
            return
        self.app.pop_screen()
        main.switch_to_history(thread_id, loaded.model, loaded.cursor)

    def _start_open(
        self,
        thread_id: str,
        kind: str,
        inputs: HistoryInputs,
        *,
        on_ready,
        withdrawn: set[str] | frozenset = frozenset(),
    ) -> None:
        self._opening = (thread_id, kind)
        if self._selected_card() is not None:
            self._indicator = OpenIndicator(
                _OPEN_LABELS[kind], host=self, paint=self._paint_opening
            )
        self._loader.start(
            thread_id,
            inputs,
            withdrawn=withdrawn,
            on_ready=on_ready,
            on_error=self._open_failed,
        )

    def _open_failed(self, exc: Exception) -> None:
        """Сбой загрузки (битый файл треда): индикатор снят, список
        остаётся."""
        self._stop_indicator()
        self.app.notify(f"Не удалось открыть: {exc}", severity="error")

    def _paint_opening(self, frame) -> None:
        """Кадр индикатора — в карточку выбранной сессии, какой она стоит
        сейчас: список мог перерисоваться (смена выбора отменяет
        открытие)."""
        card, task = self._selected_card(), self._selected_task()
        if card is not None and task is not None:
            card.update(self._card_text(task, selected=True, opening=frame))

    def _selected_card(self) -> Optional[Static]:
        cards = self.query("#sessions-list .card.selected")
        return cards.first(Static) if cards else None

    def _stop_indicator(self) -> None:
        self._opening = None
        if self._indicator is not None:
            self._indicator.stop()
            self._indicator = None

    def _cancel_open(self) -> None:
        """Отменить открытие: загрузка дорабатывает в фоне, результат
        выбрасывается; идущий ход не трогается."""
        if self._opening is None:
            return
        self._loader.cancel()
        self._stop_indicator()

    def _delete_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        short = _short_id(task.thread_id)
        from gc_tui.screens.confirm import ConfirmScreen

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._delete_task(task.thread_id)

        self.app.push_screen(
            ConfirmScreen(
                title="Удаление",
                message=f"Удалить сессию {short}?\nЭто действие необратимо.",
            ),
            _on_result,
        )

    def _delete_task(self, thread_id: str) -> None:
        self._delete_or_warn([thread_id])
        self._reload()

    def _delete_or_warn(self, thread_ids: list[str]) -> None:
        """Удалить сессии; тред, который ведёт durable-исполнитель, удаляет
        только он сам — такие пропускаются с предупреждением."""
        kept = 0
        for thread_id in thread_ids:
            try:
                self._store.delete_task(thread_id)
            except OwnershipError:
                kept += 1
        if kept:
            self.app.notify(
                f"Не удалено сессий: {kept} — их ведёт durable-исполнитель",
                severity="warning",
            )

    def _open_mass_delete(self) -> None:
        tasks = self._store.list_tasks(limit=10000)  # весь проект, не страница
        if not tasks:
            return
        now = datetime.now().astimezone()
        from gc_tui.screens.mass_delete import MassDeleteScreen

        def _on_criterion(criterion: Optional[dict]) -> None:
            if criterion:
                self._confirm_mass_delete(tasks, criterion, now)

        self.app.push_screen(
            MassDeleteScreen(tasks=tasks, now=now), _on_criterion
        )

    def _confirm_mass_delete(self, tasks: list, criterion: dict, now) -> None:
        from gc_tui.services.session_delete_select import select_for_deletion

        ids = select_for_deletion(tasks, now=now, **criterion)
        if not ids:
            return
        total = len(tasks)
        from gc_tui.screens.confirm import ConfirmScreen

        def _on_confirm(ok: bool) -> None:
            if not ok:
                return
            self._delete_or_warn(list(ids))
            self._reload()

        self.app.push_screen(
            ConfirmScreen(
                title="Массовое удаление",
                message=f"Удалить {len(ids)} из {total} сессий?\nЭто действие необратимо.",
            ),
            _on_confirm,
        )

    def _rename_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        tid = task.thread_id
        from gc_tui.screens.rename import RenameScreen

        def _on_result(value: Optional[str]) -> None:
            if value is None:
                return
            self._apply_rename(tid, value)

        self.app.push_screen(
            RenameScreen(current=self._title_of(task)), _on_result
        )

    def _apply_rename(self, thread_id: str, value: str) -> None:
        # пустое значение → откат к авто-имени: title="" сброшен, display_name
        # отдаёт last_actual (R7), а при его отсутствии — «Без названия».
        self._store.update_tui_meta(thread_id, {"title": value})
        self._reload()

    def _model_slots(self) -> dict:
        """Слоты уровней действующих настроек окна — для черт смены
        модели в превью."""
        from gc_core.settings import settings_of

        return settings_of(self.app.effective_settings()).slots.model_dump()

    def _preview_selected(self) -> None:
        """Ctrl+P (путь 2): модель превью — со входами базы — строится в
        фоне; превью открывается готовым."""
        task = self._selected_task()
        if task is None:
            return
        tid = task.thread_id
        if self._opening == (tid, _PREVIEW):
            return
        self._cancel_open()
        settings = self.app.base_settings().settings
        inputs = HistoryInputs(
            root=self._project_path(),
            injected_fields=tuple(sorted(settings.agent.inject_tool_fields)),
        )
        self._start_open(
            tid,
            _PREVIEW,
            inputs,
            on_ready=lambda loaded: self._show_preview(tid, loaded),
        )

    def _show_preview(self, tid: str, loaded: LoadedHistory) -> None:
        self._stop_indicator()
        subagent_records = self._store.read_subagent_trace(tid)
        record = self._store.get_record(tid)
        from gc_tui.screens.preview import PreviewScreen

        def _on_result(session: Optional[LoadedHistory]) -> None:
            # Превью уже взяло замок и построило модель сессии (путь 3).
            if session is not None:
                self._open_loaded_session(tid, session)

        self.app.push_screen(
            PreviewScreen(
                segments=loaded.segments,
                cursor=loaded.cursor,
                model=loaded.model,
                thread_id=tid,
                project_path=self._project_path(),
                subagent_records=subagent_records,
                mcp_snapshot=record.runtime.mcp_tools if record else None,
                model_slots=self._model_slots(),
            ),
            _on_result,
        )
