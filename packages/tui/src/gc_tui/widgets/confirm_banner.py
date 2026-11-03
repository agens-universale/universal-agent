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

from gc_core.security.permission_store import is_command_key, remembers_similar
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from gc_tui.palette import ACCENT, BG, BRIGHT, DEEP_DIM, DIM, RUN_COLOR
from gc_tui.widgets.reserved_hint import ReservedHint

# Глиф защитного шлюза (caution). НЕ "?" — это не вопрос агента, а security-gate.
# Width-safe-кандидат; если ⚠ в целевом терминале рендерится шире 1 ячейки —
# заменить на "▲" или "!" (единственная точка правки).
GUARD_GLYPH = "⚠"

# Payload as-is in the scroll body. Only a pathological safety net (agent can
# dump huge blobs into exec_python) — normal scripts/curls are never cut.
_PAYLOAD_SAFETY_CHAR_LIMIT = 100_000

# Rows reserved for docked options (padding-top spacer + 4 choice lines).
_OPTIONS_RESERVED_ROWS = 5


def build_guard_label(
    title: str,
    via: str | None = None,
    *,
    glyph: str = GUARD_GLYPH,
    glyph_style: str = RUN_COLOR,
) -> Text:
    """Заголовок шлюза: янтарный глиф ⚠ + bright-титул. '?' остаётся
    в самой строке вопроса ('Разрешить …?'), не в заголовке.
    via — метка субагента (child HITL), например `via subagent_worker`.
    glyph / glyph_style — для баннеров-наследников со своим значком."""
    t = Text()
    if glyph:  # пустой глиф — заголовок без значка (баннер плана)
        t.append(
            f"{glyph}  ", style=glyph_style
        )  # двойной пробел: ⚠ широкий, одиночный «съедается»
    label = f"{title} · via {via}" if via else title
    t.append(label, style=BRIGHT)
    return t


#: Разовые ответы — одни во всех видах вопроса.
_ALLOW_ONCE = ("[O]", "Разрешить разово", "allow_once")
_DENY_ONCE = ("[T]", "Запретить разово", "deny_once")

# Варианты 2×2: (keycap, label, choice_id).
# 0 — Y allow_remember, 1 — O allow_once, 2 — N deny_remember, 3 — T deny_once.
# «И подобные в сессии» = soft signal судье через insession_examples (не hard allow/deny класса).
# Только в edit: там ответ читает судья команд.
_OPTIONS = [
    ("[Y]", "Разрешить команду и подобные в сессии", "allow_remember"),
    _ALLOW_ONCE,
    ("[N]", "Запретить команду и подобные в сессии", "deny_remember"),
    _DENY_ONCE,
]

# Вопрос про команду вне edit: судьи команд нет, «в сессии» — ровно эта команда.
_OPTIONS_EXACT = [
    ("[Y]", "Разрешить эту команду в сессии", "allow_remember"),
    _ALLOW_ONCE,
    ("[N]", "Запретить эту команду в сессии", "deny_remember"),
    _DENY_ONCE,
]

# Те же ответы для вопроса НЕ про команду (запись, чтение, адрес, MCP, скрипт):
# «команды» там нет, «подобных» тоже — запоминается ровно это действие.
# Клавиши, порядок и идентификаторы совпадают с _OPTIONS.
_OPTIONS_PLAIN = [
    ("[Y]", "Разрешить в этой сессии", "allow_remember"),
    _ALLOW_ONCE,
    ("[N]", "Запретить в этой сессии", "deny_remember"),
    _DENY_ONCE,
]


def options_for(
    decision_key: str | None, permission_mode: str = "edit"
) -> list:
    """Подписи ответов под вид вопроса. «И подобные» — ровно там, где ответ
    «в сессии» уйдёт судье команд подсказкой (`remembers_similar`): одно
    правило ядра на подпись и на сам механизм."""
    if remembers_similar(decision_key, permission_mode):
        return _OPTIONS
    if is_command_key(decision_key):
        return _OPTIONS_EXACT
    return _OPTIONS_PLAIN


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


