"""UserReplyContainer — реплика пользователя: маркер `› ` на подложке ленты."""

from __future__ import annotations

import os
from typing import Literal, NamedTuple, Optional

from gc_core.attachments.model import CLIPBOARD_SOURCE, Attachment
from gc_core.utils.path_display import display_path
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.selection import Selection
from textual.widget import Widget

from gc_tui.palette import ACCENT as _ACCENT
from gc_tui.palette import DIM as _DIM
from gc_tui.palette import ERR as _ERR_RED
from gc_tui.palette import QUIET_REPLY_INK as _QUIET_INK
from gc_tui.palette import QUIET_REPLY_MARK as _QUIET_MARK
from gc_tui.palette import QUIET_REPLY_SKILL as _QUIET_SKILL
from gc_tui.palette import SKILL as _SKILL
from gc_tui.services.image_chips import chip_of, chip_spans
from gc_tui.services.path_display import fit_path
from gc_tui.services.trace_render import (
    LINE_USER_CLASS,
    NESTED_USER_PREFIX,
    USER_PREFIX,
)
from gc_tui.widgets.click_model import ClickModelMixin, confirm_is_active
from gc_tui.widgets.consequence_line import ConsequenceSlot
from gc_tui.widgets.no_select import NoSelectStatic
from gc_tui.widgets.reply_layout import LineSpec, ReplyLayout
from gc_tui.widgets.trace_row import TraceRow

#: Источник картинки из буфера обмена (`Attachment.source`) и его подпись в ленте.
_FROM_CLIPBOARD = "из буфера"

#: Следствие реплики: скилл не найден, сообщение ушло без него (P3 §2.3).
_SKILL_MISSING = "скилл {name} не найден — отправлено без него"

#: Подсказка под последней репликой в очереди (P3 §2.4, §10 ответ 2).
QUEUE_HINT = "в очереди · ↑ — изменить до отправки"


def _image_style(color: Optional[str], att: Attachment) -> Style:
    """Стиль спана картинки: цвет (`None` — цвет текста реплики) + meta `image_id` — по
    ней наведение и клик находят картинку (Textual кладёт стиль ячейки под курсором в
    `event.style`)."""
    return Style(color=color, meta={"image_id": att.id})


def _append_line(
    out: Text,
    line: str,
    images: tuple[Attachment, ...],
    hover_id: Optional[str],
) -> None:
    """Строка реплики: image-плашки — цвета текста, под указателем (`hover_id`) — ACCENT;
    остальное — как есть."""
    pos = 0
    for start, end, att in chip_spans(line, images):
        out.append(line[pos:start])
        out.append(
            line[start:end],
            style=_image_style(_ACCENT if att.id == hover_id else None, att),
        )
        pos = end
    out.append(line[pos:])


def _sub_line(att: Attachment, hovered: bool) -> Text:
    """`└ [Изображение #8] · /полный/путь/скрин.png` (визуальный контракт §3): плашка —
    цвета текста, остальное приглушено; под указателем — вся ACCENT. Путь — целиком:
    так он уходит в копию, а на экране его сокращает `_shown_sub_line`."""
    dim = _image_style(_ACCENT if hovered else _DIM, att)
    source = _FROM_CLIPBOARD if att.source == CLIPBOARD_SOURCE else att.source
    out = Text()
    out.append("└ ", style=dim)
    out.append(
        chip_of(att), style=_image_style(_ACCENT if hovered else None, att)
    )
    out.append(" · ", style=dim)
    out.append(source, style=dim)
    return out


def _shown_sub_line(att: Attachment, width: int) -> Optional[str]:
    """Строка картинки на экране: путь от папки проекта, через `~` или полностью —
    в `width` ячеек (`fit_path`; `0` — без ограничения). `None` — как есть."""
    if att.source == CLIPBOARD_SOURCE:
        return None
    head = f"└ {chip_of(att)} · "
    path = display_path(att.source, os.getcwd(), os.path.expanduser("~"))
    if width > 0:
        path = fit_path(path, width - cell_len(head))
    return head + path


