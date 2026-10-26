"""Сетка колонок ленты: строки отрендеренной ленты и их проверка.

Правило — перенос ``grid_check`` макетов P4 (спека P4 §3.1). Колонки
считаются от колонки ``●`` своего уровня: 0 — глиф события и заголовка
блока; 2 — текст заголовка и ``└`` первого члена (у одиночной строки — и её
``└ следствие``); 4 — члены группы, управление (``… ещё N``, ``… ещё N
строк`` над хвостом вывода — на месте его первой строки, с ``└`` в 2, если
она первая; ``+ ещё N строк``, ``− свернуть``), диффы членов (и
разделитель ханков ``⋮`` под номерами), хвост вывода; 6 — ``└ следствие``
члена. Фраза ``+ ещё N строк`` стоит под тем, что раскрывает (дифф файла,
продолжение команды): не посреди диффа и не под другой фразой. После
``Agent`` в колонке ``c``: ``└ задача`` — в ``c + 2`` (перенос — в ``c + 4``),
маркер окна ``… ещё N`` — без ``└``, в ``c + 2`` (колонка вложенных
глифов, под всеми следствиями ``Agent``), ``− свернуть`` раскрытого окна —
там же, в ``c + 2`` без ``└``, вложенные глифы — в ``c + 2``, и база сетки
сдвигается туда же.

``rich`` и ``gc_tui`` импортируются лениво: тест-кит от них не зависит.
"""

from __future__ import annotations

import re
from typing import Any

# `*` — глиф записи итога и заметки после ответа (`* итог:`, `* заметка:`).
_GLYPHS = "●○•■✓›*"
_LIVE_WORDS = frozenset(
    {"Exploring", "Running", "Editing", "Fetching", "Finishing"}
)
_DONE_WORDS = frozenset(
    {"Explored", "Ran", "Added", "Edited", "Deleted", "Fetched", "Finished"}
)
# Строка диффа: номер и маркер; у пустой строки контекста — только номер.
_DIFF = re.compile(r"^( *)(\d+)(?: [+\- ]|$)")
# Разделитель ханков диффа — под последней клеткой колонки номеров.
_HUNK_GAP = re.compile(r"^( *)(⋮)$")
_MORE = "… ещё"
_CTL = ("+ ", "− свернуть", _MORE)
_EXPAND = re.compile(r"\+ ещё (\d+) (\w+)")
_TIMER = re.compile(r"· \d+[чмс]( \d+[мс])?$")


def trace_rows(
    app: Any, selector: str = "#trace", *, exclude: tuple[type, ...] = ()
) -> list[Any]:
    """Строки области виджета ``selector`` со стилями сегментов в спанах.

    Берутся из кадра композитора и обрезаются по региону виджета без
    колонок полосы прокрутки (левый отступ остаётся — от него считается
    ``origin``); ряды,
    попавшие в регион видимого виджета одного из типов ``exclude``,
    выбрасываются (строка итога хода сеткой блоков не описывается).
    """
    from rich.text import Text

    screen = app.screen
    widget = screen.query_one(selector)
    region = widget.region
    right = widget.scrollable_content_region.right
    skipped = _rows_of(screen, exclude)
    strips = screen._compositor.render_strips()
    rows = []
    for y in range(region.y, region.bottom):
        if y in skipped:
            continue
        row = Text()
        for segment in strips[y].crop(region.x, right):
            row.append(segment.text, segment.style)
        rows.append(row)
    return rows


def record_frames(monkeypatch: Any, selector: str = "#trace") -> list:
    """Кадры ленты, которые экран отдаёт на вывод: после каждого прохода
    композитора — текст строк области ``selector``; кадр, равный прошлому,
    не пишется. Перемена, легшая одним кадром, добавляет ровно один —
    промежуточное состояние (перекладка в несколько шагов) видно лишним."""
    from textual.css.query import NoMatches
    from textual.screen import Screen

    frames: list[list[str]] = []
    refresh = Screen._compositor_refresh

    def spy(screen: Any) -> None:
        refresh(screen)
        if screen is not screen.app.screen:
            return
        try:
            rows = trace_rows(screen.app, selector)
        except NoMatches:
            return
        frame = [row.plain.rstrip() for row in rows]
        if not frames or frames[-1] != frame:
            frames.append(frame)

    monkeypatch.setattr(Screen, "_compositor_refresh", spy)
    return frames


