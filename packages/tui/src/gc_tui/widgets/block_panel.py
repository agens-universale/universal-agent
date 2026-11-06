"""BlockPanel — блок трейса (код или таблица): шапка с кнопкой + селектируемое тело.

Устройство:
    BlockPanel.{code-block-panel|table-block}
        Horizontal.block-header
            Static.block-label   — язык, если он что-то сообщает
            Static.block-copy    — кнопка ❐ / ✓ после копирования
        Static.block-body        — rich.Text: код или таблица

Шапка невидима для выделения: ALLOW_SELECT=False убирает её из обхода при drag,
get_selection()->None — при text_select_all(), который потомков не фильтрует
(Screen._select_all_in_widget берёт widget.query("*")). Без обоих в буфер
попадало бы «python   ❐» перед кодом.

Тело таблицы зависит от ширины, поэтому пересобирается на resize; тело кода —
нет (перенос выключен через CSS text-wrap: nowrap).
"""

from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from gc_tui.widgets.click_model import ClickModelMixin, opens_event
from gc_tui.widgets.no_select import NoSelectStatic

# Глиф кнопки копирования — оба варианта «наложенные квадраты», шириной в одну
# колонку. `⧉` (Misc Mathematical Symbols-B) читается лучше, но его нет в шрифтах
# консоли Windows; `❐` (Dingbats) покрывается ими наравне с `✓`, которую проект
# уже использует. Какой из них отдать, решает terminal_caps: см. copy_glyph().
COPY_GLYPH_RICH = "⧉"
COPY_GLYPH_SAFE = "❐"
DONE_GLYPH = "✓"
COPY_FEEDBACK_S = 1.5


def copy_glyph() -> str:
    """Глиф кнопки для текущего терминала: `⧉` там, где он точно отрисуется."""
    from gc_tui.services.terminal_caps import supports_font_fallback

    return COPY_GLYPH_RICH if supports_font_fallback() else COPY_GLYPH_SAFE


class CopyFeedbackMixin:
    """Отклик кнопки ❐ и открытие события по Ctrl+клику — общие у панели,
    блока правки и строки вызова с правкой. Хозяин держит ``_restore_timer``
    и ``_event_no``."""

    def _flash_copied(self) -> None:
        self.query_one(".block-copy", Static).update(DONE_GLYPH)
        if self._restore_timer is not None:
            self._restore_timer.stop()
        self._restore_timer = self.set_timer(
            COPY_FEEDBACK_S, self._restore_copy_glyph
        )

    def _restore_copy_glyph(self) -> None:
        self._restore_timer = None
        try:
            self.query_one(".block-copy", Static).update(copy_glyph())
        except Exception:
            pass  # блок уже размонтирован — восстанавливать нечего

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from gc_tui.messages import OpenEventRequest

        return OpenEventRequest(self._event_no)