class _ReplyLine(NamedTuple):
    """Строка реплики для раскладки."""

    prefix: Text  # префикс первой строки
    body: Text  # оформленный текст — он же уходит в копию
    indent: int  # отступ строк-продолжений
    shown: Optional[str] = (
        None  # что рисуется вместо текста (`LineSpec.shown`)
    )


def _build_lines(
    text: str,
    images: tuple[Attachment, ...],
    hover_id: Optional[str],
    lead: Text,
    width: int,
) -> list[_ReplyLine]:
    """Строки реплики при ширине `width`: строки сообщения (первая — с `lead`) и строки
    картинок под ними. Всё, что ниже первой строки, начинается под текстом после
    `lead`; строка картинки влезает в одну строку, а если нет — переносится под
    плашку."""
    indent = lead.cell_len
    pad = Text(" " * indent)
    lines: list[_ReplyLine] = []
    for i, line in enumerate(text.split("\n")):
        body = Text()
        _append_line(body, line, images, hover_id)
        lines.append(_ReplyLine(lead if i == 0 else pad, body, indent))
    for att in images:
        lines.append(
            _ReplyLine(
                pad,
                _sub_line(att, att.id == hover_id),
                indent + len("└ "),
                _shown_sub_line(att, max(width - indent, 0)),
            )
        )
    return lines


class _WrappedReply:
    """Реплика, которую переносит своя раскладка (`reply_layout`), а не Textual:
    плашки не рвутся, продолжение — под текстом, выделение копирует текст сообщения
    без префиксов и переносов раскладки. Строки даёт `_reply_lines`."""

    _images: tuple[Attachment, ...] = ()
    _reply_layout: Optional[ReplyLayout] = None

    def _reply_lines(self, width: int) -> list[_ReplyLine]:
        raise NotImplementedError

    def _layout(self, width: int) -> tuple[ReplyLayout, list[_ReplyLine]]:
        lines = self._reply_lines(width)
        specs = tuple(
            LineSpec(
                line.body.plain,
                tuple(
                    (a, b)
                    for a, b, _ in chip_spans(line.body.plain, self._images)
                ),
                line.prefix.cell_len,
                line.indent,
                line.shown,
            )
            for line in lines
        )
        layout = self._reply_layout
        if layout is None or (layout.specs, layout.width) != (specs, width):
            layout = self._reply_layout = ReplyLayout(specs, width)
        return layout, lines

    def render(self) -> Text:
        layout, lines = self._layout(self.content_size.width)
        return layout.render(
            [line.prefix for line in lines], [line.body for line in lines]
        )

    def get_content_height(self, container, viewport, width: int) -> int:
        # Высоту спрашивают до того, как у виджета есть размер, — считаем при `width`.
        return self._layout(width)[0].height if width else 0

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        layout, _ = self._layout(self.content_size.width)
        return layout.copy(selection.start, selection.end), "\n"


def _image_path(
    images: tuple[Attachment, ...], image_id: str
) -> Optional[str]:
    """Файл картинки реплики по `image_id` из meta спана под курсором (Ctrl+клик)."""
    for att in images:
        if att.id == image_id:
            return att.path
    return None


def _image_id_under(event: events.MouseEvent) -> Optional[str]:
    """Картинка под указателем — meta `image_id` стиля ячейки (кладёт Screen)."""
    return event.style.meta.get("image_id") or None