def _rows_of(screen: Any, types: tuple[type, ...]) -> set[int]:
    if not types:
        return set()
    rows: set[int] = set()
    for widget in screen.query("*"):
        if isinstance(widget, types) and widget.display:
            rows.update(range(widget.region.y, widget.region.bottom))
    return rows


def assert_trace_grid(rows: list[Any], *, origin: int) -> None:
    """Строки ``rich.text.Text`` ленты укладываются в сетку колонок P4.

    ``origin`` — колонка ``●`` верхнего уровня. Таймер не обязателен ни
    одному вызову (быстрее секунды его нет, спека P4 §3.4); запрещён он
    у неисполненного. Нарушение — ``AssertionError`` с номером строки (с
    единицы) и её текстом.
    """
    _Grid(rows, origin).walk()


class _Grid:
    """Обход строк сверху вниз с состоянием текущего блока."""

    def __init__(self, rows: list[Any], origin: int) -> None:
        from gc_tui import palette

        self._rows = rows
        self._origin = origin
        self._run = palette.RUN_COLOR.lower()
        self._err = palette.ERR.lower()
        self._refused = palette.REFUSED.lower()
        self._dim = palette.DIM.lower()
        self._base = 0
        self._nest: int | None = None
        self._prev: tuple[str, int] | None = None
        self._ev_word = ""
        self._ev_glyph = ""
        self._pending: int | None = None

    def walk(self) -> None:
        for n, row in enumerate(self._rows):
            self._row(n, row)

    def _fail(self, n: int, msg: str) -> None:
        text = self._rows[n].plain.rstrip()
        raise AssertionError(f"строка {n + 1}: {msg}: {text!r}")

    def _fg(self, row: Any, col: int) -> str:
        """Цвет текста в колонке ``col`` строки (последний спан побеждает)."""
        from rich.style import Style

        i = self._origin + col
        color = ""
        for span in row.spans:
            if not span.start <= i < span.end:
                continue
            style = span.style
            if isinstance(style, str):
                style = Style.parse(style)
            if style is not None and style.color is not None:
                color = style.color.get_truecolor().hex.lower()
        return color

    def _row(self, n: int, row: Any) -> None:
        plain = row.plain.rstrip()
        if not plain.strip():
            if self._prev and self._prev[0] != "member":
                self._nest = None
            return
        body = plain[self._origin :]
        if not body.strip():
            self._fail(n, "текст левее колонки глифа")
        col = len(body) - len(body.lstrip(" "))
        ch = body[col]
        is_event = ch in _GLYPHS
        in_diff = bool(_DIFF.match(body) or _HUNK_GAP.match(body))
        self._settle_pending(in_diff and not is_event)
        text = body[col:]
        if is_event:
            self._event(n, row, body, col)
        elif in_diff:
            self._diff(n, body)
        elif text.startswith("└ "):
            self._branch(n, row, text[2:], col)
        elif text.startswith(_CTL):
            self._control(n, text, col)
        elif col == self._base + 4:
            self._member(n, row, col)
        elif col > self._base + 4 and self._after_member():
            self._prev = ("wrap", self._prev[1])
        else:
            self._fail(n, f"текст в колонке {col}")

    def _settle_pending(self, in_diff: bool) -> None:
        """Фраза раскрытия стоит под тем, что раскрывает: строка диффа
        под ней — фраза посреди диффа."""
        if self._pending is not None and in_diff:
            self._fail(self._pending, "фраза раскрытия посреди диффа")
        self._pending = None

    def _after_member(self) -> bool:
        """Перенос длинного текста члена — под его текстом."""
        return self._prev is not None and self._prev[0] in ("member", "wrap")

    def _event(self, n: int, row: Any, body: str, col: int) -> None:
        if col not in (0, self._nest):
            self._fail(n, f"глиф в колонке {col} (у члена группы глифа нет)")
        self._base = col
        word = body[col + 2 :].split(" ", 1)[0]
        glyph = self._fg(row, col)
        self._ev_word, self._ev_glyph = word, glyph
        if word in _LIVE_WORDS and glyph != self._run:
            self._fail(n, "живой глагол без янтарного глифа")
        if word in _DONE_WORDS and glyph == self._run:
            self._fail(n, "прошедшее время с янтарным глифом")
        if word == "Agent":
            self._nest = col + 2
        elif col != self._nest:
            self._nest = None
        self._prev = ("event", col)

    def _diff(self, n: int, body: str) -> None:
        match = _DIFF.match(body) or _HUNK_GAP.match(body)
        # Колонка диффа — начало колонки номеров (ширина 3, прижата вправо).
        dcol = len(match.group(1)) + len(match.group(2)) - 3
        if dcol not in (self._base + 2, self._base + 4):
            want = f"{self._base + 2}/{self._base + 4}"
            self._fail(n, f"дифф в колонке {dcol}, ждали {want}")
        self._prev = ("diff", dcol)

    def _branch(self, n: int, row: Any, text: str, col: int) -> None:
        if self._agent_level() and text.startswith(_MORE):
            self._fail(n, "маркер окна `Agent` с `└`")
        ctl = text.startswith(_CTL)
        if not ctl:
            reason = self._fg(row, col + 2)
            self._check_timer(n, text, reason, col)
            self._check_reason_colour(n, reason, col)
        if col == self._base + 2:
            self._prev = ("ctl" if ctl else "member", col + 2)
        elif col == self._base + 6:
            if self._prev != ("member", self._base + 4):
                self._fail(n, "следствие не на шаг глубже своего члена")
            self._prev = ("cons", col)
        elif self._nest is not None and col == self._nest:
            self._prev = ("ctl", col + 2)  # «└ … ещё N» субагента
        else:
            self._fail(n, f"`└` в колонке {col}")

    def _check_timer(self, n: int, text: str, reason: str, col: int) -> None:
        """У неисполненного вызова таймера нет."""
        if col not in (self._base + 2, self._base + 6):
            return
        if not self._prev or self._prev[0] not in ("event", "member"):
            return
        owner = self._rows[n - 1].plain.rstrip().rstrip("❐").rstrip()
        timed = bool(_TIMER.search(owner))
        never = reason == self._refused or text.startswith("не запущено")
        if never and timed:
            self._fail(n, "у неисполненного вызова есть таймер")

    def _check_reason_colour(self, n: int, reason: str, col: int) -> None:
        if col == self._base + 6 and reason not in (
            self._err,
            self._refused,
            self._dim,
        ):
            self._fail(n, "причина члена не цветом исхода")
        single = (
            col == self._base + 2
            and self._prev
            and self._prev[0] == "event"
            and self._ev_word in ("Ran", "Edit")
            and self._ev_glyph in (self._err, self._refused)
        )
        if single and reason != self._ev_glyph:
            self._fail(n, "причина одиночного вызова не цветом исхода")

    def _control(self, n: int, text: str, col: int) -> None:
        if self._agent_level() and text.startswith(_MORE):
            if col != self._nest:
                want = f"в колонке {col}, ждали {self._nest}"
                self._fail(n, f"маркер окна `Agent` {want}")
            self._prev = ("ctl", col)
            return
        if col != self._base + 4 and not self._agent_collapse(text, col):
            want = self._base + 4
            self._fail(n, f"управление в колонке {col}, ждали {want}")
        if text.startswith("+ "):
            if not _EXPAND.fullmatch(text):
                self._fail(n, "фраза раскрытия не «+ ещё N строк»")
            if self._prev is not None and self._prev[0] == "ctl":
                self._fail(n, "фраза раскрытия не под тем, что раскрывает")
            self._pending = n
        self._prev = ("ctl", col)

    def _agent_level(self) -> bool:
        """Строки под самим ``Agent`` — до его первого вложенного события."""
        return self._ev_word == "Agent" and self._nest == self._base + 2

    def _agent_collapse(self, text: str, col: int) -> bool:
        """``− свернуть`` раскрытого окна ``Agent`` — последняя строка его
        блока, в колонке маркера ``… ещё N`` (вложенных глифов)."""
        return (
            self._nest is not None
            and col == self._nest
            and text.startswith("− свернуть")
        )

    def _member(self, n: int, row: Any, col: int) -> None:
        if self._fg(row, col) in (self._err, self._refused):
            self._fail(n, "текст члена цветом сбоя (должен как у соседей)")
        self._prev = ("member", col)


__all__ = ["assert_trace_grid", "record_frames", "trace_rows"]
