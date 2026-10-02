"""Лёгкий render-A markdown (§2.5 / §3.2).

API:
- `segment_answer` — сегментирует ответ на прозовые прогоны и code/table-блоки.
- `render_prose`   — рендерит прозовый прогон построчно с inline-стилями.

Семантика соответствует canon §3.2:
- `**bold**`        → accent (без `**`)
- `# heading`       → accent bold (без `#`)
- inline `` `code` `` → slate code (без бэктиков)
- списки `- item`   → ink, маркер `• `
- fenced ``` блок ``` → code-block (мягкий серый); закрывает блок только голая
  ограда (без info-строки), не короче открывающей (CommonMark) — вложенная
  ограда с языком (например ```` ```bash ```` внутри ```` ```markdown ````)
  остаётся содержимым; незакрытый блок тянется до конца текста
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

if TYPE_CHECKING:
    from rich.table import Table

from gc_tui.palette import ACCENT as _ACC
from gc_tui.palette import CODE as _CODE
from gc_tui.palette import DEEP_DIM as _DEEP_DIM
from gc_tui.palette import INK as _INK


@dataclass(frozen=True)
class Block:
    """Один блок markdown: prose-прогон, fenced-код или таблица.

    `lang` — только для kind="code-block". `headers`/`rows`/`aligns` — только для kind="table".
    """

    kind: str  # 'prose' | 'code-block' | 'table'
    content: str = ""
    lang: str | None = None
    headers: list[str] | None = None
    rows: list[list[str]] | None = None
    aligns: list[str] | None = None


_TABLE_SEP_CELL = re.compile(r"^\s*:?-{1,}:?\s*$")


def _split_table_row(line: str) -> list[str]:
    """`| a | b |` → ['a', 'b']. Срезает крайние пустые ячейки от обрамляющих `|`."""
    cells = line.split("|")
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _is_table_separator(line: str) -> bool:
    """Строка-разделитель таблицы: `|---|:--:|--:|` (каждая ячейка — дефисы с опц. `:`)."""
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return len(cells) > 0 and all(_TABLE_SEP_CELL.match(c) for c in cells)


def _parse_table_aligns(sep_line: str, ncols: int) -> list[str]:
    aligns: list[str] = []
    for c in _split_table_row(sep_line):
        left, right = c.startswith(":"), c.endswith(":")
        if left and right:
            aligns.append("center")
        else:
            aligns.append("right" if right else "left")
    return (aligns + ["left"] * ncols)[:ncols]


_FENCE_RE = re.compile(r"^(`{3,})(.*)$")


def _fence_open(line: str) -> tuple[int, str | None] | None:
    """Открывающая ограда: (длина, метка языка или None); иначе None."""
    m = _FENCE_RE.match(line)
    if not m:
        return None
    return len(m.group(1)), (m.group(2).strip() or None)


def _fence_closes(line: str, opened: int) -> bool:
    """Закрывающая ограда — одни кавычки, не короче открывающей (CommonMark)."""
    m = _FENCE_RE.match(line)
    return bool(m) and len(m.group(1)) >= opened and not m.group(2).strip()


def segment_answer(text: str) -> list[Block]:
    """Сегментирует ответ на прозовые прогоны (verbatim, пустые строки внутри
    сохранены, по краям срезаны) и code/table-блоки. kind ∈ {'prose','code-block','table'}."""
    segs: list[Block] = []
    lines = text.split("\n")
    i = 0
    prose: list[str] = []

    def _flush() -> None:
        run = prose[:]
        while run and not run[0].strip():
            run.pop(0)
        while run and not run[-1].strip():
            run.pop()
        if run:
            segs.append(Block("prose", "\n".join(run)))
        prose.clear()

    while i < len(lines):
        ln = lines[i]
        opened = _fence_open(ln)
        if opened is not None:
            _flush()
            fence_len, lang_tag = opened
            j = i + 1
            chunk: list[str] = []
            while j < len(lines) and not _fence_closes(lines[j], fence_len):
                chunk.append(lines[j])
                j += 1
            content = "\n".join(chunk)
            # Пустой блок панелью не становится (Е2): ни кнопки копирования, ни рамки
            # вокруг пустоты — обе половины ленты (финал и живой путь) читают сегменты.
            if content.strip():
                segs.append(Block("code-block", content, lang_tag))
            i = j + 1
            continue
        if (
            "|" in ln
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1])
        ):
            _flush()
            headers = _split_table_row(ln)
            ncols = len(headers)
            aligns = _parse_table_aligns(lines[i + 1], ncols)
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(
                    (_split_table_row(lines[j]) + [""] * ncols)[:ncols]
                )
                j += 1
            segs.append(
                Block("table", headers=headers, rows=rows, aligns=aligns)
            )
            i = j
            continue
        prose.append(ln)
        i += 1
    _flush()
    return segs


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _apply_inline_styles(text: Text, line: str) -> None:
    """Apply **bold** и `code` спаны к одной строке."""
    matches: list[tuple[int, int, str, str]] = []
    for m in _BOLD_RE.finditer(line):
        matches.append((m.start(), m.end(), "bold", m.group(1)))
    for m in _INLINE_CODE_RE.finditer(line):
        matches.append((m.start(), m.end(), "code", m.group(1)))
    matches.sort(key=lambda x: x[0])

    # Не пересекающиеся в порядке появления
    final: list[tuple[int, int, str, str]] = []
    cur_end = 0
    for start, end, kind, content in matches:
        if start >= cur_end:
            final.append((start, end, kind, content))
            cur_end = end

    pos = 0
    for start, end, kind, content in final:
        if start > pos:
            text.append(line[pos:start], style=_INK)
        if kind == "bold":
            text.append(content, style=f"{_ACC} bold")
        else:  # code
            text.append(content, style=_CODE)
        pos = end
    if pos < len(line):
        text.append(line[pos:], style=_INK)


_PROSE_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_PROSE_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def render_prose(text: str) -> Text:
    """Прозовый рендер (стрим и финал): построчно inline-стили (**bold**, `code`) + line-стили
    heading/bullet; fenced-блоки и таблицы — plain (рамки/сетка только на финале).
    Возвращает rich.Text в нормальном цвете (INK)."""
    out = Text()
    in_fence = 0  # 0 — вне блока, иначе длина открывающей ограды
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append("\n")
        if in_fence:
            if _fence_closes(line, in_fence):
                in_fence = 0
            out.append(line, style=_INK)
            continue
        opened = _fence_open(line)
        if opened is not None:
            in_fence = opened[0]
            out.append(line, style=_INK)
            continue
        m = _PROSE_HEADING_RE.match(line)
        if m:
            out.append(m.group(2).strip(), style=f"{_ACC} bold")
            continue
        if _PROSE_BULLET_RE.match(line):
            out.append("• ", style=_INK)
            _apply_inline_styles(out, _PROSE_BULLET_RE.sub("", line))
            continue
        _apply_inline_styles(out, line)
    return out


def build_markdown_table(
    headers: list[str], rows: list[list[str]], aligns: list[str]
) -> Table:
    """Структура таблицы → rich.Table (полная сетка box.SQUARE, рамка dim,
    заголовок accent-bold, в ячейках — inline-разметка). Ширину не фиксируем
    здесь: её задаёт вызывающий `table_render.table_to_text(..., width=)`,
    которым пользуется `BlockPanel._refresh_body`."""
    import rich.box as _box
    from rich.table import Table
    from rich.text import Text as _Text

    table = Table(
        box=_box.SQUARE,
        border_style=_DEEP_DIM,
        show_header=True,
        expand=False,
        pad_edge=True,
    )
    for h, a in zip(headers, aligns):
        table.add_column(h, justify=a, header_style=f"{_ACC} bold")
    for row in rows:
        cells: list[_Text] = []
        for cell in row:
            t = _Text()
            _apply_inline_styles(t, cell)
            cells.append(t)
        table.add_row(*cells)
    return table


def table_block_to_markdown(
    headers: list[str], rows: list[list[str]], aligns: list[str]
) -> str:
    """Реконструировать валидный markdown таблицы из Block (для clipboard).

    left → ``---`` (GFM-дефолт). ``|`` в ячейке → ``\\|`` для вставки в редактор.
    Round-trip через текущий segment_answer для ячеек с ``|`` НЕ обещаем:
    парсер режет по сырому ``|`` без escape.
    """

    def esc(cell: str) -> str:
        return cell.replace("|", r"\|")

    lines = ["| " + " | ".join(esc(h) for h in headers) + " |"]
    sep: list[str] = []
    for a in aligns:
        if a == "center":
            sep.append(":---:")
        else:
            sep.append("---:" if a == "right" else "---")
    lines.append("| " + " | ".join(sep) + " |")
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)