class _ImageLinksMixin:
    """Картинки реплики как ссылки (визуальный контракт §3): наведение подсвечивает все
    места картинки под указателем — плашки в тексте и её строку под репликой; обычный
    клик по ним открывает файл (`ClickModelMixin._plain_click_image_path`).

    Образец — сводка изменений строки «Выполнил»: засчитывается один короткий клик
    левой кнопкой, нажатый и отпущенный в той же ячейке той же картинки. Протяжка
    выделения, отпущенная на плашке, протяжка внутри плашки и второй клик двойного —
    выделение текста, не открытие. Перерисовка — `_repaint_images` каждой реплики.
    """

    _images: tuple[Attachment, ...] = ()
    _hover_image_id: Optional[str] = None
    _pressed: Optional[tuple[str, object]] = None  # картинка и ячейка нажатия

    def _repaint_images(self) -> None:
        raise NotImplementedError

    def clear_image_hover(self) -> None:
        """Погасить подсветку: окно терминала потеряло фокус (клик открыл картинку в
        просмотрщике) — «указатель ушёл» терминал уже не пришлёт. Зовёт приложение
        (`TuiApp.watch_app_focus`): подписка одна на всё приложение."""
        self._pressed = None
        self._set_image_hover(None)

    def _set_image_hover(self, image_id: Optional[str]) -> None:
        if image_id == self._hover_image_id:
            return
        self._hover_image_id = image_id
        self._repaint_images()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if event.button:
            return  # идёт выделение мышью — не перерисовываем
        # Окно терминала без фокуса: первый клик лишь активирует окно, подсветка
        # обещала бы открытие одним кликом.
        idle = confirm_is_active(self) or not self.app.app_focus
        self._set_image_hover(None if idle else _image_id_under(event))

    def on_leave(self, _event: events.Leave) -> None:
        # Leave всплывает и из дочерних виджетов строки: указатель ещё здесь.
        if self.is_mouse_over:
            return
        self._pressed = None
        self._set_image_hover(None)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        image_id = _image_id_under(event)
        self._pressed = (
            (image_id, event.screen_offset)
            if image_id and event.button == 1 and not event.ctrl
            else None
        )

    def _plain_click_image_path(self, event: events.Click) -> Optional[str]:
        pressed, self._pressed = self._pressed, None
        image_id = _image_id_under(event)
        if (
            pressed is None
            or event.button != 1
            or event.chain != 1
            or pressed != (image_id, event.screen_offset)
        ):
            return None
        return _image_path(self._images, image_id)


_State = Literal["normal", "pending", "queued", "failed"]

#: Цвет маркера `› ` по состоянию; не названное — ACCENT.
_MARKER_COLOR = {"failed": _ERR_RED, "queued": _QUIET_MARK}


class _ReplyBand(_WrappedReply, Widget):
    """Подложка реплики (`.user-band`): текст, картинки и подсветка — у
    реплики-владельца."""

    def __init__(self, owner: UserReplyContainer) -> None:
        super().__init__(classes="user-band")
        self._owner = owner

    @property
    def _images(self) -> tuple[Attachment, ...]:
        return self._owner._images

    def _reply_lines(self, width: int) -> list[_ReplyLine]:
        return self._owner._reply_lines(width)


