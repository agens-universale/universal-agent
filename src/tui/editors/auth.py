"""Редактор авторизации: hand-rolled опции ●/○-состояний + PasteInput токена
под пунктом «Bearer-токен» (password). «Пропустить» — действие (не состояние),
доступно только при allow_skip=True (онбординг; /settings его не показывает)."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, Static

import onboarding
from onboarding_fields import AUTH_OPTIONS
from tui.palette import ACCENT, DEEP_DIM
from tui.widgets.paste_input import PasteInput


def active_option_index(current_header: str | None) -> int | None:
    """Индекс ДЕЙСТВУЮЩЕГО варианта по current_header:
    0 = Bearer-токен, 1 = без авторизации ("" — пустой заголовок),
    None = не выбрано (онбординг: None) / не задано (сентинел).
    /settings передаёт "" когда токена нет → «без авторизации» подсвечено ●."""
    if current_header is None or current_header == onboarding.TOKEN_SENTINEL:
        return None
    return 1 if current_header == "" else 0


class AuthEditor(Vertical):
    def __init__(self, *, current_header: str | None, token_raw: str = "",
                 token_id: str = "onb-token", allow_skip: bool = False,
                 framed: bool = False) -> None:
        super().__init__(classes="onb-dyn")
        self._current_header = current_header
        self._active = active_option_index(current_header)
        self._idx = self._active if self._active is not None else 0
        self._token_val = token_raw
        self._token_id = token_id
        self._framed = framed
        # Исходное состояние (idx, токен) — эталон для dirty-проверки /settings.
        self._initial = (self._idx, (token_raw or "").strip())
        self._options = list(AUTH_OPTIONS[:2]) + (
            [AUTH_OPTIONS[2]] if allow_skip else [])

    def _render_option(self, i: int, *, input_empty: bool):
        cursor = "▸ " if i == self._idx else "  "
        mark = ("● " if i == self._active else "○ ") if i in (0, 1) else "  "
        t = Text()
        t.append(cursor, style=ACCENT)          # курсор teal, как в меню
        t.append(mark + self._options[i])
        if i == 0 and self._active == 0 and input_empty and self._current_header:
            # Действующее значение: литерал — маской, ссылка ${ENV:VAR} — как ссылка со статусом.
            t.append("   " + onboarding.display_secret(self._current_header), style=DEEP_DIM)
        return t

    def compose(self) -> ComposeResult:
        input_empty = not self._token_val
        spaced = " set-spaced" if self._framed else ""
        for i in range(len(self._options)):
            cls = "onb-option onb-dyn" + spaced + (" selected" if i == self._idx else "")
            yield Static(self._render_option(i, input_empty=input_empty), classes=cls)
            if i == 0:
                tcls = "auth-nested onb-dyn" + (" set-framed" if self._framed else "")
                yield PasteInput(value=self._token_val, placeholder="токен (скрыт)",
                                 password=True, id=self._token_id, classes=tcls)

    def on_mount(self) -> None:
        tok = self.query_one(Input)
        tok.display = self._idx == 0
        if self._idx == 0:
            tok.focus()

    def _refresh_options(self) -> None:
        input_empty = not self.query_one(Input).value.strip()
        for i, w in enumerate(self.query(".onb-option")):
            w.update(self._render_option(i, input_empty=input_empty))
            (w.add_class if i == self._idx else w.remove_class)("selected")

    def move(self, delta: int) -> None:
        for e in self.query("#onb-auth-error"):
            e.remove()
        self._idx = (self._idx + delta) % len(self._options)
        self._refresh_options()
        tok = self.query_one(Input)
        tok.display = self._idx == 0
        if self._idx == 0:
            tok.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != self._token_id:
            return
        self._refresh_options()

    def peek(self) -> tuple[int, str]:
        """(idx, raw токен-текст) без сайд-эффектов — для dirty-проверки."""
        return self._idx, self.query_one(Input).value.strip()

    def is_dirty(self) -> bool:
        """Текущий выбор/токен отличается от исходного (dirty-маркер /settings)."""
        try:
            return self.peek() != self._initial
        except NoMatches:            # поле ещё не смонтировано (первый тик)
            return False

    def submit(self) -> tuple[bool, str | None, str]:
        """(ok, Authorization|None, raw). ok=False → шаг не продвигать (ошибка внутри).
        Bearer+токен → задать; Bearer+пусто и действующий токен есть → не менять;
        Bearer+пусто без действующего токена → «введите токен»; без авторизации → "";
        пропустить → None (отложить)."""
        for e in self.query("#onb-auth-error"):
            e.remove()
        if self._idx == 0:
            raw = self.query_one(Input).value.strip()
            if raw:
                return True, onboarding.authorization_header(raw), raw
            if self._active == 0:                 # действующий Bearer-токен остаётся
                return True, None, ""
            self.mount(Static("введите токен", id="onb-auth-error",
                              classes="onb-warn onb-dyn"))
            return False, None, ""
        if self._idx == 1:
            return True, "", ""
        return True, None, ""