def _render_options(
    selected: int, options: list | None = None, hint: str | None = None
) -> Text:
    """Вертикальный рендер вариантов; выбранный — teal-«кнопка», прочие — keycap accent + текст dim."""
    t = Text()
    for idx, (key, label, _choice, *rest) in enumerate(options or _OPTIONS):
        if idx > 0:
            t.append("\n")
        prefix = "▸ " if idx == selected else "  "
        if idx == selected:
            # Пояснение варианта (четвёртый элемент) видно только у выделенного
            # и стоит в его же строке: отдельной строкой оно читалось бы как
            # ещё один вариант.
            detail = f" — {rest[0]}" if rest and rest[0] else ""
            t.append(
                f"{prefix}{key} {label}{detail} ",
                style=f"bold {BG} on {ACCENT}",
            )
        else:
            t.append(prefix)
            t.append(f"{key}", style=ACCENT)
            t.append(f" {label}", style=DIM)
    if hint:
        # Пустая строка отделяет подсказку от вариантов: она не пункт списка.
        t.append(f"\n\n  {hint}", style=DEEP_DIM)
    return t


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
        decision_key: str | None = None,
        permission_mode: str = "edit",
        options: list | None = None,
        glyph: str = GUARD_GLYPH,
        glyph_style: str = RUN_COLOR,
        hint: str | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("classes", "")
        kwargs["classes"] = ("confirm-banner " + kwargs["classes"]).strip()
        super().__init__(**kwargs)
        self.banner_title = banner_title
        self.question = question
        self.fields = list(fields or [])
        self.via = via
        # Варианты — данные (клавиша, подпись, значение): наследник со своим
        # набором (одобрение плана) передаёт их сюда и получает тот же рендер,
        # выделение и выбор.
        self.options = (
            list(options)
            if options
            else options_for(decision_key, permission_mode)
        )
        self.glyph = glyph
        self.glyph_style = glyph_style
        # Строка под вариантами: что делает клавиша, которой нет в списке.
        self.hint = hint
        self.selected: int = 0  # 0 = Y/affirmative по умолчанию
        self._options_static: ReservedHint | None = None

    @property
    def desired_height(self) -> int:
        """Честная высота контента + рамка (для cap/trace chrome)."""
        body_lines = 2 if self.question else 1  # title, question
        for label, value in self.fields:
            if _is_payload_label(label):
                text = _fmt_payload_value(value)
            else:
                text = _fmt_field_value(value)
            if not text:
                continue
            body_lines += text.count("\n") + 1
        return body_lines + self._options_rows() + 2  # +2 border

    def _options_rows(self) -> int:
        """Ряды блока вариантов: отступ сверху, варианты, подсказка с зазором;
        с шириной — по переносу самого длинного выбора (`ReservedHint`)."""
        rows = 1 + len(self.options) + (2 if self.hint else 0)
        if self._options_static is not None:
            rows = max(rows, self._options_static.reserved_height() or 0)
        return max(_OPTIONS_RESERVED_ROWS, rows)

    def _options_variants(self) -> list[str]:
        """Блок вариантов при каждом выбранном — резерв высоты."""
        return [
            _render_options(i, self.options, self.hint).plain
            for i in range(len(self.options))
        ]

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
                build_guard_label(
                    self.banner_title,
                    via=self.via,
                    glyph=self.glyph,
                    glyph_style=self.glyph_style,
                ),
                classes="cb-label",
                markup=False,
            )
            if self.question:
                yield Static(
                    self.question, classes="cb-question", markup=False
                )
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
        # Пояснение есть только у выделенного варианта, и в узком окне его
        # строка переносится: высота — под самый длинный выбор (§9).
        opts = ReservedHint(
            _render_options(self.selected, self.options, self.hint),
            variants=self._options_variants,
            classes="cb-options",
        )
        self._options_static = opts
        yield opts

    def selected_choice(self) -> str:
        """Значение выделенного варианта."""
        return self.options[self.selected][2]

    def choice_for_key(self, key: str) -> str | None:
        """Значение варианта по его клавише (`[1]` → «1»); None — не вариант."""
        for keycap, _label, choice, *_rest in self.options:
            if keycap.strip("[]").lower() == str(key).lower():
                return choice
        return None

    def move_selection(self, delta: int) -> None:
        """Сдвинуть выделение на delta (-1 = вверх, +1 = вниз)."""
        self.selected = max(
            0, min(len(self.options) - 1, self.selected + delta)
        )
        if (
            self._options_static is not None
            and self._options_static.is_mounted
        ):
            self._options_static.update(
                _render_options(self.selected, self.options, self.hint)
            )