class UserReplyContainer(_ImageLinksMixin, ClickModelMixin, Widget):
    """Реплика пользователя (спека P3 §2.2): подложка `.user-band` с текстом
    и строками картинок, под ней — строки следствий `└` на фоне ленты
    (`add_consequence`).

    Маркер `› ` — в колонке `●` реплики агента, только на первой строке;
    продолжение — под текстом, в том числе при переносе (`_WrappedReply`).
    Состояния `normal` / `pending` выглядят одинаково; `failed` — маркер
    цветом ошибки; `queued` — отправлена во время хода и ждёт впуска
    ядром (спека P3 §2.4): класс `-queued` (тише подложка и текст), маркер
    `MUTED`, под подложкой — подсказка `QUEUE_HINT` (`set_queue_hint`).
    ``message_id`` / ``client_id`` — что снять из очереди; в поле ввода
    возвращается `plain_text`.

    ``skill`` — запускаемое имя скилла (`skill_input_name`): первым словом
    после маркера, `/имя` цветом `SKILL`; ``skill_error`` — скилл не найден:
    имя цветом текста и следствие `└ ✕ скилл … не найден …` (спека P3 §2.3).
    """

    DEFAULT_CSS = """
    UserReplyContainer .reply-consequence {
        display: none;
        width: 1fr;
        height: auto;
    }
    UserReplyContainer.has-consequence .reply-consequence {
        display: block;
    }
    """
    can_focus = True

    def __init__(
        self,
        text: str,
        *,
        event_no: int = 0,
        state: _State = "normal",
        images: tuple[Attachment, ...] = (),
        skill: str | None = None,
        skill_error: bool = False,
        message_id: str | None = None,
        client_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._text = text
        self.message_id = message_id
        self.client_id = client_id
        self._skill = skill
        self._skill_error = skill_error
        self._event_no = event_no
        self._state: _State = state
        self.set_class(state == "queued", "-queued")
        # Картинки реплики: плашки в тексте и подстроки под ним (визуальный контракт §3).
        self._images: tuple[Attachment, ...] = tuple(images)
        self._consequences: list[Text] = []
        self._band = _ReplyBand(self)
        self._queue_hint = NoSelectStatic(
            QUEUE_HINT, classes="queue-hint", markup=False
        )
        # Следствие реплики в копию выделения не идёт.
        self._consequence_slot = ConsequenceSlot(
            0, selectable=False, classes="reply-consequence"
        )
        if skill is not None and skill_error:
            self.add_consequence(
                Text(_SKILL_MISSING.format(name=skill), style=_ERR_RED)
            )

    def compose(self):
        yield self._band
        yield self._queue_hint
        yield self._consequence_slot

    @property
    def state(self) -> _State:
        return self._state

    @property
    def plain_text(self) -> str:
        """Текст реплики без оформления; скилл — `/имя` первым словом.
        Его же ↑, стоп и конец хода возвращают в поле ввода."""
        if self._skill is None:
            return self._text
        return f"/{self._skill} {self._text}".rstrip()

    @property
    def has_images(self) -> bool:
        return bool(self._images)

    @property
    def images(self) -> tuple[Attachment, ...]:
        return self._images

    def set_state(self, new_state: _State) -> None:
        """Переключить состояние реплики: цвет маркера и вид очереди."""
        self._state = new_state
        self.set_class(new_state == "queued", "-queued")
        if new_state != "queued":
            self.set_queue_hint(False)
        self._band.refresh()

    def set_queue_hint(self, shown: bool) -> None:
        """Показать или скрыть подсказку очереди под подложкой (на фоне
        ленты, в колонке текста). В копию выделения она не идёт."""
        self.set_class(shown, "-queue-hint")

    def add_consequence(self, body: Text) -> None:
        """Дописать строку `└ body` под подложкой реплики (на фоне ленты).
        В копию выделения строка не идёт."""
        self._consequences.append(body)
        self.add_class("has-consequence")
        self._consequence_slot.set_bodies(self._consequences)

    def set_event_no(self, event_no: int) -> None:
        """Номер события треда: живая реплика получает его при сверке с записью треда —
        до этого Ctrl+клик и Enter экрана события не открывают."""
        self._event_no = event_no

    def set_images(self, images: tuple[Attachment, ...]) -> None:
        """Заменить картинки реплики: pending-реплика при сверке получает копии из
        треда вместо файлов кармана (дизайн картинок §3)."""
        self._images = tuple(images)
        self._band.refresh(layout=True)

    def _reply_lines(self, width: int) -> list[_ReplyLine]:
        color = _MARKER_COLOR.get(self._state, _ACCENT)
        lead = Text(USER_PREFIX, style=Style(color=color))
        lines = _build_lines(
            self._text, self._images, self._hover_image_id, lead, width
        )
        if self._skill is None:
            return lines
        first = lines[0]
        head = self._skill_head(quiet=self._state == "queued")
        body = head.append_text(first.body)
        return [first._replace(body=body), *lines[1:]]

    def _skill_head(self, *, quiet: bool) -> Text:
        """`/имя ` перед текстом первой строки; без текста — только имя.
        `quiet` — приглушённое имя (тихая реплика)."""
        head = Text()
        color = _QUIET_SKILL if quiet else _SKILL
        head.append(
            "/" + self._skill,
            style="" if self._skill_error else Style(color=color),
        )
        if self._text:
            head.append(" ")
        return head

    def quiet_line(self) -> Text:
        """Реплика одной строкой «тихим» видом: `›` + `/имя` + текст. Тот же
        вид, что у реплики в очереди (подложка `QUEUE_BG`, текст
        `QUIET_REPLY_INK`); липкая реплика над лентой рисует его же."""
        line = Text(USER_PREFIX, style=Style(color=_QUIET_MARK), no_wrap=True)
        if self._skill is not None:
            line.append_text(self._skill_head(quiet=True))
        line.append(
            " ".join(self._text.split()), style=Style(color=_QUIET_INK)
        )
        return line

    def _repaint_images(self) -> None:
        self._band.refresh()

    def _image_path(self, image_id: str) -> Optional[str]:
        return _image_path(self._images, image_id)

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from gc_tui.messages import OpenEventRequest

        return OpenEventRequest(self._event_no)


def is_queued(widget: object) -> bool:
    """Реплика ждёт впуска ядром: ленте — прижать к низу (P3 §2.4)."""
    return isinstance(widget, UserReplyContainer) and widget.state == "queued"


def _nested_lines(
    text: str,
    images: tuple[Attachment, ...],
    hover_id: Optional[str],
    width: int,
) -> list[_ReplyLine]:
    """Строки вложенного ответа: колонка текста уже стоит под `› ` — префиксов нет."""
    return _build_lines(text, images, hover_id, Text(), width)


def _nested_body(
    text: str, images: tuple[Attachment, ...], hover_id: Optional[str]
) -> Text:
    """Содержимое колонки текста целиком, без переноса (`TraceRow._content`)."""
    return Text("\n").join(
        line.body for line in _nested_lines(text, images, hover_id, 0)
    )


class _NestedReplyBody(_WrappedReply, Widget):
    """Колонка текста вложенного ответа; текст, картинки и подсветка — у
    строки-владельца."""

    DEFAULT_CSS = """
    _NestedReplyBody {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, owner: NestedUserReply, **kwargs) -> None:
        super().__init__(**kwargs)
        self._owner = owner

    @property
    def _images(self) -> tuple[Attachment, ...]:
        return self._owner._images

    def _reply_lines(self, width: int) -> list[_ReplyLine]:
        owner = self._owner
        return _nested_lines(
            owner._text, owner._images, owner._hover_image_id, width
        )


class NestedUserReply(_ImageLinksMixin, TraceRow):
    """Ввод человека под строкой субагента: реплика во время работы ребёнка и ответ на
    его вопрос (child HITL). Строка вложенного ввода (`NESTED_USER_PREFIX`); картинки
    ответа — как у реплики (визуальный контракт §3): плашки цвета текста, под текстом —
    строки картинок, наведение подсвечивает, клик и Ctrl+клик открывают файл."""

    def __init__(
        self, text: str, *, images: tuple[Attachment, ...] = ()
    ) -> None:
        super().__init__(
            prefix=NESTED_USER_PREFIX,
            content=_nested_body(text, images, None),
            kind_class=LINE_USER_CLASS,
            event_no=0,
            focusable=False,
        )
        self._text = text
        self._images = tuple(images)

    def _make_prefix(self) -> Widget:
        # Маркер `› ` — не текст ответа: в копию не идёт.
        return NoSelectStatic(
            self._prefix, classes="trace-prefix", markup=False
        )

    def _make_content(self) -> Widget:
        return _NestedReplyBody(self, classes="trace-content")

    def _repaint_images(self) -> None:
        # Размер строки не меняется — без перекомпоновки ленты.
        self._content = _nested_body(
            self._text, self._images, self._hover_image_id
        )
        self.query_one(_NestedReplyBody).refresh()

    def _image_path(self, image_id: str) -> Optional[str]:
        return _image_path(self._images, image_id)
