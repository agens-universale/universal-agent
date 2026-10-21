"""CommandTextArea — многострочный ввод: Enter отправляет, Ctrl+J / Shift+Enter / Ctrl+Enter — новая строка."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Sequence
from functools import partial

from gc_core.attachments.model import Attachment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import (
    DocumentNavigator,
    Edit,
    EditResult,
    Location,
    Selection,
    WrappedDocument,
)

from gc_tui.messages import OpenImageRequest
from gc_tui.palette import SKILL
from gc_tui.services.image_chips import chip_number, chip_spans
from gc_tui.services.image_paste import pasted_file_path
from gc_tui.services.paste_chips import (
    CHIP_TEXT,
    ChipStore,
    expand,
    should_collapse,
    token_spans,
)
from gc_tui.widgets.chip_wrap import ChipWrapDocument

#: Плашка на строке: (строка, начало, конец) — столбцы, как у `token_spans`.
_Span = tuple[int, int, int]


def _span_containing(
    spans: Sequence[_Span], location: Location
) -> _Span | None:
    """Плашка, внутри которой (строго между краями) стоит `location`."""
    row, col = location
    for span in spans:
        if span[0] == row and span[1] < col < span[2]:
            return span
    return None


def _nearest_edge(span: _Span, location: Location) -> Location:
    row, start, end = span
    return (
        (row, start)
        if location[1] - start <= end - location[1]
        else (row, end)
    )


def _edge_off_chip(
    span: _Span,
    location: Location,
    previous: Location,
    anchor: Location | None,
    *,
    keyboard: bool,
) -> Location:
    """Куда уйти краю выделения (курсору), попавшему внутрь плашки. С клавиатуры шаг с
    одного края плашки — на другой: так стрелки и слова переходят плашку целиком, и
    Shift+→ после Shift+← снимает выделение. Иначе пустой курсор (клик, вертикаль) —
    на ближний край, край выделения — на край дальше от якоря: плашка входит в
    выделение целиком. Мышь переходов не делает: протяжка повторяет ту же точку внутри
    плашки, и правило перехода уводило бы край туда-обратно."""
    row, start, end = span
    if keyboard and previous == (row, end):
        return (row, start)
    if keyboard and previous == (row, start):
        return (row, end)
    if anchor is None:
        return _nearest_edge(span, location)
    return (row, start) if location <= anchor else (row, end)


# Константы вместо повторяющихся литералов (Sonar S1192).
_ACT_SELECT_ALL = "выделить всё"

#: Первое слово `/имя` — известная команда или скилл.
_COMMAND_STYLE = Style(color=SKILL)


class CommandTextArea(TextArea):
    """Multi-line input. Enter submits; Ctrl+J / Shift+Enter / Ctrl+Enter insert newline.

    Использует нативное сообщение ``TextArea.Changed`` для live-фильтра палитры;
    собственное ``Submitted`` постится по Enter (без newline-модификатора).
    """

    # Textual TextArea: ctrl+a = cursor_line_start. Нам нужен select-all
    # (Backspace/копирование); priority перебивает дефолт + ЙЦУКЕН.
    BINDINGS = [
        Binding(
            "ctrl+a", "select_all", _ACT_SELECT_ALL, show=False, priority=True
        ),
        Binding(
            "ctrl+ф", "select_all", _ACT_SELECT_ALL, show=False, priority=True
        ),
        Binding(
            "ctrl+A", "select_all", _ACT_SELECT_ALL, show=False, priority=True
        ),
        Binding(
            "ctrl+Ф", "select_all", _ACT_SELECT_ALL, show=False, priority=True
        ),
        # macOS: option+backspace (терминал в режиме Option-as-Meta шлёт alt+backspace).
        # В дефолтах TextArea клавиша не занята; Win/Linux — прежнее поведение контрактом.
        Binding(
            "alt+backspace", "delete_word_left", "удалить слово", show=False
        ),
    ]

    class Submitted(Message):
        """Posted when user presses Enter (without newline-modifier).

        ``attachments`` — изображения сообщения: image-плашки буфера (первое вхождение
        каждого id, порядок текста), снятые в момент Enter. Экран очищает поле раньше,
        чем их прочтёт, а очистка сбрасывает стор плашек."""

        def __init__(
            self, value: str, attachments: tuple[Attachment, ...] = ()
        ) -> None:
            self.value = value
            self.attachments = attachments
            super().__init__()

    class PasteCollapsed(Message):
        """Posted when a large paste was collapsed into a chip (screen shows a hint)."""

    class ImagePasteRequested(Message):
        """Вставка ждёт изображение. ``path`` — файл-кандидат из вставленного текста,
        None — буфер обмена ОС; ``on_miss`` — что сделать, если изображения нет (текст
        пути, свой буфер приложения). None — вставка просила именно изображение (пустая
        вставка): нет его, полный лимит, нечем читать буфер — отказы. ``files_only`` —
        из буфера только скопированные файлы-картинки (`image_paste.astage_paste`).
        Читает и сохраняет экран: у него конфиг, тред для номера плашки и футер для
        отказов."""

        def __init__(
            self,
            *,
            path: str | None,
            on_miss: Callable[[], None] | None,
            files_only: bool = False,
        ) -> None:
            self.path = path
            self.on_miss = on_miss
            self.files_only = files_only
            super().__init__()

    # keys that insert a newline instead of submitting.
    # ctrl+j — надёжно во всех терминалах (LF).
    # shift/ctrl+enter — Kitty keyboard protocol.
    # shift/ctrl+\r — xterm modifyOtherKeys / fixterms (Windows Terminal и др.).
    _NEWLINE_KEYS = {
        "ctrl+j",
        "shift+enter",
        "ctrl+enter",
        "shift+\r",
        "ctrl+\r",
    }

    def __init__(self, *args, **kwargs) -> None:
        # Плоский ввод без gutter с номерами строк, как обычный Input.
        kwargs.setdefault("show_line_numbers", False)
        super().__init__(*args, **kwargs)
        # Состояние «плашек» объёмной вставки (когезивно с буфером ввода).
        self._chips = ChipStore()
        self._expand_chip_id: int | None = (
            None  # последняя свёрнутая плашка (без таймера)
        )
        self._pointer_selecting = (
            False  # идёт жест мыши (см. _selection_off_chips)
        )
        # Известно ли имя команды или скилла (без «/»): правило запуска
        # задаёт экран; None — первое слово не подсвечивается.
        self.is_known_command: Callable[[str], bool] | None = None

    def _set_document(self, text: str, language: str | None) -> None:
        """Документ поля и перенос, не разрывающий плашки (`widgets/chip_wrap`): расчёт
        переноса и навигатор курсора видят пробелы внутри плашек непробельными.
        Переопределяет внутренний метод Textual (его зовут конструктор и `load_text`);
        сторож — `test_chip_wrap.py`."""
        super()._set_document(text, language)
        self.wrapped_document = WrappedDocument(
            ChipWrapDocument(self.document, self._chip_spans_by_row),
            tab_width=self.indent_width,
        )
        self.navigator = DocumentNavigator(self.wrapped_document)
        self._rewrap_and_refresh_virtual_size()

    def get_component_rich_style(self, *names: str, **kwargs) -> Style:
        """Подложка строки курсора — только фон. Штатный стиль несёт и цвет
        текста: `_render_line` кладёт его поверх `get_line` и гасит `SKILL`
        у `/имени` на строке с курсором (а это почти всегда она)."""
        if names == ("text-area--cursor-line",):
            full = super().get_component_rich_style(*names, **kwargs)
            return Style(bgcolor=full.bgcolor)
        return super().get_component_rich_style(*names, **kwargs)

    def refresh_command_highlight(self) -> None:
        """Перерисовать поле после смены ответа `is_known_command` (кэш
        скиллов прогрет или сброшен): кэш строк TextArea ключуется без него."""
        self._line_cache.clear()
        self.refresh()

    def get_line(self, line_index: int) -> Text:
        """Строка поля; у первой — `/имя` цветом `SKILL`, если экран знает
        это имя (рисование Textual зовёт этот метод на каждую строку)."""
        line = super().get_line(line_index)
        known = self.is_known_command
        if line_index or known is None or not line.plain.startswith("/"):
            return line
        token = line.plain.split(None, 1)[0]
        if known(token[1:]):
            line.stylize(_COMMAND_STYLE, 0, len(token))
        return line

    def _chip_spans_by_row(self) -> dict[int, list[tuple[int, int]]]:
        """Спаны плашек текущего текста по строкам — для `ChipWrapDocument`. Пока идёт
        базовый конструктор, стора плашек ещё нет: документ он строит раньше."""
        chips = getattr(self, "_chips", None)
        by_row: dict[int, list[tuple[int, int]]] = {}
        if chips is None:
            return by_row
        for _key, row, start, end in token_spans(self.text, chips):
            by_row.setdefault(row, []).append((start, end))
        return by_row

    async def _on_key(self, event: events.Key) -> None:
        if event.key in self._NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(
                self.Submitted(self.submission_text(), self.images_in_buffer())
            )
            return
        # let TextArea handle other keys normally (printable insert, navigation, …)
        await super()._on_key(event)

    # --- paste chips --------------------------------------------------

    def submission_text(self) -> str:
        """Текст для отправки: буфер с развёрнутыми плашками по их местам."""
        return expand(self.text, self._chips)

    def _clear_chip_state(self) -> None:
        self._chips.reset()
        self._close_expand_window()

    def load_text(self, text: str) -> None:
        """Программная замена текста (`inp.text = …`): отправка, двойной Esc, история.

        Опустевший так буфер сбрасывает плашки: TextArea.load_text стирает историю
        undo, и Ctrl+Z их уже не вернёт. Буфер, опустевший от правки (Backspace по
        последней плашке, вырезание), плашки хранит — Ctrl+Z возвращает подпись, и она
        снова разворачивается / едет вложением (класс Ctrl+Z, спека §12). Сброс не в
        Enter-ветке: Enter не всегда «отправка» (стоп записи голосом оставляет текст)."""
        super().load_text(text)
        if not text:
            self._clear_chip_state()

    def insert_image_chips(
        self, images: Sequence[Attachment], first_n: int
    ) -> int:
        """Image-плашки на место выделения одной правкой (один шаг Ctrl+Z), через пробел;
        от соседнего непробельного символа плашки тоже отделяет пробел (визуальный
        контракт §2). Набранное или вставленное вплотную к плашке позже получит пробел
        само (`_edit_off_chips`). Новые номера — first_n, first_n+1, … (сквозные по
        треду, их выдаёт экран); та же картинка снова — её прежняя плашка. Возвращает,
        сколько номеров заняли плашки."""
        labels, used = self._chips.add_images(images, first_n)
        start, end = sorted(self.selection)
        head = self.document.get_line(start[0])[: start[1]]
        tail = self.document.get_line(end[0])[end[1] :]
        text = " ".join(labels)
        if head and not head[-1].isspace():
            text = " " + text
        if tail and not tail[0].isspace():
            text += " "
        self._insert_replacing_selection(text)
        self._close_expand_window()
        return used

    def is_blank(self) -> bool:
        """Поле пустое: нет текста и нет видимой подсказки реплики.

        Шов `feat/prompt-suggestions`: подсказка живёт в `TextArea.suggestion`
        (реактив Textual), и поле с ней — не пустое. Единственный предикат
        «поле пустое» для ↑, плашки «в конец» и Enter (спека P3 §2.4)."""
        return not self.text and not self.suggestion

    def images_in_buffer(self) -> tuple[Attachment, ...]:
        """Вложения сообщения: image-плашки буфера, первое вхождение каждого id, по порядку."""
        return tuple(self._chips.images_in_text(self.text))

    def _insert_replacing_selection(self, text: str) -> None:
        start, end = sorted(self.selection)
        result = self.replace(text, start, end)
        self.move_cursor(result.end_location)

    def _open_expand_window(self, chip_id: int) -> None:
        # Запомнить последнюю свёрнутую плашку — повторная вставка её текста
        # развернёт инлайн в любой момент (без таймера). Сбрасывается при самом
        # развороте, вставке чего-то другого или отправке.
        self._expand_chip_id = chip_id

    def _close_expand_window(self) -> None:
        self._expand_chip_id = None

    def _expand_chip_inline(self, chip_id: int) -> None:
        full = self._chips.get(chip_id)
        if full is None:
            return
        for key, row, c0, c1 in token_spans(self.text, self._chips):
            if key == (CHIP_TEXT, chip_id):
                result = self.replace(full, (row, c0), (row, c1))
                self.move_cursor(result.end_location)
                break

    def _handle_paste(self, text: str) -> None:
        """Единая точка сведения трёх путей вставки."""
        # Пустая вставка: так терминал передаёт Ctrl+V / Cmd+V, когда в буфере только
        # картинка. Разбор — раньше замены выделения: пустая строка стёрла бы его.
        if not text:
            self.post_message(
                self.ImagePasteRequested(path=None, on_miss=None)
            )
            return
        # Нормализуем концы строк: bracketed-paste (Ctrl+V в Windows Terminal и пр.)
        # шлёт \r как разделитель, а should_collapse/chip_label считают \n —
        # иначе многострочная \r-вставка не сворачивается (уходит инлайн).
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 1. та же паста в окне «развернуть» → развернуть последнюю плашку инлайн
        #    (только если плашка ещё в буфере; иначе — обычная вставка ниже:
        #    Esc/очистка могли стереть label, оставив orphan expand-окно)
        if self._expand_chip_id is not None and text == self._chips.get(
            self._expand_chip_id
        ):
            chip_id = self._expand_chip_id
            if any(
                key == (CHIP_TEXT, chip_id)
                for key, *_ in token_spans(self.text, self._chips)
            ):
                self._expand_chip_inline(chip_id)
                self._close_expand_window()
                return
            self._close_expand_window()
        # 2. один путь к файлу (перетаскивание в терминал): байты проверит экран вне
        #    UI-потока — изображение станет плашкой, иначе этот же текст вставится как есть.
        path = pasted_file_path(text)
        if path is not None:
            self.post_message(
                self.ImagePasteRequested(
                    path=path, on_miss=partial(self.insert_paste_text, text)
                )
            )
            return
        self.insert_paste_text(text)

    def insert_paste_text(self, text: str) -> None:
        """Текстовая вставка: крупная — плашкой (+ окно «развернуть»), мелкая — как есть."""
        if should_collapse(text):
            chip_id, label = self._chips.add(text)
            self._insert_replacing_selection(label)
            self._open_expand_window(chip_id)
            self.post_message(self.PasteCollapsed())
            return
        self._insert_replacing_selection(text)
        self._close_expand_window()

    async def _on_paste(self, event: events.Paste) -> None:
        """Нативная (bracketed) вставка — через нашу логику плашек.

        prevent_default обязателен: без него Textual проходит MRO и вызывает ещё и
        базовый TextArea._on_paste, который вставит сырой текст (дубль). См. _on_key."""
        if self.read_only:
            return
        event.stop()
        event.prevent_default()
        self._handle_paste(event.text)

    # --- атомарность плашки -------------------------------------------
    # Плашка — один символ для любого действия. Два правила стоят на единственных
    # входах Textual: изменение выделения (`_watch_selection`) и правка документа
    # (`edit`). Поэтому их не обходят ни мышь, ни стрелки, ни слова, ни Shift, ни
    # будущие команды TextArea. Ctrl+Z идёт мимо `edit` и возвращает плашку целиком —
    # запись в сторе остаётся, подпись снова становится плашкой.

    def _chip_spans(self) -> list[_Span]:
        chips = getattr(
            self, "_chips", None
        )  # до конца __init__ стора ещё нет
        if chips is None:
            return []
        return [
            (row, start, end)
            for _key, row, start, end in token_spans(self.text, chips)
        ]

    def _watch_selection(
        self, previous_selection: Selection, selection: Selection
    ) -> None:
        """Правило 1: курсор и края выделения никогда не стоят внутри плашки."""
        fixed = self._selection_off_chips(previous_selection, selection)
        if fixed != selection:
            self.selection = (
                fixed  # наблюдатель сработает снова — уже без плашек внутри
            )
            return
        super()._watch_selection(previous_selection, selection)

    def _selection_off_chips(
        self, previous: Selection, selection: Selection
    ) -> Selection:
        spans = self._chip_spans()
        keyboard = not getattr(self, "_pointer_selecting", False)
        start, end = selection
        if selection.is_empty:
            span = _span_containing(spans, end)
            if span is None:
                return selection
            return Selection.cursor(
                _edge_off_chip(
                    span, end, previous.end, None, keyboard=keyboard
                )
            )
        span = _span_containing(spans, end)
        if span is not None:
            end = _edge_off_chip(
                span, end, previous.end, start, keyboard=keyboard
            )
        span = _span_containing(spans, start)
        if span is not None:
            row, span_start, span_end = span
            start = (row, span_start) if start <= end else (row, span_end)
        return Selection(start, end)

    def edit(self, edit: Edit) -> EditResult:
        """Правило 2: правка, задевшая часть плашки, забирает её целиком (удаление
        слова, Ctrl+U и Ctrl+K, вырезание); вставка внутрь плашки встаёт на её край.
        Правило 3: набранное или вставленное вплотную к плашке отделяется от неё
        пробелом — той же правкой, один шаг Ctrl+Z."""
        fixed = self._edit_off_chips(edit)
        result = super().edit(fixed)
        if (
            fixed.text.endswith(" ")
            and not edit.text.endswith(" ")
            and self.selection == Selection.cursor(result.end_location)
        ):
            # Пробел добавлен перед плашкой: курсор — до него, у набранного текста,
            # иначе следующая буква снова встала бы вплотную к плашке.
            row, col = result.end_location
            self.move_cursor((row, col - 1))
        return result

    def _edit_off_chips(self, edit: Edit) -> Edit:
        spans = self._chip_spans()
        top, bottom = sorted((edit.from_location, edit.to_location))
        if top == bottom:
            span = _span_containing(spans, top)
            if span is not None:
                top = _nearest_edge(span, top)
                edit = dataclasses.replace(
                    edit, from_location=top, to_location=top
                )
            return self._space_from_chips(edit, spans, top)
        span = _span_containing(spans, top)
        if span is not None:
            top = (span[0], span[1])
        span = _span_containing(spans, bottom)
        if span is not None:
            bottom = (span[0], span[2])
        if (top, bottom) == tuple(
            sorted((edit.from_location, edit.to_location))
        ):
            return edit
        return dataclasses.replace(edit, from_location=top, to_location=bottom)

    @staticmethod
    def _space_from_chips(
        edit: Edit, spans: Sequence[_Span], spot: Location
    ) -> Edit:
        """Вставка вплотную к плашке: справа от неё — пробел перед текстом, слева — после,
        если текст сам не начинается (не кончается) пробелом или переводом строки."""
        text = edit.text
        if not text:
            return edit
        row, col = spot
        if (
            any(r == row and end == col for r, _start, end in spans)
            and not text[0].isspace()
        ):
            text = " " + text
        if (
            any(r == row and start == col for r, start, _end in spans)
            and not text[-1].isspace()
        ):
            text = text + " "
        return (
            edit if text == edit.text else dataclasses.replace(edit, text=text)
        )

    # --- image-плашки: откат отправки и смена сессии --------------------

    def restore_images(self, images: Sequence[Attachment]) -> None:
        """Вернуть записи image-плашек после отката отправки: подписи уже в тексте,
        записи стора сброшены отправкой (дизайн картинок §12)."""
        for att in images:
            self._chips.add_image(att, chip_number(att))

    def renumber_image_chips(self, first_n: int) -> int:
        """Перенумеровать image-плашки текста с `first_n` (смена сессии, дизайн картинок
        §4, §12); записи стёртых плашек уходят.

        Подписи в тексте меняются одним проходом: цепочка #3→#5, #5→#7 не сливается,
        длинная подпись пробуется раньше короткой. Присваивание `text` сбрасывает
        историю undo — так и надо: в ней старые подписи, которых после перенумерации
        в сторе нет. Возвращает, сколько номеров заняли плашки."""
        mapping = self._chips.renumber_images(first_n, self.text)
        if any(old != new for old, new in mapping.items()):
            pattern = re.compile(
                "|".join(
                    re.escape(old)
                    for old in sorted(mapping, key=len, reverse=True)
                )
            )
            self.text = pattern.sub(lambda m: mapping[m.group(0)], self.text)
            self.move_cursor(self.document.end)
        return len(mapping)

    def _image_chip_at(self, location: tuple[int, int]) -> Attachment | None:
        """Вложение image-плашки, внутри которой стоит позиция документа (row, col)."""
        row, col = location
        line = self.document.get_line(row)
        for start, end, att in chip_spans(
            line, self._chips.images_in_text(self.text)
        ):
            if start <= col < end:
                return att
        return None

    # --- визуальная геометрия курсора (для ↑/↓ history-навигации экрана) ----

    def cursor_on_first_visual_line(self) -> bool:
        """Курсор на самой верхней ВИЗУАЛЬНОЙ строке (с учётом soft-wrap).

        Логический ``cursor_location[0] == 0`` недостаточен: длинная строка
        переносится на несколько визуальных — курсор может быть в row 0, но
        визуально ниже. Экран спрашивает это, решая «↑ → история или вверх».
        """
        return (
            self.wrapped_document.location_to_offset(self.cursor_location).y
            == 0
        )

    def cursor_on_last_visual_line(self) -> bool:
        """Курсор на самой нижней ВИЗУАЛЬНОЙ строке (с учётом soft-wrap)."""
        wd = self.wrapped_document
        return wd.location_to_offset(self.cursor_location).y == wd.height - 1

    def action_paste(self) -> None:
        """Ctrl+V и правый клик: скопированные файлы-картинки → текст ОС → изображение
        ОС → внутренний буфер приложения.

        Текст раньше изображения: в буфере бывают и текст, и картинка — Ctrl+V вставляет
        текст, картинку берёт Alt+V. Но скопированные файлы (Finder) кладут рядом свои
        имена текстом — файлы-картинки важнее имён: при тексте в буфере экран сначала
        ищет их (только там, где имена приходят, — `copied_files_come_with_names`),
        нет — вставляет текст. Изображение раньше app.clipboard: иначе текст от
        copy-on-select перебивал бы скриншот. Буфер ОС экран читает вне UI-потока
        (ImagePasteRequested). bracketed-paste (events.Paste) сюда не заходит — там
        текст приходит сам.
        """
        from gc_tui.services import os_ops

        text = os_ops.read_clipboard()
        if text and not os_ops.copied_files_come_with_names():
            self._handle_paste(text)
            return
        if text:
            self.post_message(
                self.ImagePasteRequested(
                    path=None,
                    on_miss=partial(self._handle_paste, text),
                    files_only=True,
                )
            )
            return
        self.post_message(
            self.ImagePasteRequested(
                path=None, on_miss=self.paste_app_clipboard
            )
        )

    def paste_app_clipboard(self) -> None:
        """Внутренний буфер приложения (copy-on-select, Ctrl+C) — штатная вставка TextArea."""
        super().action_paste()

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        """Правый клик → та же вставка, что Ctrl+V (текст ОС → изображение → буфер TUI).

        При включённом mouse-reporting терминал отдаёт правый клик приложению как
        mouse-событие, и нативная «paste по правому клику» терминала не срабатывает.
        Обрабатываем сами — UX как в обычном терминале. Остальные кнопки —
        штатное поведение TextArea (позиционирование курсора / выделение).
        """
        # Ctrl+клик по image-плашке — открыть картинку (дизайн картинок §20.2).
        # Раньше правой кнопки: часть терминалов отдаёт Ctrl+клик как button 3.
        if event.ctrl:
            att = self._image_chip_at(self.get_target_document_location(event))
            if att is not None:
                event.stop()
                event.prevent_default()
                self.post_message(OpenImageRequest(att.path))
                return
        if event.button == 3:
            event.stop()
            event.prevent_default()
            self.action_paste()
            return
        # Остальное — штатный обработчик TextArea: его зовёт сам Textual следующим по
        # MRO. Явный вызов отсюда исполнял его дважды, и второй вызов возвращал курсор
        # внутрь плашки. Курсор на краю плашки держит `_watch_selection`; жест мыши
        # (клик, протяжка) он узнаёт по флагу — до отпускания кнопки.
        self._pointer_selecting = True

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        # Штатный обработчик TextArea Textual зовёт следующим по MRO.
        self._pointer_selecting = False
