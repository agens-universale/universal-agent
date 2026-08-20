"""McpSectionEditor — тело секции «MCP-серверы» хаба /settings (спека C,
контракт §4). Строки manifest∪wired + ручной read-only блок; статусы —
фоновым воркером (идиома model-fetch, Task 6); операции — через инъецируемый
McpManager."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Static

from gc_tui.palette import ACCENT, DEEP_DIM, DIM, INK, OK, WARN
from gc_tui.widgets.loading_wave import wave_glyph


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу (1 обновление / 2 обновления / 5 обновлений)."""
    n100 = abs(n) % 100
    if 11 <= n100 <= 14:
        return many
    n10 = n100 % 10
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


class McpSectionEditor(Vertical):
    def __init__(self, *, manager, on_change=None) -> None:
        super().__init__(classes="onb-dyn")
        self._mgr = manager
        self._on_change = on_change
        self._rows = manager.rows()
        self._states: dict[str, object] = {}     # name -> McpState (после воркера, Task 6)
        self._idx = 0                             # курсор среди управляемых (не ручных)
        self._action_kind: str | None = None      # курсор на строке-действии: "missing"/"update"/None
        self._wave_i = 0
        self._pending = None                       # {"kind": "remove"|"install", "names": [...]} в режиме подтверждения

    @property
    def _managed(self) -> list:
        return [r for r in self._rows if not r.manual]

    @property
    def _manual(self) -> list:
        return [r for r in self._rows if r.manual]

    @property
    def current_row(self):
        if self._action_kind is not None:
            return None
        managed = self._managed
        return managed[self._idx] if managed and self._idx < len(managed) else None

    @property
    def current_action(self) -> str | None:
        """Курсор на строке-действии ⤓/⟳: "missing"/"update"/None."""
        return self._action_kind

    def compose(self) -> ComposeResult:
        for i, row in enumerate(self._managed):
            selected = i == self._idx and self._action_kind is None
            cls = "mcp-row mcp-row-managed onb-dyn set-spaced" + (" selected" if selected else "")
            yield Static(self._row_text(row, i), classes=cls, markup=False)
        manual = self._manual
        if manual:
            yield Static("─" * 40, classes="mcp-divider onb-dyn", markup=False)
            for row in manual:
                yield Static(self._row_text(row, -1), classes="mcp-row mcp-manual onb-dyn set-spaced",
                            markup=False)

    def _row_text(self, row, i: int) -> Text:
        t = Text()
        if row.manual:
            t.append("  ")
            t.append(f"{row.name:22}", style=DEEP_DIM)
            t.append("вручную в config.yml", style=DEEP_DIM)
            return t
        selected = i == self._idx and self._action_kind is None
        t.append("▸" if selected else " ", style=ACCENT)
        box = "[●] " if row.wired else "[ ] "        # ● — активно (v3-язык)
        t.append(box, style=INK if row.wired else DEEP_DIM)
        t.append(f"{row.name:20}", style=INK)
        t.append("  ")
        t.append_text(self._status_text(row))
        desc = getattr(row, "description", "")
        if desc:
            col = self._desc_col()
            pad = col - t.cell_len
            t.append(" " * pad if pad > 0 else "  ")
            t.append(desc, style=DEEP_DIM)
        return t

    def _managed_prefix_len(self, row) -> int:
        """Ширина строки до описания: курсор(1)+чекбокс(4)+имя(поле ≥20)+зазор(2)+статус."""
        name_w = max(20, len(row.name))
        return 1 + 4 + name_w + 2 + self._status_text(row).cell_len

    def _desc_col(self) -> int:
        """Колонка старта описания = по самому широкому статусу среди управляемых строк:
        описания строго в столбец (при узких статусах — шире, при длинном «установлен,
        отключён» — столбец сдвигается, но выровнен), не ломается на одной длинной строке."""
        managed = self._managed
        if not any(getattr(r, "description", "") for r in managed):
            return 0
        return max(self._managed_prefix_len(r) for r in managed) + 2

    def _status_text(self, row) -> Text:
        st = self._states.get(row.name)
        t = Text()
        if st is None:
            t.append_text(wave_glyph(self._wave_i))    # статус ещё грузится (Task 6)
            return t
        if row.wired:
            t.append("подключён", style=OK)
        elif st.installed:
            t.append("установлен, отключён", style=DIM)
        elif st.installed is None:
            t.append("— не проверено", style=DEEP_DIM)
        else:
            t.append("не установлен", style=DEEP_DIM)
        if st.version:
            t.append(f" · v{st.version}", style=DIM)
        if st.update:
            t.append(f" · доступно {st.update}", style=WARN)
        return t

    def move(self, delta: int) -> None:
        managed = self._managed
        kinds = self._action_kinds()
        total = len(managed) + len(kinds)
        if total == 0:
            return
        if self._action_kind is not None and self._action_kind not in kinds:
            self._action_kind = None
        pos = self._idx if self._action_kind is None else len(managed) + kinds.index(self._action_kind)
        pos = (pos + delta) % total
        if pos < len(managed):
            self._idx, self._action_kind = pos, None
        else:
            self._action_kind = kinds[pos - len(managed)]
        self._refresh_rows()
        self.refresh_action_rows()

    def _refresh_rows(self) -> None:
        managed = self._managed
        rows = list(self.query(".mcp-row-managed"))
        for i, (w, row) in enumerate(zip(rows, managed)):
            try:
                w.update(self._row_text(row, i))
                if i == self._idx and self._action_kind is None:
                    w.add_class("selected")
                else:
                    w.remove_class("selected")
            except Exception:
                continue

    def on_mount(self) -> None:
        self._wave_timer = self.set_interval(0.16, self._wave_tick)
        self.run_worker(self._fetch_states, thread=True, name="mcp-states")

    def _wave_tick(self) -> None:
        # волна крутится, пока есть строки без статуса
        if not self.is_mounted or all(r.name in self._states for r in self._managed):
            if getattr(self, "_wave_timer", None):
                self._wave_timer.stop(); self._wave_timer = None
            return
        self._wave_i = (self._wave_i + 1) % 4
        self._refresh_rows()

    def _fetch_states(self) -> None:
        for row in self._managed:
            try:
                st = self._mgr.state(row.name)
            except Exception:
                from gc_tui.services.mcp_manager import McpState
                st = McpState(None, None, None)
            self.app.call_from_thread(self._apply_state, row.name, st)

    def _apply_state(self, name: str, st) -> None:
        if not self.is_mounted:
            return
        self._states[name] = st
        self._refresh_rows()
        self.refresh_action_rows()

    def status_ready(self, name: str) -> bool:
        """Статус строки пришёл (Ctrl+U/Del активны только тогда, §4)."""
        return name in self._states

    # --- строки-действия ⤓/⟳ (за разделителем, скрываются если нечего делать) ---
    def _missing_names(self) -> list:
        return [r.name for r in self._managed
                if (st := self._states.get(r.name)) is not None and st.installed is False]

    def _updatable_names(self) -> list:
        """Подключённые с ОБНАРУЖЕННЫМ обновлением (pypi-пин ≠ установленной) — для
        счётчика «N обновлений»; source-рецепты сигнала не дают."""
        return [r.name for r in self._managed
                if r.wired and (st := self._states.get(r.name)) is not None and st.update]

    def _connected_names(self) -> list:
        """Все подключённые серверы — цель «Обновить все подключённые» (идемпотентная
        пересборка). §4: строка видна, когда есть подключённые, не только при обновлении."""
        return [r.name for r in self._managed if r.wired]

    def _action_kinds(self) -> list:
        kinds = []
        if self._missing_names():
            kinds.append("missing")
        if self._connected_names():
            kinds.append("update")
        return kinds

    def _action_row_text(self, kind: str) -> Text:
        selected = self._action_kind == kind
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        if kind == "missing":
            names = self._missing_names()
            t.append("↓ ", style=ACCENT)
            t.append("Установить все недостающие", style=INK)
            t.append(f"    {len(names)} шт: {', '.join(names)}", style=DIM)
        else:
            names = self._connected_names()
            upd = self._updatable_names()
            t.append("↻ ", style=ACCENT)
            t.append("Обновить все подключённые", style=INK)
            detail = f"    {len(names)} шт: {', '.join(names)}"
            if upd:
                word = _ru_plural(len(upd), "обновление", "обновления", "обновлений")
                detail += f" · {len(upd)} {word}"
            t.append(detail, style=DIM)
        return t

    def refresh_action_rows(self) -> None:
        kinds = self._action_kinds()
        if self._action_kind not in kinds:
            self._action_kind = None
        # Набор строк-действий не изменился → обновляем текст на месте (курсор ▸),
        # без remove+mount: снос в Textual асинхронен, а move() дёргает это часто.
        mounted = [getattr(w, "_action_kind", None) for w in self.query(".mcp-action-row")]
        if mounted == kinds:
            self._refresh_action_text()
            return
        for w in self.query(".mcp-action"):
            w.remove()
        if not kinds:
            return
        try:
            anchor = self.query_one(".mcp-divider")   # разделитель управляемые/ручные (Task 5)
        except NoMatches:
            anchor = None
        widgets = [Static("─" * 40, classes="mcp-action onb-dyn", markup=False)]
        for kind in kinds:
            cls = "mcp-action mcp-action-row onb-dyn set-spaced" + (" selected" if self._action_kind == kind else "")
            w = Static(self._action_row_text(kind), classes=cls, markup=False)
            w._action_kind = kind
            widgets.append(w)
        for w in widgets:
            if anchor is not None:
                self.mount(w, before=anchor)
            else:
                self.mount(w)

    def _refresh_action_text(self) -> None:
        """Обновить текст строк-действий на месте (для курсора ▸), без ре-маунта."""
        for w in self.query(".mcp-action-row"):
            kind = getattr(w, "_action_kind", None)
            if kind is not None:
                w.update(self._action_row_text(kind))
                if self._action_kind == kind:
                    w.add_class("selected")
                else:
                    w.remove_class("selected")

    def toggle(self) -> str | None:
        """Space: wired→unwire, installed→wire. Возвращает хинт-строку, если действие
        неприменимо (не установлен → подсказать про Enter), иначе None."""
        if self.awaiting_confirm:
            return None                # confirm-баннер висит — no-op (Enter/Esc решают его)
        row = self.current_row
        if row is None:
            return None
        if row.wired:
            self._mgr.unwire(row.name)
        else:
            st = self._states.get(row.name)
            if st is None:
                return None                 # статус ещё грузится — молча
            if not st.installed:
                return f"«{row.name}» не установлен — Enter, чтобы установить"
            self._mgr.wire(row.name)
        self._reload_rows()
        if self._on_change is not None:
            self._on_change()
        return None

    def _after_pip(self) -> None:
        """После install/remove — перечитать строки и статусы, поднять волну загрузки,
        уведомить владельца."""
        self._reload_rows()
        self._states.clear()
        self.run_worker(self._fetch_states, thread=True, name="mcp-states")
        if getattr(self, "_wave_timer", None) is None:
            self._wave_timer = self.set_interval(0.16, self._wave_tick)
        if self._on_change is not None:
            self._on_change()

    def _install_names(self, names: list) -> list:
        """Ставит перечисленные MCP через app.suspend (живой pip-вывод); per-item
        try/except — падение одного не валит остальные. Fallback без suspend —
        headless/тесты. Возвращает [(name, ok)]."""
        results = []

        def _do():
            results.clear()
            for name in names:
                try:
                    rc = self._mgr.install(name)
                    results.append((name, rc == 0))
                except Exception:
                    results.append((name, False))
        try:
            with self.app.suspend():
                print("\nУстанавливаю MCP: " + ", ".join(names) + " …\n")
                _do()
        except Exception:
            _do()
        self._after_pip()
        return results

    @property
    def awaiting_confirm(self) -> bool:
        return self._pending is not None

    def request_remove(self) -> bool:
        if self.awaiting_confirm:
            return False            # confirm-баннер уже висит — no-op
        row = self.current_row
        if row is None or row.manual:
            return False
        st = self._states.get(row.name)
        if st is None or not st.installed:
            return False           # нечего удалять / статус не пришёл
        self._pending = {"kind": "remove", "names": [row.name]}
        banner = Static(
            Text(f"Удалить пакет {row.name} из venv? Подключение уже снято, "
                 f"файлы пакета будут удалены. Enter — удалить · Esc — отмена", style=WARN),
            classes="mcp-confirm onb-dyn", markup=False)
        self.mount(banner)
        return True

    def request_install(self, names: list, *, verb: str) -> bool:
        if self.awaiting_confirm or not names:
            return False
        self._pending = {"kind": "install", "names": list(names)}
        banner = Static(
            Text(f"{verb}: {', '.join(names)}? Enter — {verb.lower()} · Esc — отмена", style=WARN),
            classes="mcp-confirm onb-dyn", markup=False)
        self.mount(banner)
        return True

    def begin_enter(self) -> bool:
        """Enter вне режима подтверждения: решает цель (недостающие/обновления/выбранный
        сервер) и показывает confirm — ничего не устанавливает сразу."""
        if self._action_kind == "missing":
            return self.request_install(self._missing_names(), verb="Установить")
        if self._action_kind == "update":
            return self.request_install(self._connected_names(), verb="Обновить")
        row = self.current_row
        if row is None or row.wired or row.manual:
            return False
        st = self._states.get(row.name)
        if st is None or st.installed:
            return False           # статус ещё грузится / уже установлен
        return self.request_install([row.name], verb="Установить")

    def begin_update(self) -> bool:
        """Ctrl+U вне режима подтверждения: показывает confirm обновления выбранного
        сервера. No-op на строке-действии / ручной / не-установленной / до статуса."""
        row = self.current_row
        if row is None or row.manual:
            return False
        st = self._states.get(row.name)
        if st is None or not (row.wired or st.installed):
            return False
        return self.request_install([row.name], verb="Обновить")

    def confirm_pending(self) -> list:
        p = self._pending
        self._clear_confirm()
        if p is None:
            return []
        if p["kind"] == "remove":
            self._mgr.remove(p["names"][0])
            self._after_pip()
            return []
        return self._install_names(p["names"])

    def cancel_pending(self) -> None:
        self._clear_confirm()

    def _clear_confirm(self) -> None:
        self._pending = None
        for w in self.query(".mcp-confirm"):
            w.remove()

    def _reload_rows(self) -> None:
        cur = self.current_row
        self._rows = self._mgr.rows()
        managed = self._managed
        if cur is not None:
            names = [r.name for r in managed]
            self._idx = names.index(cur.name) if cur.name in names else 0
        # клампинг: курсор мог стоять на строке-действии (cur is None) или строки
        # сократились — _idx не должен выходить за границу управляемых
        self._idx = min(self._idx, len(managed) - 1) if managed else 0
        self._refresh_rows()
        self.refresh_action_rows()