class BlockPanel(CopyFeedbackMixin, ClickModelMixin, Vertical):
    """Код-панель или таблица трейса.

    Конструируется фабриками `for_code` / `for_table` — они задают тело, метку
    шапки и payload кнопки. Прямой конструктор не используется.
    """

    ALLOW_SELECT = False  # выделяется тело, а не панель целиком
    FOCUS_ON_CLICK = (
        False  # выделение ставит on_click, и только за клик по телу
    )

    # Отступы задаются ОДНИМ shorthand-правилом на селектор: Textual не мёржит
    # margin-*/padding-* из разных правил — второе затирает весь box.
    # `.block-header` margin-bottom — воздух между шапкой и содержимым блока;
    # `.block-copy` padding-right — чтобы кнопка не липла к правому краю панели.
    # `.no-lang .block-header` (Д9) — без метки языка воздух под шапкой не нужен:
    # строка с ⧉ и так пуста наполовину, код должен начинаться сразу под ней.
    DEFAULT_CSS = """
    BlockPanel { height: auto; width: 1fr; }
    BlockPanel .block-header { height: 1; width: 1fr; margin: 0 0 1 0; }
    BlockPanel.no-lang .block-header { margin: 0 0 0 0; }
    BlockPanel .block-label { width: 1fr; height: 1; }
    BlockPanel .block-copy { width: auto; height: 1; padding: 0 1 0 0; }
    BlockPanel .block-body { width: 1fr; height: auto; text-wrap: nowrap; text-overflow: clip; }
    /* Код переносится, таблица — нет: у таблицы строка это ряд сетки, и перенос
       её разрушает (её вместо этого подгоняет под ширину `table_to_text`).
       Перенос отдан Textual, а не сделан вручную: свой рез вставил бы в текст
       НАСТОЯЩИЕ разрывы, и выделение мышью отдавало бы строку разорванной —
       а контракт `highlight_code`: `.plain` равен исходному коду. Цена —
       перенос словесный: слишком длинное слово Textual сложит сам
       (`text-overflow: fold`), но короткое первое слово может уехать на ряд
       одно. Лимита строк у блока кода нет, поэтому точный счёт рядов, ради
       которого дифф переносит себя сам, здесь не нужен. */
    BlockPanel.code-block-panel .block-body { text-wrap: wrap; text-overflow: fold; }
    """

    def __init__(
        self,
        *,
        label: str,
        copy_payload: str,
        event_no: int,
        focusable: bool,
        classes: str,
    ) -> None:
        super().__init__(classes=classes)
        self._label = label
        self._copy_payload = copy_payload
        self._event_no = event_no
        self.can_focus = focusable
        self._restore_timer = None
        # Тело и его исходник ставят фабрики; объявлены здесь, чтобы панель,
        # собранная в обход фабрики, не падала на `AttributeError` в обработчике
        # resize вместо внятного пустого тела.
        self._body_text = None
        self._table_source = None

    # --- фабрики -----------------------------------------------------------

    @classmethod
    def for_code(
        cls,
        content: str,
        lang: str | None,
        *,
        event_no: int,
        focusable: bool = False,
        indent: bool = False,
    ) -> BlockPanel:
        """Код: метка — язык (если известен), копия — чистый код без обрамления."""
        from gc_tui.services.code_render import highlight_code, normalize_code

        classes = "block-panel code-block-panel"
        if indent:
            classes += " code-indent"
        if not lang:
            classes += (
                " no-lang"  # Д9: без метки языка — без воздуха под шапкой
            )
        panel = cls(
            label=lang or "",
            copy_payload=normalize_code(content),
            event_no=event_no,
            focusable=focusable,
            classes=classes,
        )
        panel._body_text = highlight_code(content, lang)
        panel._table_source = None
        return panel

    @classmethod
    def for_table(
        cls,
        headers: list[str],
        rows: list[list[str]],
        aligns: list[str],
        *,
        event_no: int,
        focusable: bool = False,
        indent: bool = False,
    ) -> BlockPanel:
        """Таблица: метка пустая, копия — markdown (его и вставляют, не рамки)."""
        from gc_tui.services.markdown_render import table_block_to_markdown

        classes = "block-panel table-block"
        if indent:
            classes += " table-indent"
        panel = cls(
            label="",
            copy_payload=table_block_to_markdown(headers, rows, aligns),
            event_no=event_no,
            focusable=focusable,
            classes=classes,
        )
        panel._body_text = None
        panel._table_source = (headers, rows, aligns)
        return panel

    # --- сборка ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(classes="block-header"):
            yield NoSelectStatic(
                self._label, classes="block-label", markup=False
            )
            yield NoSelectStatic(
                copy_glyph(), classes="block-copy trace-action", markup=False
            )
        yield Static("", classes="block-body", markup=False)

    def on_mount(self) -> None:
        self._refresh_body()
        self._refresh_header()

    def on_resize(self, event: events.Resize) -> None:
        if self._table_source is not None:
            self._refresh_body()
        self._refresh_header()

    def _refresh_body(self) -> None:
        body_query = self.query(".block-body")
        if not body_query:
            # Панель смонтировали и тут же удалили синхронно (retry-коллапс
            # submit_solution) — Mount/Resize всё равно доходят до обработчика,
            # но compose() своих детей отработать не успел. Тела ещё/уже нет —
            # обновлять нечего.
            return
        body = body_query.first(Static)
        if self._table_source is not None:
            from gc_tui.services.table_render import table_to_text

            headers, rows, aligns = self._table_source
            width = max(self.size.width - 2, 10)  # padding панели
            self._body_text = table_to_text(headers, rows, aligns, width=width)
        body.update(self._body_text)

    def _refresh_header(self) -> None:
        """Метка языка скрывается при нехватке ширины, кнопка ❐ — никогда (§3.3).

        Ширину не отдаём через display:none — тогда `❐` потеряла бы правое
        прижатие (метка держит его через block-label { width: 1fr }). Вместо
        этого метка очищается текстом — колонка остаётся, кнопка на месте.
        """
        if not self._label:
            return
        label_query = self.query(".block-label")
        if not label_query:
            return
        label = label_query.first(Static)
        available = max(self.size.width - 2, 0)  # минус padding панели (0 1)
        fits = available >= len(self._label) + 2  # метка + пробел перед ❐
        label.update(self._label if fits else "")

    # --- взаимодействие ----------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        """Кнопка ❐ и двойной клик копируют блок; остальное — общая клик-модель.

        Ctrl+клик по кнопке пропускает эту ветку: спека §2 обещает открытие
        события по Ctrl+клику с любой точки блока, а не только копирование.

        `prevent_default()` на входе: Textual вызывает ВСЕ одноимённые хэндлеры
        по MRO, и без него ClickModelMixin.on_click отработал бы вторым —
        выделял бы панель за клик по кнопке и постил бы ВТОРОЙ запрос открытия
        по Ctrl+клику. Миксин зовём сами, явным super() там, где он нужен.
        """
        event.prevent_default()
        widget = getattr(event, "widget", None)
        if (
            not opens_event(event)
            and widget is not None
            and widget.has_class("block-copy")
        ):
            event.stop()
            self._copy_block()
            return
        if event.chain == 2:
            event.stop()
            self.query_one(".block-body", Static).text_select_all()
            self._copy_block()
            return
        super().on_click(event)

    def _copy_block(self) -> None:
        self.post_message(_copy_request(self._copy_payload))
        self._flash_copied()


def _copy_request(payload: str) -> Message:
    from gc_tui.messages import CopyBlockRequest

    return CopyBlockRequest(payload)
