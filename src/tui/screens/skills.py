"""SkillsScreen — picker скиллов двустрочными карточками (id / тип · путь)."""
from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from tui.palette import BRIGHT, CODE, DIM, INK, MUTED
from tui.services.skill_view import source_label, truncate_path_left

_FOOTER = "[↑↓] выбрать  [Enter] открыть  [Esc] back  /skill <id>"
_PATH_MIN = 16          # минимально видимая длина пути (хвост-папка)
_DEFAULT_WIDTH = 80     # пока ширина списка неизвестна (до первой раскладки)
_TYPE_RESERVE = 16      # длиннейший RU-ярлык типа ("пользовательский")


class SkillsScreen(Screen):
    """Список скиллов двустрочными карточками; Enter — вьювер скилла."""

    DEFAULT_CSS = ""

    def __init__(self, *, skills: Optional[list[dict]] = None,
                 config: Optional[dict] = None) -> None:
        super().__init__()
        self._all_skills = skills or []
        self._config = config or {}
        self._skills = list(self._all_skills)
        self._selected_idx = 0 if self._skills else -1
        self._filter = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="skills-root"):
            with Horizontal(id="header"):
                yield Static("", id="header-left", markup=False)
            yield VerticalScroll(id="skills-list")
            with Horizontal(id="skills-filter-row"):
                yield Static("> ", id="skills-filter-prompt", markup=False)
                yield Input(placeholder="фильтр…", id="skills-filter")
            yield Static(_FOOTER, id="footer", markup=False)

    def on_mount(self) -> None:
        self._render_list()
        self._refresh_header()
        self.query_one("#skills-filter", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        # Ширина изменилась → пересчитать адаптивное усечение пути.
        self._render_list()

    def _refresh_header(self) -> None:
        self.query_one("#header-left", Static).update(
            f"доступные навыки · {len(self._skills)}")

    def _reload(self) -> None:
        f = self._filter.lower()
        if f:
            self._skills = [
                s for s in self._all_skills
                if f in (s.get("id", "")).lower()
                or f in (s.get("root_path", "")).lower()
                or f in source_label(s.get("source", "")).lower()
            ]
        else:
            self._skills = list(self._all_skills)
        self._selected_idx = 0 if self._skills else -1
        self._render_list()
        self._refresh_header()

    def _path_maxlen(self) -> int:
        try:
            width = self.query_one("#skills-list", VerticalScroll).size.width
        except Exception:
            width = 0
        if width <= 0:
            width = self.app.size.width or _DEFAULT_WIDTH
        # запас: паддинг (2) + ярлык типа + разделитель " · " (3)
        return max(_PATH_MIN, width - 2 - _TYPE_RESERVE - 3)

    def _id_line(self, skill: dict, *, selected: bool) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(skill.get("id") or "?", style=BRIGHT if selected else INK)
        return t

    def _meta_line(self, skill: dict, *, maxlen: int) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(source_label(skill.get("source", "")), style=CODE)
        path = skill.get("root_path") or ""
        if path:
            t.append(" · ", style=MUTED)
            t.append(truncate_path_left(path, maxlen), style=DIM)
        return t

    def _card_text(self, skill: dict, *, selected: bool, maxlen: int) -> Text:
        """Карточка скилла одним 2-строчным виджетом: id + строка типа/пути."""
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append_text(self._id_line(skill, selected=selected))
        t.append("\n")
        t.append_text(self._meta_line(skill, maxlen=maxlen))
        return t

    def _render_list(self) -> None:
        listview = self.query_one("#skills-list", VerticalScroll)
        listview.remove_children()
        if not self._skills:
            listview.mount(Static("(скиллы недоступны)", classes="line-meta", markup=False))
            return
        maxlen = self._path_maxlen()
        selected_card: Optional[Static] = None
        for idx, s in enumerate(self._skills):
            suffix = " selected" if idx == self._selected_idx else ""
            card = Static(self._card_text(s, selected=bool(suffix), maxlen=maxlen),
                          classes="card" + suffix, markup=False)
            if suffix:
                selected_card = card
            listview.mount(card)
        if selected_card is not None:
            # Элемент — ОДИН виджет (2 строки): scroll_visible тащит его целиком в
            # обе стороны кругового перехода (как карточка MCP), без конфликта двух
            # независимых прокруток заголовка и меты.
            self.call_after_refresh(selected_card.scroll_visible, animate=False)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "skills-filter":
            self._filter = event.value.strip()
            self._reload()

    def on_key(self, event: events.Key) -> None:
        if event.key == "up":
            if self._skills:   # круговой: с первой строки ↑ → на последнюю
                self._selected_idx = (self._selected_idx - 1) % len(self._skills)
                self._render_list()
            event.stop()
        elif event.key == "down":
            if self._skills:   # круговой: с последней ↓ → на первую
                self._selected_idx = (self._selected_idx + 1) % len(self._skills)
                self._render_list()
            event.stop()
        elif event.key in ("home", "end"):
            if self._skills:
                self._selected_idx = 0 if event.key == "home" else len(self._skills) - 1
                self._render_list()
            event.stop()
        elif event.key == "enter":
            if self._skills and 0 <= self._selected_idx < len(self._skills):
                self._open_selected()
            event.stop()
        elif event.key == "escape":
            self.app.pop_screen()
            event.stop()

    def _open_selected(self) -> None:
        skill = self._skills[self._selected_idx]
        sid = skill.get("id")
        try:
            from tools.skills.registry import get_skills_registry
            reg = get_skills_registry(self._config)
            detail = reg.load_skill_detail(sid)
        except Exception as exc:
            self.app.notify(f"/skill: {exc}", severity="error")
            return
        from tui.screens.viewer import ViewerScreen
        from tui.services.skill_view import build_skill_viewer
        title, body = build_skill_viewer(detail)
        self.app.push_screen(ViewerScreen(title=title, content=body))
