"""Нативный Textual-онбординг gc-ua (презентация + оркестрация шагов).

Списки-с-выбором — hand-rolled Static-карточки + on_key (идиома McpScreen).
Переиспользует ядро: onboarding (политика), onboarding_config_writer,
model_picker, mcp_runtime.cli. Логика сбора — как в плоском onboarding_wizard,
меняется только презентация."""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static
from rich.text import Text

import onboarding
from tui import branding
from tui.palette import ACCENT, DEEP_DIM, DIM, INK, OK

_STEP_TITLES = ("Адрес API-сервера", "Авторизация", "Модель", "MCP-серверы", "Подтверждение")
_STEP_STEPS = ("Адрес", "Авторизация", "Модель", "MCP")   # короткие имена для степпера
_LAST_STEP = 4   # final/confirm


class OnboardingScreen(Screen):
    # Модальный экран: конфига ещё нет, глобальная навигация (F1/F4/…) поверх мастера
    # показывала бы справку с неработающими клавишами и уведомления по незаписанному
    # конфигу. Гейт — TuiApp.check_action.
    BLOCKS_GLOBAL_NAV = True

    # priority=True — перехватывает PgUp раньше фокусированного Input (иначе поля
    # #onb-endpoint/#onb-token/#model-filter не отдали бы клавишу назад).
    BINDINGS = [
        Binding("pageup", "go_back", "назад", show=False, priority=True),
    ]

    def __init__(self, config, config_path, *, review_all, on_commit,
                 fetch=None, mcp=None) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        self._review_all = review_all
        self._on_commit = on_commit
        from model_picker import fetch_models
        self._fetch = fetch or fetch_models
        if mcp is None:
            import mcp_runtime.cli as mcp_mod
            self._mcp = mcp_mod
        else:
            self._mcp = mcp
        self._step = 0
        self._values: dict = {}
        self._idx: dict = {}
        self._mcp_selected: set = set()
        self._mcp_installed: set = set()   # уже подключённые (wired) — отмечаем, не предлагаем
        self._auth_token_raw = ""   # сырой токен (для восстановления поля при возврате; маскируется)

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="onb-header"):
                yield Static(self._title_text(), id="header-left", markup=False)
            scroll = VerticalScroll(id="onb-scroll")
            scroll.can_focus = False
            with scroll:
                with Vertical(id="onb-frame"):
                    yield Static(branding.logo_text(), id="onb-logo", markup=False)
                    yield Static(branding.tagline_under_logo(), id="onb-tagline", markup=False)
                    yield Static(self._stepper_text(), id="onb-stepper", markup=False)
                    yield Static(self._summary_text(), id="onb-summary",
                                 classes="onb-hint", markup=False)
                    yield Static(self._body_placeholder(), id="onb-body", markup=False)
            yield Static(self._footer_text(), id="onb-footer", markup=False)

    # --- хром (хедер-титул + именованный степпер + футер-подсказки, как основной экран) ---
    def _title_text(self) -> str:
        if self._step == _LAST_STEP:
            return "Настройка · подтверждение"
        return f"Настройка · {self._step + 1}/4"

    def _stepper_text(self) -> Text:
        t = Text()
        for i, name in enumerate(_STEP_STEPS):
            if i:
                t.append("    ")
            if i < self._step:
                mark, style = "✓ ", OK             # пройденные — зелёные (на финале все шаги пройдены)
            elif i == self._step:
                mark, style = "● ", INK            # текущий — белый (как выбранная строка)
            else:
                mark, style = "○ ", DEEP_DIM
            t.append(mark, style=style)
            t.append(name, style=f"bold {style}" if i == self._step else style)
        return t

    def _summary_text(self) -> str:
        from tui.services.onboarding_summary import summary_line
        # Шаблонные заглушки конфига (…_SENTINEL) — это «ещё не задано», а не значение:
        # показываем прочерком, как редактор адреса трактует URL_SENTINEL пустым полем.
        url = self._values.get("completions_url") or self._config.get("completions_url") or ""
        if url == onboarding.URL_SENTINEL:
            url = ""
        model = self._values.get("model") or self._config.get("model") or ""
        if model == onboarding.MODEL_SENTINEL:
            model = ""
        return summary_line(
            url=url,
            auth=self._values.get("Authorization"),
            model=model,
            mcp_count=len(self._mcp_selected),
        )

    def _footer_text(self) -> str:
        if self._step == _LAST_STEP:
            return "Enter — подтвердить и завершить настройку  ·  PgUp — назад  ·  Esc — выход"
        parts = ["↑↓ навигация", "Enter — далее", "Esc — выход"]
        if self._step > 0:
            parts.insert(1, "PgUp — назад")
        if self._step == 3:
            parts.insert(0, "Space — выбрать")
        return "  ·  ".join(parts)

    def _body_placeholder(self) -> str:
        return _STEP_TITLES[self._step]

    def on_mount(self) -> None:
        self._mount_step_body()

    def _mount_step_body(self) -> None:
        """Смонтировать виджеты текущего шага в #onb-frame (после #onb-body)."""
        for w in self.query(".onb-dyn"):
            w.remove()
        body = self.query_one("#onb-frame", Vertical)
        if self._step == 0:
            from tui.editors.endpoint import EndpointEditor
            cur = self._values.get("completions_url")
            if not cur:
                cfg_url = (self._config or {}).get("completions_url")
                cur = cfg_url if cfg_url and cfg_url != onboarding.URL_SENTINEL else ""
            ed = EndpointEditor(current=cur, framed=True)
            body.mount(ed)
            self.call_after_refresh(ed.focus_input)
        elif self._step == 1:
            from tui.editors.auth import AuthEditor
            chosen = self._values.get("Authorization")   # выбор в этой сессии (напр. "" — без авторизации)
            if chosen is not None:
                cur_hdr = chosen
            elif self._review_all:
                cur_hdr = (self._config.get("chat_completion_headers") or {}).get("Authorization")
            else:
                cur_hdr = None
            ed = AuthEditor(current_header=cur_hdr, token_raw=self._auth_token_raw,
                            allow_skip=True, framed=True)
            body.mount(ed)
        elif self._step == 2:
            from tui.editors.model import ModelEditor
            endpoint = self._values.get("completions_url") or self._config.get("completions_url") or ""
            auth = self._values.get("Authorization")
            if auth is None:
                auth = (self._config.get("chat_completion_headers") or {}).get("Authorization")
            if auth == onboarding.TOKEN_SENTINEL:
                auth = None
            cfg_model = self._config.get("model") or ""
            if cfg_model == onboarding.MODEL_SENTINEL:
                cfg_model = ""
            ed = ModelEditor(current=self._values.get("model", ""), endpoint=endpoint,
                             auth=auth, verify=bool(self._config.get("use_ssl", True)),
                             fetch=self._fetch,
                             active=self._values.get("model") or cfg_model, framed=True)
            body.mount(ed)
        elif self._step == 3:
            self._idx.setdefault(3, 0)
            names = list(self._mcp.installable_names())
            self._mcp_names = names
            try:
                self._mcp_desc = self._mcp.descriptions()
            except Exception:
                self._mcp_desc = {}
            try:
                self._mcp_installed = set(self._mcp.wired_names())
            except Exception:
                self._mcp_installed = set()
            for i, name in enumerate(names):
                cls = "onb-option onb-dyn set-spaced onb-mcp-row" + (" selected" if i == self._idx[3] else "")
                body.mount(Static(self._mcp_row_text(i == self._idx[3], name), classes=cls))
        elif self._step == 4:
            from onboarding import mask_authorization
            addr = self._values.get("completions_url") or self._config.get("completions_url") or "—"
            model = self._values.get("model") or self._config.get("model") or "—"
            auth = self._values.get("Authorization")
            if auth is None:
                tok = "—"
            elif auth == "":
                tok = "без авторизации"
            else:
                tok = mask_authorization(auth)
            mcp_val = (", ".join(sorted(self._mcp_selected)) if self._mcp_selected
                       else "не выбраны — можно добавить позже")
            rows = [("адрес", addr), ("модель", model), ("токен", tok), ("MCP", mcp_val)]
            summary = Text()
            for n, (label, val) in enumerate(rows):
                if n:
                    summary.append("\n")            # воздух между строками сводки
                summary.append("✓ ", style=OK)
                summary.append(f"{label:7}", style=DIM)
                summary.append(f"{val}\n", style=INK)
            if self._mcp_selected:
                summary.append("(MCP установятся после подтверждения)", style=DEEP_DIM)
            body.mount(Static(summary, classes="onb-dyn"))

    def _render_step(self) -> None:
        """Перерисовать хром (хедер/степпер/футер) + тело под текущий _step."""
        self.query_one("#header-left", Static).update(self._title_text())
        self.query_one("#onb-stepper", Static).update(self._stepper_text())
        self.query_one("#onb-summary", Static).update(self._summary_text())
        self.query_one("#onb-footer", Static).update(self._footer_text())
        self.query_one("#onb-body", Static).update(self._body_placeholder())
        self._mount_step_body()

    def _submit_address(self) -> None:
        from tui.editors.endpoint import EndpointEditor
        ed = self.query_one(EndpointEditor)
        ok, value = ed.submit()
        if not ok:
            return
        if value is not None:
            self._values["completions_url"] = value
        self._advance()

    def _submit_model(self) -> None:
        from tui.editors.model import ModelEditor
        value = self.query_one(ModelEditor).submit()
        if value is not None:
            self._values["model"] = value
        self._advance()

    def _move_auth(self, delta: int) -> None:
        from tui.editors.auth import AuthEditor
        self.query_one(AuthEditor).move(delta)

    _DESC_COL = 50   # то же значение, что McpSectionEditor._row_text в /settings (согласованность)

    def _mcp_row_text(self, is_cursor: bool, name: str) -> Text:
        """Строка MCP: курсор ▸ + чекбокс/статус + имя + приглушённое описание (тот же
        источник, что /settings). Уже подключённые (wired в config.local.yml) — [✓] «подключён» (тускло)."""
        t = Text()
        t.append("▸ " if is_cursor else "  ", style=ACCENT)
        desc = self._mcp_desc.get(name, "") if hasattr(self, "_mcp_desc") else ""
        if name in self._mcp_installed:
            t.append("[✓] ", style=OK)
            t.append(name, style=DEEP_DIM)
            t.append("  подключён", style=DEEP_DIM)
            self._append_mcp_desc(t, desc)
            return t
        # Чекбокс (множественный выбор): [●] включён — белый кружок, единый язык «●=активно».
        t.append("[")
        if name in self._mcp_selected:
            t.append("●", style=INK)
        else:
            t.append(" ")
        t.append(f"] {name}")
        self._append_mcp_desc(t, desc)
        return t

    def _append_mcp_desc(self, t: Text, desc: str) -> None:
        """Дописать приглушённое описание с выравниванием на _DESC_COL (приём — как
        McpSectionEditor._row_text в /settings)."""
        if not desc:
            return
        pad = self._DESC_COL - t.cell_len
        t.append(" " * pad if pad > 0 else "  ")
        t.append(desc, style=DEEP_DIM)

    def _move_mcp(self, delta: int) -> None:
        names = getattr(self, "_mcp_names", [])
        n = max(1, len(names))
        self._idx[3] = (self._idx.get(3, 0) + delta) % n
        cur = self._idx[3]
        opts = list(self.query(".onb-option"))
        for i, w in enumerate(opts):
            w.update(self._mcp_row_text(i == cur, names[i]))
            if i == cur:
                w.add_class("selected")
            else:
                w.remove_class("selected")

    def _toggle_mcp(self) -> None:
        names = getattr(self, "_mcp_names", [])
        if not names:
            return
        cur = self._idx.get(3, 0)
        name = names[cur]
        if name in self._mcp_installed:
            return   # уже подключён — не переключаем
        if name in self._mcp_selected:
            self._mcp_selected.discard(name)
        else:
            self._mcp_selected.add(name)
        # update only the toggled widget in-place
        opts = list(self.query(".onb-option"))
        if cur < len(opts):
            opts[cur].update(self._mcp_row_text(True, name))

    def _submit_auth(self) -> None:
        from tui.editors.auth import AuthEditor
        ok, header, raw = self.query_one(AuthEditor).submit()
        if not ok:
            return                              # «введите токен» показан внутри редактора
        if raw:
            self._auth_token_raw = raw   # не затирать введённый ранее токен пустым выбором
        if header is not None:
            self._values["Authorization"] = header
        self._advance()

    def _commit(self) -> None:
        import onboarding_config_writer as w
        from config import _default_config_path, validate_config_text
        from onboarding import FIELD_ANCHOR_PATHS
        dotted = {FIELD_ANCHOR_PATHS.get(k, k): v for k, v in self._values.items()}
        try:
            w.write_values(
                self._config_path, dotted,
                template_path=_default_config_path(),
                validate=validate_config_text,
            )
        except ValueError as exc:
            self.query_one("#onb-body", Static).update(f"✗ конфиг не записан: {exc}")
            return
        self.query_one("#onb-body", Static).update("✓ конфиг записан")
        self._install_selected_mcp()
        self._on_commit()

    def _install_selected_mcp(self) -> None:
        """Установить выбранные MCP. Вывод pip шумный (subprocess) и корёжит alt-screen —
        выходим из него через app.suspend() (pip-прогресс идёт в обычный терминал, потом
        TUI возвращается). Fallback без suspend — для headless/тестов, где он не поддержан.
        Per-item try/except: одна упавшая установка не валит остальные и не блокирует переход."""
        names = sorted(self._mcp_selected)
        if not names:
            return

        def _do() -> None:
            for name in names:
                try:
                    self._mcp.install(name)
                except Exception:
                    pass

        try:
            with self.app.suspend():
                print("\nУстанавливаю MCP: " + ", ".join(names) + " …\n")
                _do()
        except Exception:
            _do()

    # --- навигация ---
    def _advance(self) -> None:
        if self._step < _LAST_STEP:
            self._step += 1
            self._render_step()

    def _go_back(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._render_step()

    def action_go_back(self) -> None:
        self._go_back()

    def _confirm_exit(self) -> None:
        """Esc → модалка подтверждения выхода (как в основном TUI), не мгновенный выход."""
        from tui.screens.confirm import ConfirmScreen

        def _cb(ok) -> None:
            if ok:
                self.app.exit()

        self.app.push_screen(
            ConfirmScreen(message="Выйти из настройки? Введённые данные не сохранятся.",
                          hint="[Y] выйти  [N] остаться", title="Выход из настройки"),
            _cb,
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self._confirm_exit()
            return
        if event.key in ("up", "down") and self._step == 1:
            event.stop()
            self._move_auth(-1 if event.key == "up" else 1)
            return
        if event.key in ("up", "down") and self._step == 2:
            from tui.editors.model import ModelEditor
            ed = self.query(ModelEditor)
            if ed and ed.first().models_loaded and ed.first().has_models:
                event.stop()
                ed.first().move(-1 if event.key == "up" else 1)
                return
        if event.key in ("up", "down") and self._step == 3:
            event.stop()
            self._move_mcp(-1 if event.key == "up" else 1)
            return
        if event.key == "space" and self._step == 3:
            event.stop()
            self._toggle_mcp()
            return
        if event.key == "enter":
            event.stop()
            if self._step == 0:
                self._submit_address()
            elif self._step == 1:
                self._submit_auth()
            elif self._step == 2:
                self._submit_model()
            elif self._step == _LAST_STEP:
                self._commit()
            else:
                self._advance()
