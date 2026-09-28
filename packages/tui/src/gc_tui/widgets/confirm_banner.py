"""ConfirmBanner — служебное подтверждение, sticky-блок над полем ввода.

Отличается от AskHumanBanner назначением (security/служебный confirm, не вопрос
агента по задаче) и набором вариантов. Трейс остаётся виден; поле ввода прячет
вызывающая сторона (см. wiring).

fields — структурированные детали решения (Tool/Path/Command/Reason/Risk),
которые приходят из confirm-порта (request_confirm). Рендерятся строками между
вопросом и вариантами — чтобы баннер был информативен (CLI-промпт их печатает,
здесь — тоже).

Layout: скроллируемое тело (команда/код как есть) + Y/O/N/T с ``dock: bottom``.
При ``max_height`` от ``_cap_sticky_banner`` main.py выставляет ``height=cap``,
иначе Textual клипает box, а дети layout'ятся полной высотой и опции уезжают.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from gc_tui.palette import ACCENT, BG, BRIGHT, DIM, RUN_COLOR

# Глиф защитного шлюза (caution). НЕ "?" — это не вопрос агента, а security-gate.
# Width-safe-кандидат; если ⚠ в целевом терминале рендерится шире 1 ячейки —
# заменить на "▲" или "!" (единственная точка правки).
GUARD_GLYPH = "⚠"

# Payload as-is in the scroll body. Only a pathological safety net (agent can
# dump huge blobs into exec_python) — normal scripts/curls are never cut.
_PAYLOAD_SAFETY_CHAR_LIMIT = 100_000

# Rows reserved for docked options (padding-top spacer + 4 choice lines).
_OPTIONS_RESERVED_ROWS = 5


def build_guard_label(title: str, via: str | None = None) -> Text:
    """Заголовок шлюза: янтарный глиф ⚠ + bright-титул. '?' остаётся только
    в самой строке вопроса ('Разрешить …?'), не в заголовке.
    via — метка субагента (child HITL), например `via subagent_worker`."""
    t = Text()
    t.append(
        f"{GUARD_GLYPH}  ", style=RUN_COLOR
    )  # двойной пробел: ⚠ широкий, одиночный «съедается»
    label = f"{title} · via {via}" if via else title
    t.append(label, style=BRIGHT)
    return t


# Варианты 2×2: (keycap, label, choice_id).
# 0 — Y allow_remember, 1 — O allow_once, 2 — N deny_remember, 3 — T deny_once.
# «И подобные в сессии» = soft signal судье через insession_examples (не hard allow/deny класса).
_OPTIONS = [
    ("[Y]", "Разрешить команду и подобные в сессии", "allow_remember"),
    ("[O]", "Разрешить разово", "allow_once"),
    ("[N]", "Запретить команду и подобные в сессии", "deny_remember"),
    ("[T]", "Запретить разово", "deny_once"),
]

# Метки primary payload-поля — рендерится первым, multiline, cb-payload class.
_PAYLOAD_LABELS = frozenset(
    {
        "команда",
        "код",
        "command",
        "code",
        "path",
        "url",
        "путь",
    }
)


def _is_payload_label(label: str) -> bool:
    return str(label).strip().casefold() in _PAYLOAD_LABELS


def _fmt_payload_value(
    value: object,
    *,
    safety_limit: int = _PAYLOAD_SAFETY_CHAR_LIMIT,
) -> str:
    """Full multiline payload for confirm body (scroll handles overflow).

    Newlines preserved. No soft 500-char / line caps — user must see the real
    command/script. Only ``safety_limit`` clips pathological megabyte blobs.
    """
    if isinstance(value, (list, tuple)):
        text = "\n".join(str(x) for x in value)
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return text
    if safety_limit > 0 and len(text) > safety_limit:
        omitted = len(text) - safety_limit
        return f"{text[:safety_limit]}\n… (+{omitted} chars)"
    return text


def _fmt_field_value(value: object, *, limit: int = 200) -> str:
    """Однострочное компактное значение мета-поля: списки — через запятую,
    переводы строк схлопнуты, длинное — усечено."""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(x) for x in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_options(selected: int) -> Text:
    """Вертикальный рендер вариантов; выбранный — teal-«кнопка», прочие — keycap accent + текст dim."""
    t = Text()
    for idx, (key, label, _choice) in enumerate(_OPTIONS):
        if idx > 0:
            t.append("\n")
        prefix = "▸ " if idx == selected else "  "
        if idx == selected:
            t.append(f"{prefix}{key} {label} ", style=f"bold {BG} on {ACCENT}")
        else:
            t.append(prefix)
            t.append(f"{key}", style=ACCENT)
            t.append(f" {label}", style=DIM)
    return t


def choice_for(selected: int) -> str:
    """Маппинг индекса выделенного варианта → строка выбора (4-way)."""
    if 0 <= selected < len(_OPTIONS):
        return _OPTIONS[selected][2]
    return "deny_remember"


class ConfirmBanner(Vertical):
    DEFAULT_CSS = ""  # стили в styles.tcss (.confirm-banner)
    # Фокус на баннере, пока висит confirm: иначе Tab/клик по ToolCallRow
    # перехватывает Enter (открывает detail) до Screen.on_key.
    can_focus = True
    # Сигнал для main._cap_sticky_banner: при обрезке нужен height=cap
    # (docked options + scroll body), иначе max_height клипает box впустую.
    needs_height_when_capped = True

    def __init__(
        self,
        *,
        banner_title: str,
        question: str,
        fields: list | None = None,
        via: str | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("classes", "")
        kwargs["classes"] = ("confirm-banner " + kwargs["classes"]).strip()
        super().__init__(**kwargs)
        self.banner_title = banner_title
        self.question = question
        self.fields = list(fields or [])
        self.via = via
        self.selected: int = 0  # 0 = Y/affirmative по умолчанию
        self._options_static: Static | None = None

    @property
    def desired_height(self) -> int:
        """Честная высота контента + рамка (для cap/trace chrome)."""
        body_lines = 2  # title, question
        for label, value in self.fields:
            if _is_payload_label(label):
                text = _fmt_payload_value(value)
            else:
                text = _fmt_field_value(value)
            if not text:
                continue
            body_lines += text.count("\n") + 1
        return body_lines + _OPTIONS_RESERVED_ROWS + 2  # +2 border

    # Клавиши прокрутки тела: решение принимают по полному тексту команды,
    # а колесо мыши есть не у всех.
    _BODY_SCROLL_KEYS = ("pagedown", "pageup", "home", "end")

    def _body(self) -> VerticalScroll | None:
        try:
            return self.query_one(".cb-body", VerticalScroll)
        except Exception:
            return None

    def set_hug_content(self, hug: bool) -> None:
        """Тело тянется на остаток только когда баннер упёрся в потолок.

        ``height: 1fr`` внутри ``auto``-родителя убирает зависимость высоты от
        контента, и баннер всегда занимает максимум. Растягиваем тело лишь
        тогда, когда высота и правда зафиксирована.
        """
        body = self._body()
        if body is not None:
            body.styles.height = "auto" if hug else "1fr"

    def scroll_body(self, key: str) -> bool:
        """Прокрутка тела с клавиатуры. True — клавиша обработана."""
        if key not in self._BODY_SCROLL_KEYS:
            return False
        body = self._body()
        if body is None:
            return False
        if key == "pagedown":
            body.scroll_page_down(animate=False)
        elif key == "pageup":
            body.scroll_page_up(animate=False)
        elif key == "home":
            body.scroll_home(animate=False)
        else:
            body.scroll_end(animate=False)
        return True

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="cb-body"):
            yield Static(
                build_guard_label(self.banner_title, via=self.via),
                classes="cb-label",
                markup=False,
            )
            yield Static(self.question, classes="cb-question", markup=False)
            for label, value in self.fields:
                if _is_payload_label(label):
                    text = _fmt_payload_value(value)
                    css_class = "cb-payload"
                else:
                    text = _fmt_field_value(value)
                    css_class = "cb-field"
                if not text:
                    continue  # пустые поля (напр. Reason="") не засоряют баннер
                yield Static(
                    f"  {label}: {text}", classes=css_class, markup=False
                )
        opts = Static(_render_options(self.selected), classes="cb-options")
        self._options_static = opts
        yield opts

    def move_selection(self, delta: int) -> None:
        """Сдвинуть выделение на delta (-1 = вверх, +1 = вниз)."""
        self.selected = max(0, min(len(_OPTIONS) - 1, self.selected + delta))
        if (
            self._options_static is not None
            and self._options_static.is_mounted
        ):
            self._options_static.update(_render_options(self.selected))
