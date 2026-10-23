"""Рендер диффов для TUI.

`render_hunks` — движок правок агента: из поля `hunks` журнала (волна 1)
собирает готовый к отрисовке `rich.Text` с колонкой номеров строк, маркерами,
фоновой заливкой строк, внутристрочной подсветкой и подсветкой синтаксиса. Его
рисуют и блоки правок в ленте, и вьюер `/diff` — один дифф не имеет права
выглядеть в двух местах продукта по-разному.

`HunkBody` — тот же движок, но разобранным телом наружу: число строк известно
сразу и дёшево, а рисуются те строки, которые заказали. Из него живут экраны
правок (Р23): место под участок резервируется по числу строк, а картинка
считается, когда участок доехал до кадра. `render_hunks` — частный случай
«нарисуй всё».

Выход обоих строго `rich.Text`: `Widget.get_selection` отдаёт текст только для
Text/Content, иначе виджет молча выпадает из мышиного выделения (см.
services/code_render).

`copyable_hunks` — единственный здесь выход в `str`: те же хэнки, но для
ВНЕШНЕГО инструмента (`git apply`/`patch`), с подставленным на показе именем
файла в заголовках. Картинку он не трогает.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from gc_core.utils.file_text import split_lines_lf
from rich.style import Style
from rich.text import Text

from gc_tui.palette import (
    DIFF_ADD_BG,
    DIFF_ADD_GUTTER,
    DIFF_ADD_HL,
    DIFF_DEL_BG,
    DIFF_DEL_GUTTER,
    DIFF_DEL_HL,
    DIFF_GUTTER,
)
from gc_tui.services.code_render import highlight_code, normalize_code

# Порог похожести строк для intraline: ниже него подсветка вырождается в
# «изменилось всё» и только рябит. 0.3 отсекает пары, у которых общего меньше
# трети, — переименование строки целиком уже не разбирается по буквам.
_INTRALINE_MIN_RATIO = 0.3
# Потолок длины строки для intraline. Отсекает абсурдные строки (минифицированный
# код, блобы) до любых подсчётов: на строке в тысячу символов внутристрочная
# подсветка всё равно нечитаема, а сам гвард ниже линеен по длине и на строке в
# 11 тысяч символов стоил бы уже 0.66 мс.
_INTRALINE_MAX_LINE_CHARS = 1000
# Потолок работы SequenceMatcher. Без autojunk матчинг квадратичен по числу
# совпадающих пар символов: внутренний цикл difflib для каждого символа old
# перебирает позиции этого символа в new, то есть работа ~ сумме произведений
# частот общих символов (_match_work). Замер: 0.14-0.19 мкс на единицу работы
# устойчиво на всех формах входа, поэтому 5000 единиц — это ~0.95 мс худшего
# случая на пару. Проверено на реальных строках репозитория: худшая пара под
# бюджетом (4982 единицы, 380 символов) считается 0.956 мс.
# Одной длины тут мало: строка из 200 одинаковых символов даёт 2.9 мс, а из 400 —
# 13.4 мс, при том что 380 символов обычного кода укладываются в миллисекунду.
_INTRALINE_MAX_MATCH_WORK = 5000
# Свыше этого числа строк тела подсветка синтаксиса отключается: лексер по
# всему телу перестаёт окупаться, а дифф такого размера и так читается целиком.
_SYNTAX_MAX_LINES = 400
# ...и свыше этой длины ОДНОЙ строки — тоже. Счёт строк такую цену не ловит:
# тело из трёх строк по 9 тыс. символов (генерированный или минифицированный
# код) проходит лимит строк насквозь, а лексер выдаёт на нём 7212 спанов и
# 101 мс, и рендер развёрнутого блока стоит 265 мс против 2 мс без подсветки —
# Rich складывает эти спаны на КАЖДУЮ отрисовку, и лента залипает на скролле
# (замер смока 2026-09-10). Раскрашивать такую строку и незачем: читать её
# всё равно построчно нельзя. Предел заведомо выше любой рукописной строки —
# сосед по идее, `_INTRALINE_MAX_LINE_CHARS`, стоит на 1000.
_SYNTAX_MAX_LINE_CHARS = 2000
_HUNK_SEPARATOR = "⋮"
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# --- движок правок агента ----------------------------------------------------


@dataclass
class DiffRender:
    """Готовый к отрисовке дифф и честные счётчики строк тела."""

    text: Text
    shown_lines: int
    total_lines: int
    truncated: bool
    # Ряды, срезанные потолком ВНУТРИ логической строки (см. `HunkBody`).
    hidden_rows: int = 0


@dataclass
class _Row:
    kind: str  # "add" | "del" | "ctx" | "sep"
    old_no: int | None
    new_no: int | None
    content: str


def render_hunks(
    hunks: str,
    *,
    path: str,
    max_lines: int | None = None,
    syntax: bool = True,
    fill_width: int | None = None,
    wrap: bool = False,
    min_gutter: int = 1,
    logical: bool = False,
) -> DiffRender:
    """Хэнки журнала правок → `rich.Text` с номерами строк и подсветкой.

    Понимает обе формы поля `hunks`: difflib (`--- ` / `+++ ` / `@@`) и git
    (`diff --git` / `index` / `new file mode` / `@@`). Служебные заголовки в
    тело не выводятся, так что обе формы дают одинаковый результат.

    `max_lines` обрезает тело сверху; хвостовую сноску «ещё N строк» дорисовывает
    вызывающий виджет. Непарсибельный вход даёт пустой рендер без исключений.

    `fill_width` — целевая ширина ЗАЛИТОЙ строки в ячейках вместе с гаттером:
    вызывающий отдаёт сюда правый край своей подложки (блок ленты — минус поле
    в один символ, экраны — минус колонка скроллбара), чтобы заливка не
    обрывалась по самой длинной строке тела. `None` — ширина ещё не известна
    (сборка до раскладки): заливка идёт по содержимому, а правый край доводит
    первый же `Resize` вызывающего. Содержимое шире целевой ширины не режется:
    обрезкой занимается CSS виджета.

    `min_gutter` — ширина колонки номеров не меньше: под блоком ленты P4
    номер прижат вправо к третьей клетке колонки диффа (спека P4 §6.2).

    `logical` — бюджет `max_lines` в ЛОГИЧЕСКИХ строках (под блоком ленты
    P4, §6.3): строка не режется по рядам, перенос делает превью выше, и
    скрытое — ровно строки сверх бюджета.

    Рисует тело ЦЕЛИКОМ — этого и хочет блок ленты. Тому, кто рисует его
    участками, нужен `HunkBody`: там та же картинка, но по заказанным строкам.
    """
    body = HunkBody(
        hunks,
        path=path,
        max_lines=max_lines,
        syntax=syntax,
        fill_width=fill_width,
        wrap=wrap,
        min_gutter=min_gutter,
        logical=logical,
    )
    return DiffRender(
        body.render(),
        body.shown_lines,
        body.total_lines,
        body.truncated,
        body.hidden_rows,
    )


def diff_line_count(hunks: str) -> int:
    """Логических строк тела диффа (без заголовков) — без отрисовки."""
    return sum(1 for row in _parse(hunks or "") if row.kind != "sep")


def copyable_hunks(hunks: str, *, path: str) -> str:
    """Хэнки, пригодные для `git apply`/`patch` (Ж5 батча 4).

    `build_diff` зовёт `difflib.unified_diff` без имён файлов, и в журнал
    попадают пустые заголовки `--- `/`+++ `: читать такой дифф можно, применить
    — нет, инструменту неизвестен файл. Имя подставляется ЗДЕСЬ, на показе, а не
    в журнале: формат хранения остаётся прежним, старые треды валидны, счётчики
    (`build_diff` считает тело позиционно, `lines[2:]`) не задеты.

    Форма git (командный канал хранит вывод `git diff`) уже несёт имена и
    возвращается как есть — как и любой вход, который не начинается ровно с
    двух пустых difflib-заголовков.

    Абсолютный ключ тоже уходит как есть: `canonical_path_key` штатно отдаёт
    абсолютный POSIX-путь файлам ВНЕ проекта, а `git apply -p1`
    не примет `--- a/C:/…` — заголовок `a/`/`b/` осмыслен только над путём от
    корня репозитория. Дифф без имени честнее диффа с именем, которое инструмент
    отвергнет.
    """
    lines = hunks.split("\n")
    if (
        len(lines) < 2
        or lines[0].rstrip() != "---"
        or lines[1].rstrip() != "+++"
    ):
        return hunks
    rel = path.replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        return hunks
    lines[0] = f"--- a/{rel}"
    lines[1] = f"+++ b/{rel}"
    return "\n".join(lines)


class HunkBody:
    """Разобранное тело файла: строки известны сразу, картинка — по заказу.

    Разбор дёшев, а картинка дорога: тело в 8 тыс. строк разбирается за 8 мс, а
    рисуется за 540 (львиная доля — внутристрочные акценты на четырёх тысячах
    пар). Экраны правок из этого и живут (Р23): число строк они узнают сразу и
    резервируют по нему место участка, а рисовать просят то, что доехало до
    кадра, — открытие перестаёт зависеть от размера диффа.

    Решения, общие для ВСЕГО тела, считаются один раз, здесь: ширина колонки
    номеров, ширина заливки, порог и спаны подсветки синтаксиса. Посчитанные по
    участку порознь, они разъехались бы гаттером и раскраской — тот самый
    запрет, из-за которого раньше резали уже готовый `rich.Text`.

    Внутристрочные акценты считаются по группам «подряд удалённые + подряд
    добавленные»: пара разбирается целиком, поэтому заказанный диапазон
    расширяется до краёв групп, в которые он попал, — результат тот же, что у
    разбора всего тела разом, а работа только по нужным группам.
    """

    def __init__(
        self,
        hunks: str,
        *,
        path: str,
        max_lines: int | None = None,
        syntax: bool = True,
        fill_width: int | None = None,
        wrap: bool = False,
        min_gutter: int = 1,
        logical: bool = False,
    ) -> None:
        rows = _parse(hunks or "")
        total = sum(1 for row in rows if row.kind != "sep")
        # Ширина колонки номеров — по ВСЕМУ телу, а не по отобранной части:
        # от неё зависит ширина реза, а рез решает, что отбирать. Заодно
        # колонка перестаёт прыгать при разворачивании блока.
        self._gutter_w = max(
            (
                len(str(row.old_no if row.kind == "del" else row.new_no))
                for row in rows
                if row.kind != "sep"
            ),
            default=1,
        )
        self._gutter_w = max(self._gutter_w, min_gutter)
        # Перенос — ЯВНЫЙ, а не следствие известной ширины: ширину передают и
        # экраны правок, но там строка не переносится (по дизайну у них
        # прокрутка вбок, а на нерасползающейся высоте держится ленивое тело).
        self._wrap_w = (
            fill_width - (self._gutter_w + 1) - 1
            if wrap and fill_width is not None
            else None
        )
        # Лимита нет — не ограничиваем ВОВСЕ. Подставлять сюда число
        # логических строк нельзя: бюджет считается в рядах, и перенесённая
        # строка «не влезала» бы, исчезая целиком.
        limit = None if max_lines is None else max(0, max_lines)
        # `logical` — бюджет в строках файла: ряды переноса не считаются,
        # и строка целиком (потолка рядов нет).
        self.rows = _take(rows, limit, None if logical else self._wrap_w)
        # Потолок рядов на СТРОКУ. `_take` берёт первую строку всегда — пустое
        # тело с одной сноской бесполезнее переполненного, — но одна длинная
        # строка переполняла бюджет не «на ряд», как я закладывал, а в десять
        # раз: бюджет 30 рядов давал 310. Рез по границе логической строки
        # остаётся правилом; это исключение ровно для того случая, когда сама
        # строка в бюджет не влезает.
        self._row_cap = (
            limit if (limit and self._wrap_w and not logical) else None
        )
        self.kinds = [row.kind for row in self.rows]
        self.total_lines = total

        body = [row for row in self.rows if row.kind != "sep"]
        self.shown_lines = len(body)
        # Ряды, срезанные потолком ВНУТРИ логической строки. Считать их
        # обязательно: `_append_row` роняет лишние куски молча, а
        # `shown_lines`/`total_lines` считаются по СТРОКАМ и такого реза не
        # видят — тело из одной длинной строки отдавало `truncated=False`,
        # хвост «… ещё N строк» не появлялся, и обрезанный блок нельзя было
        # развернуть. Число рядов у `_chunks` точное (рез по символам), так
        # что делением и обходимся — куски строить не нужно.
        self.hidden_rows = 0
        if self._row_cap and self._wrap_w:
            for row in body:
                rows_of_line = max(1, -(-len(row.content) // self._wrap_w))
                if rows_of_line > self._row_cap:
                    self.hidden_rows += rows_of_line - self._row_cap
        self.truncated = self.shown_lines < total or self.hidden_rows > 0
        self._body = body
        # Номер строки ТЕЛА для каждой строки рендера (разделитель `⋮` номера не
        # занимает): по нему участок находит свои спаны подсветки и акценты.
        self._body_at: list[int] = []
        index = 0
        for row in self.rows:
            self._body_at.append(index)
            if row.kind != "sep":
                index += 1

        # Ширина заливки после гаттера (маркер + содержимое): rich.Text красит
        # только сам текст, поэтому фон добивается пробелами. Минимум — самая
        # длинная строка тела, чтобы содержимое не осталось без фона; заказанная
        # ширина тянет заливку дальше, до края подложки виджета.
        if self._wrap_w and self._wrap_w > 0:
            # При переносе заливка равна ширине ПОКАЗА: после реза ряда длиннее
            # неё не бывает. Считать её по самой длинной ЛОГИЧЕСКОЙ строке (как
            # без переноса) — значит добивать пробелами КАЖДЫЙ кусок до её
            # длины: строка в 100 000 знаков давала 1031 ряд по 100 000, то есть
            # текст на 103 млн знаков и около 200 МБ на один блок ленты.
            self._fill_w = self._wrap_w + 1
        else:
            self._fill_w = max(
                (1 + len(row.content) for row in body), default=1
            )
            if fill_width is not None:
                self._fill_w = max(
                    self._fill_w, fill_width - (self._gutter_w + 1)
                )
        self._lang = (
            _lang_for_path(path)
            if syntax and 0 < len(body) <= _SYNTAX_MAX_LINES
            else None
        )
        self._code_spans: list | None = None
        self._groups = _replace_groups(self.kinds)
        self._group_ends = [end for _start, end in self._groups]
        self._done: set[int] = set()
        self._accents: dict[int, list[tuple[int, int]]] = {}

    def render(self, begin: int = 0, end: int | None = None) -> Text:
        """Строки `[begin:end)` готовым `rich.Text`; без границ — всё тело."""
        stop = len(self.rows) if end is None else min(end, len(self.rows))
        text = Text(no_wrap=True)
        if begin >= stop or not self.shown_lines:
            return text
        code_spans = self._spans()
        accents = self._accents_for(begin, stop)
        for position in range(begin, stop):
            row = self.rows[position]
            if position > begin:
                text.append("\n")
            if row.kind == "sep":
                text.append(
                    _HUNK_SEPARATOR.rjust(self._gutter_w), style=DIFF_GUTTER
                )
                continue
            body_index = self._body_at[position]
            _append_row(
                text,
                row,
                self._gutter_w,
                self._fill_w,
                code_spans[body_index],
                accents.get(body_index, ()),
                wrap_w=self._wrap_w,
                max_rows=self._row_cap,
            )
        return text

    def _spans(self) -> list:
        """Спаны подсветки по строкам тела — считаются один раз на всё тело.

        Порознь их считать нельзя: лексер разбирает тело целиком, и токен,
        начавшийся в одном участке, кончается в другом.
        """
        if self._code_spans is None:
            self._code_spans = _syntax_spans(
                [row.content for row in self._body], self._lang
            )
        return self._code_spans

    def _accents_for(self, begin: int, stop: int) -> dict:
        """Акценты для строк `[begin:stop)`: считаются по нетронутым группам."""
        first = bisect_right(self._group_ends, begin)
        for index in range(first, len(self._groups)):
            start, end = self._groups[index]
            if start >= stop:
                break
            if index in self._done:
                continue
            self._done.add(index)
            offset = self._body_at[start]
            for body_index, spans in _intraline(self.rows[start:end]).items():
                self._accents[body_index + offset] = spans
        return self._accents


def _append_row(
    text: Text,
    row: _Row,
    gutter_w: int,
    fill_w: int,
    code_spans,
    accents,
    *,
    wrap_w: int | None = None,
    max_rows: int | None = None,
) -> None:
    """Одна строка тела: гаттер, маркер, содержимое, фон, акценты.

    Номер и маркер красятся ОДНИМ цветом по типу строки — колонка слева
    показывает структуру правки раньше, чем прочитан код. Маркер лежит уже на
    заливке строки, поэтому его цвет сведён с ней (см. комментарий у токенов).

    Порядок наложения стилей = порядок спанов: сначала синтаксис (цвет текста,
    без фона — его срезает code_render), поверх — фон строки (задаёт только
    bgcolor, цвет от синтаксиса сохраняется), поверх — intraline-акцент (снова
    только bgcolor). Rich складывает спаны слева направо, поэтому цвет текста
    доживает до низа стопки, а фон выигрывает последний.
    """
    number = row.old_no if row.kind == "del" else row.new_no
    marker = {"add": "+", "del": "-"}.get(row.kind, " ")
    gutter = f"{number:>{gutter_w}} "
    filled = row.kind in ("add", "del")
    gutter_style = {"add": DIFF_ADD_GUTTER, "del": DIFF_DEL_GUTTER}.get(
        row.kind, DIFF_GUTTER
    )

    chunks = _chunks(row.content, wrap_w)
    if max_rows is not None and len(chunks) > max_rows:
        chunks = chunks[:max_rows]
    for chunk_no, (at, chunk) in enumerate(chunks):
        if chunk_no:
            text.append("\n")
        start = len(text)
        # Номер — только у ПЕРВОГО ряда: он опознаёт строку файла, и
        # повторённый на продолжении соврал бы. Маркер, наоборот, на каждом:
        # он говорит, какого рода строку читаешь, и на продолжении это нужно
        # не меньше.
        head = gutter if chunk_no == 0 else " " * len(gutter)
        line = head + marker + chunk
        if filled:
            line = line.ljust(len(gutter) + fill_w)
        text.append(line)

        if chunk_no == 0:
            text.stylize(gutter_style, start, start + gutter_w)
        text.stylize(
            gutter_style, start + len(gutter), start + len(gutter) + 1
        )
        content_at = start + len(gutter) + 1
        for span_start, span_end, style in _clip(code_spans, at, len(chunk)):
            text.stylize(style, content_at + span_start, content_at + span_end)
        if filled:
            background = DIFF_ADD_BG if row.kind == "add" else DIFF_DEL_BG
            accent = DIFF_ADD_HL if row.kind == "add" else DIFF_DEL_HL
            # Заливка идёт от начала РЯДА, ВКЛЮЧАЯ колонку номеров: полоса
            # правки читается как одна полоса, а не как текст с приклеенным
            # слева номером на чужом фоне. Контраст номера при этом уже
            # гарантирован — маркер того же цвета лежал на заливке всегда
            # (`test_markers_readable_on_their_row_backgrounds`).
            text.stylize(Style(bgcolor=background), start, len(text))
            for span_start, span_end, _ in _clip(
                [(a, b, None) for a, b in accents], at, len(chunk)
            ):
                text.stylize(
                    Style(bgcolor=accent),
                    content_at + span_start,
                    content_at + span_end,
                )


def _chunks(content: str, width: int | None) -> list:
    """Содержимое строки кусками по `width` — перенос делаем САМИ.

    У Rich `overflow="fold"` рвёт сначала по СЛОВАМ и складывает только слишком
    длинное слово: на строке кода это оставляет `def` одиноко на своём ряду и
    тратит ряды впустую. Свой рез вдобавок даёт точное число рядов — бюджет
    свёрнутого блока считается без округлений — и явную расстановку номера с
    маркером.

    `width` None или неположительна (ширина ещё не известна) — один кусок,
    как было.
    """
    if not width or width <= 0 or len(content) <= width:
        return [(0, content)]
    return [
        (at, content[at : at + width]) for at in range(0, len(content), width)
    ]


def _clip(spans, at: int, size: int):
    """Спаны строки, пересчитанные в координаты куска `[at, at + size)`."""
    for span_start, span_end, style in spans:
        left, right = max(span_start, at), min(span_end, at + size)
        if left < right:
            yield left - at, right - at, style


def _parse(hunks: str) -> list[_Row]:
    """Хэнки (difflib или git) → строки тела; служебные заголовки отбрасываются."""
    rows: list[_Row] = []
    old_no = new_no = 0
    in_hunk = False
    seen_hunk = False
    for raw in split_lines_lf(hunks):
        header = _HUNK_RE.match(raw)
        if header:
            old_no, new_no = int(header.group(1)), int(header.group(2))
            if seen_hunk:
                rows.append(_Row("sep", None, None, ""))
            seen_hunk = True
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw.startswith("\\"):  # \ No newline at end of file
            continue
        if raw.startswith("diff --git"):  # следующий файл в потоке
            in_hunk = False
            continue
        # `\r` в хвосте — половина перевода строки CRLF, а не текст строки: с
        # ним заливка считалась на клетку длиннее выведенного («лесенка»), а
        # внутристрочная подсветка метила его изменённым символом.
        body = raw[1:].removesuffix("\r")
        marker, content = (raw[:1], normalize_code(body)) if raw else (" ", "")
        if marker == "+":
            rows.append(_Row("add", None, new_no, content))
            new_no += 1
        elif marker == "-":
            rows.append(_Row("del", old_no, None, content))
            old_no += 1
        elif marker == " ":
            rows.append(_Row("ctx", old_no, new_no, content))
            old_no += 1
            new_no += 1
        else:
            in_hunk = False
    return rows


def _take(
    rows: list[_Row], limit: int | None, wrap_w: int | None = None
) -> list[_Row]:
    """Строки тела, укладывающиеся в `limit` ЭКРАННЫХ рядов, с разделителями.

    Бюджет считается в рядах, а не в строках файла: смысл лимита — сколько
    вертикали блок вправе занять, и перенесённая строка занимает больше одной.

    Рез — по границе ЛОГИЧЕСКОЙ строки: половина перенесённой не показывается
    никогда, иначе сноска «ещё N строк» не может быть точной. Первая строка
    берётся всегда, даже если одна не влезает в бюджет: пустое тело с одной
    сноской бесполезнее переполненного на ряд.
    """
    taken: list[_Row] = []
    used = 0
    for row in rows:
        if row.kind == "sep":
            taken.append(row)
            continue
        if not wrap_w or wrap_w <= 0:
            height = 1
        else:
            height = max(1, -(-len(row.content) // wrap_w))
        if limit is not None:
            if used and used + height > limit:
                break
            if used >= limit:
                break
        taken.append(row)
        used += height
    while taken and taken[0].kind == "sep":
        taken.pop(0)
    while taken and taken[-1].kind == "sep":
        taken.pop()
    return taken


def _replace_groups(kinds: list[str]) -> list[tuple[int, int]]:
    """Границы групп «подряд удалённые + подряд добавленные» в строках рендера.

    Внутристрочные акценты живут ровно внутри такой группы и наружу не
    выходят — поэтому участок считает только свои группы, а не всё тело.
    Обход тот же, что в `_intraline`: разделитель хэнка группу разрывает.
    """
    groups: list[tuple[int, int]] = []
    index = 0
    size = len(kinds)
    while index < size:
        if kinds[index] != "del":
            index += 1
            continue
        del_end = index
        while del_end < size and kinds[del_end] == "del":
            del_end += 1
        add_end = del_end
        while add_end < size and kinds[add_end] == "add":
            add_end += 1
        groups.append((index, add_end))
        index = add_end
    return groups


def _intraline(shown_rows: list[_Row]) -> dict[int, list[tuple[int, int]]]:
    """Индекс строки тела → куски, изменившиеся внутри пары «удалено/добавлено».

    Пары берутся из replace-групп: подряд идущие удалённые строки и сразу за
    ними подряд идущие добавленные, сопоставленные по порядку. Лишние строки
    группы остаются без акцентов, как и слишком непохожие пары.
    """
    # Разделитель хэнка остаётся в последовательности: он разрывает группу.
    sequence: list[tuple[int, _Row]] = []
    body_index = 0
    for row in shown_rows:
        sequence.append((body_index, row))
        if row.kind != "sep":
            body_index += 1

    accents: dict[int, list[tuple[int, int]]] = {}
    index = 0
    size = len(sequence)
    while index < size:
        if sequence[index][1].kind != "del":
            index += 1
            continue
        del_end = index
        while del_end < size and sequence[del_end][1].kind == "del":
            del_end += 1
        add_end = del_end
        while add_end < size and sequence[add_end][1].kind == "add":
            add_end += 1
        for (del_i, deleted), (add_i, added) in zip(
            sequence[index:del_end], sequence[del_end:add_end]
        ):
            _pair_accents(
                deleted.content, added.content, del_i, add_i, accents
            )
        index = add_end
    return accents


def _pair_accents(
    old: str,
    new: str,
    old_index: int,
    new_index: int,
    accents: dict[int, list[tuple[int, int]]],
) -> None:
    if not old or not new:
        return
    # Гварды строго ДО SequenceMatcher: `ratio()` уже оплачивает весь матчинг,
    # так что проверять порог похожести постфактум поздно. Пара, не прошедшая
    # гвард, просто остаётся без акцентов — обычный фон added/removed.
    if (
        len(old) > _INTRALINE_MAX_LINE_CHARS
        or len(new) > _INTRALINE_MAX_LINE_CHARS
    ):
        return
    if _match_work(old, new) > _INTRALINE_MAX_MATCH_WORK:
        return
    # autojunk=False обязателен: на строках длиннее 200 символов эвристика
    # объявляет частые символы мусорными и рушит сравнение — строка с
    # повторяющейся структурой, отличающаяся одним символом, даёт replace на
    # полторы сотни колонок вместо одной. Просевший заодно ratio уводит пару под
    # _INTRALINE_MIN_RATIO, и подсветка пропадает совсем. Цену этого отказа от
    # штатной защиты difflib и держат гварды выше.
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    if matcher.ratio() < _INTRALINE_MIN_RATIO:
        return
    removed: list[tuple[int, int]] = []
    inserted: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace") and i2 > i1:
            removed.append((i1, i2))
        if tag in ("insert", "replace") and j2 > j1:
            inserted.append((j1, j2))
    if removed:
        accents[old_index] = removed
    if inserted:
        accents[new_index] = inserted


def _match_work(old: str, new: str) -> int:
    """Оценка работы SequenceMatcher: сумма произведений частот общих символов.

    Ровно то, что перебирает внутренний цикл difflib. Считается за один проход
    по обеим строкам, поэтому гвард сам по себе линеен и не съедает то, что
    экономит.
    """
    new_counts = Counter(new)
    return sum(
        count * new_counts[char]
        for char, count in Counter(old).items()
        if char in new_counts
    )


def _syntax_spans(contents: list[str], lang: str | None):
    """Стили подсветки, разложенные по строкам тела (координаты — от начала строки).

    Лексер зовётся один раз на всё тело: построчный вызов Syntax на диффе в
    сотни строк стоит секунды. Спаны режутся по границам строк, поэтому
    многострочный токен не утекает в соседнюю строку.
    """
    # Строки сверх предела длины уходят в лексер ПУСТЫМИ. Гасить из-за них
    # всё тело нельзя: соседние нормальные строки ни в чём не виноваты и
    # обязаны остаться цветными. Список той же длины, поэтому спаны
    # по-прежнему раскладываются по индексу строки — у погашенных их
    # просто не будет.
    contents = [
        c if len(c) <= _SYNTAX_MAX_LINE_CHARS else "" for c in contents
    ]
    joined = "\n".join(contents)
    highlighted = highlight_code(joined, lang)
    per_line: list[list[tuple[int, int, object]]] = [[] for _ in contents]
    # normalize_code внутри highlight_code срезает хвостовые переводы строк —
    # результат остаётся префиксом; всё остальное означает рассинхрон, и тогда
    # подсветка просто не накладывается.
    if not joined.startswith(highlighted.plain):
        return per_line

    starts: list[int] = []
    position = 0
    for content in contents:
        starts.append(position)
        position += len(content) + 1

    spans: list[tuple[int, int, object]] = []
    if highlighted.style:
        spans.append((0, len(highlighted.plain), highlighted.style))
    spans.extend((s.start, s.end, s.style) for s in highlighted.spans)

    for span_start, span_end, style in spans:
        line = max(0, bisect_right(starts, span_start) - 1)
        while line < len(contents) and starts[line] < span_end:
            line_start = starts[line]
            line_end = line_start + len(contents[line])
            begin, finish = (
                max(span_start, line_start),
                min(span_end, line_end),
            )
            if finish > begin:
                per_line[line].append(
                    (begin - line_start, finish - line_start, style)
                )
            line += 1
    return per_line


def _lang_for_path(path: str) -> str | None:
    """Язык подсветки по имени файла (тот же резолвер, что у просмотра файлов)."""
    from pathlib import Path

    from gc_tui.services.file_view import lexer_name_for_path

    return lexer_name_for_path(Path(path))
