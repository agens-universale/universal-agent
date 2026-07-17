"""Редактор подключения: адрес + токен на одном экране, единый курсор.
Строки: 0=Адрес (input), 1=Bearer-токен (option + input), 2=без авторизации (option).
Секрет НЕ предзаполняется в поле токена — текущий показан подписью-маской у варианта."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, Static

import onboarding
from tui.palette import ACCENT, DEEP_DIM
from tui.widgets.paste_input import PasteInput

_URL_ERROR = "нужны http(s):// и хост"
_URL_EMPTY = "введите адрес"
_TOKEN_EMPTY = "введите токен"
_AUTH_OPTIONS = ("Bearer-токен", "без авторизации (токен не нужен)")


def _auth_active(header: str | None) -> int | None:
    """Действующий вариант авторизации из сырого Authorization: 0=Bearer, 1=без, None=не задано."""
    if header is None or header == onboarding.TOKEN_SENTINEL:
        return None
    return 1 if header == "" else 0


class ConnectionEditor(Vertical):
    def __init__(self, *, url_current: str, auth_header: str | None,
                 framed: bool = False) -> None:
        super().__init__(classes="onb-dyn")
        self._url_current = url_current or ""
        self._auth_header = auth_header
        self._auth_active = _auth_active(auth_header)
        self._framed = framed
        self._idx = 0                       # курсор: 0=url, 1=bearer, 2=без авторизации
        # эталон dirty: (url в файле, выбран ли Bearer, пустой токен)
        self._initial_url = self._url_current

    # --- рендер ---
    def _token_placeholder(self) -> str:
        """placeholder ≠ значение: маска действующего токена (литерал) или ссылка (не секрет).
        В файл НЕ пишется — печать заменяет, пустое поле = «не менять». Так поле визуально
        не пустое, но резолвнутый секрет/ссылка в редактируемое содержимое не заезжает."""
        if self._auth_active == 0 and self._auth_header:
            return onboarding.display_secret(self._auth_header)
        return "введите токен"

    def _opt_cls(self, row_i: int) -> str:
        spaced = " set-spaced" if self._framed else ""
        sel = " selected" if self._idx == row_i else ""
        return "onb-option onb-dyn" + spaced + sel

    def _option_text(self, opt_i: int) -> Text:
        row_i = opt_i + 1                    # опции идут после строки адреса
        cursor = "▸ " if self._idx == row_i else "  "
        mark = "● " if opt_i == self._auth_active else "○ "
        t = Text()
        t.append(cursor, style=ACCENT)
        t.append(mark + _AUTH_OPTIONS[opt_i])
        if opt_i == 0 and self._auth_active == 0 and self._auth_header:
            # Действующий токен рядом с вариантом: литерал — маской, ссылка — как есть.
            t.append("   " + onboarding.display_secret(self._auth_header), style=DEEP_DIM)
        return t

    def compose(self) -> ComposeResult:
        icls = "onb-dyn set-framed" if self._framed else "onb-dyn"
        yield Static("Адрес API-сервера", classes="onb-hint onb-dyn")
        yield PasteInput(value=self._url_current, placeholder="напр. https://host/v1/chat/completions",
                         id="conn-url", classes=icls)
        yield Static("Авторизация", classes="onb-hint onb-dyn set-group-gap")
        yield Static(self._option_text(0), classes=self._opt_cls(1), id="conn-opt-0")
        yield PasteInput(value="", placeholder=self._token_placeholder(), password=True,
                         id="conn-token", classes="auth-nested onb-dyn"
                         + (" set-framed" if self._framed else ""))
        yield Static(self._option_text(1), classes=self._opt_cls(2), id="conn-opt-1")

    def on_mount(self) -> None:
        self._sync_visibility()
        self.query_one("#conn-url", Input).focus()

    # --- навигация ---
    def _sync_visibility(self) -> None:
        # токен-поле видно только когда курсор на Bearer (idx 1) И Bearer — активный/выбранный
        self.query_one("#conn-token", Input).display = self._idx == 1

    def move(self, delta: int) -> None:
        # Навигация двигает только курсор ▸ и подсветку; метка ● (действующий вариант)
        # не едет за курсором — выбор фиксирует submit()/peek() по позиции (как «Модель»).
        self._idx = (self._idx + delta) % 3
        self._refresh()
        self._sync_visibility()
        target = {0: "#conn-url", 1: "#conn-token", 2: None}[self._idx]
        if target is not None:
            self.query_one(target, Input).focus()

    def _refresh(self) -> None:
        for opt_i, wid in ((0, "#conn-opt-0"), (1, "#conn-opt-1")):
            row_i = opt_i + 1
            w = self.query_one(wid, Static)
            w.update(self._option_text(opt_i))
            (w.add_class if self._idx == row_i else w.remove_class)("selected")   # подсветка выбранной строки

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh()

    def focus_first(self) -> None:
        self._idx = 0
        self._refresh()
        self._sync_visibility()
        self.query_one("#conn-url", Input).focus()

    # --- контракт для экрана ---
    def _selected_auth(self) -> int | None:
        """Вариант авторизации под курсором (его зафиксирует Enter): idx 1→Bearer(0),
        idx 2→без авторизации(1); курсор на адресе (idx 0)→None — авторизацию не трогаем."""
        return self._idx - 1 if self._idx in (1, 2) else None

    def peek(self) -> tuple[str, int, str]:
        """(url_field, auth_idx, token_raw) — без сайд-эффектов, для probe и dirty.
        auth_idx — выбор под курсором; на адресе — действующий вариант из конфига."""
        url = self.query_one("#conn-url", Input).value.strip()
        token = self.query_one("#conn-token", Input).value.strip()
        auth_idx = self._selected_auth()
        if auth_idx is None:
            auth_idx = self._auth_active if self._auth_active is not None else 0
        return url, auth_idx, token

    def is_dirty(self) -> bool:
        try:
            url, _auth_idx, token = self.peek()
        except NoMatches:
            return False
        if url != self._initial_url:
            return True
        active = self._auth_active if self._auth_active is not None else 0
        sel = self._selected_auth()
        if sel is not None and sel != active:
            return True                      # под курсором выбран не действующий вариант
        if sel == 0 and token:
            return True                      # Bearer выбран и введён новый токен
        return False

    def submit(self) -> tuple[str | None, str | None, str | None]:
        """(url|None, header|None, error|None). None-значение = не менять."""
        url, _auth_idx, token = self.peek()
        # адрес
        out_url: str | None = None
        if url != self._initial_url:
            if not url:
                return None, None, _URL_EMPTY
            if not onboarding._valid_endpoint_url(url):
                return None, None, _URL_ERROR
            out_url = url
        elif not url:                        # был пуст и остался пуст, но адрес обязателен
            return None, None, _URL_EMPTY
        # авторизация — по варианту под курсором
        sel = self._selected_auth()
        active = _auth_active(self._auth_header)
        header: str | None = None
        if sel == 1:                         # «без авторизации»
            if active != 1:
                header = ""                  # снять действующую авторизацию
        elif sel == 0:                       # Bearer
            if token:
                header = onboarding.authorization_header(token)
            elif active != 0:                # выбрали Bearer, а токена нет ни в поле, ни действующего
                return None, None, _TOKEN_EMPTY
            # active == 0, поле пусто → действующий токен остаётся (header None)
        return out_url, header, None
