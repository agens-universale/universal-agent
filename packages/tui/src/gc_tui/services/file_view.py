"""TUI file viewer: resolve / probe / windowed read / in-memory позиция.

Кроссплатформенное чтение текстового файла для `/view` — без shell.
Не пишет в thread jsonl; позиция страницы живёт только в процессе TUI.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from pygments import lex
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from rich.syntax import Syntax

from gc_tui.palette import BAND_BG

DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_COUNT_EAGER_BYTES = 2 * 1024 * 1024
# Пока layout не дал высоту тела — временный размер страницы (только auto-режим).
DEFAULT_PAGE_LINES_FALLBACK = 20
PAGE_LINES_MIN = 1
PAGE_LINES_MAX = 200
BINARY_PROBE_BYTES = 8192
MAX_LINE_CHARS = 4000
# Как yaml_view / json_payload — один стиль с /config.
_VIEW_SYNTAX_THEME = "solarized-dark"
_PLAIN_LEXER_ALIASES = frozenset({"text", "txt", "output", "plaintext"})
MSG_USAGE = "использование: /view <path>"
MSG_DISABLED = "просмотр файлов выключен"
MSG_NOT_FOUND = "файл не найден"
MSG_IS_DIR = "это каталог, укажите файл"
MSG_BINARY = "бинарный файл, просмотр не поддерживается"
MSG_TOO_LARGE = "файл слишком большой"
MSG_EMPTY = "(пустой файл)"


def clamp_page_lines(value: int) -> int:
    return max(PAGE_LINES_MIN, min(PAGE_LINES_MAX, int(value)))


def lexer_name_for_path(path: Path) -> Optional[str]:
    """Pygments lexer alias по имени файла; plaintext / неизвестное → None."""
    try:
        lexer = get_lexer_for_filename(path.name)
    except ClassNotFound:
        return None
    aliases = {a.lower() for a in (lexer.aliases or ())}
    if aliases & _PLAIN_LEXER_ALIASES:
        return None
    if (lexer.name or "").lower() in {"text only", "text"}:
        return None
    if not lexer.aliases:
        return None
    return lexer.aliases[0]


# Pygments alias → TextArea.language (tree-sitter). Нет в наборе → None.
_TEXTAREA_LANG_MAP = {
    "python": "python",
    "javascript": "javascript",
    "js": "javascript",
    "typescript": "javascript",  # отдельного ts в TextArea нет
    "ts": "javascript",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "html": "html",
    "css": "css",
    "xml": "xml",
    "markdown": "markdown",
    "md": "markdown",
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "rust": "rust",
    "go": "go",
    "java": "java",
    "sql": "sql",
    "tsql": "sql",
}


def textarea_language_for_lexer(lexer: Optional[str]) -> Optional[str]:
    """Pygments alias → language id TextArea; неизвестное → None (без подсветки)."""
    if not lexer:
        return None
    return _TEXTAREA_LANG_MAP.get(lexer.lower())


# Pygments Token → имя стиля TextAreaTheme.syntax_styles (vscode_dark и др.).
# Ищем от конкретного типа вверх по иерархии Token.
_PYGMENTS_TO_TA_STYLE: dict[Any, str] = {
    Token.Keyword.Constant: "constant.builtin",
    Token.Keyword.Namespace: "include",
    Token.Keyword.Type: "type",
    Token.Keyword: "keyword",
    Token.Name.Function: "function",
    Token.Name.Class: "class",
    Token.Name.Decorator: "function",
    Token.Name.Builtin: "type.builtin",
    Token.Name.Tag: "tag",
    Token.Name.Attribute: "css.property",
    Token.Name.Label: "json.label",
    Token.Name.Constant: "constant.builtin",
    Token.String.Doc: "string.documentation",
    Token.String: "string",
    Token.Literal.String: "string",
    Token.Comment: "comment",
    Token.Number: "number",
    Token.Literal.Number: "number",
    Token.Operator: "operator",
    Token.Punctuation: "punctuation.delimiter",
    Token.Generic.Heading: "heading",
    Token.Generic.Subheading: "heading",
}


def _textarea_style_for_token(ttype: Any) -> Optional[str]:
    while ttype is not None:
        mapped = _PYGMENTS_TO_TA_STYLE.get(ttype)
        if mapped is not None:
            return mapped
        ttype = getattr(ttype, "parent", None)
    return None


def pygments_highlight_map(
    text: str,
    *,
    lexer: Optional[str],
) -> dict[int, list[tuple[int, Optional[int], str]]]:
    """Pygments → карта подсветок TextArea: line → [(byte_start, byte_end, style)].

    TextArea без `textual[syntax]` (tree-sitter) сам не красит; эта карта
    пишется в `TextArea._highlights` и рендерится штатной темой.
    """
    out: dict[int, list[tuple[int, Optional[int], str]]] = defaultdict(list)
    if not lexer or not text:
        return out
    try:
        pyg_lexer = get_lexer_by_name(lexer, stripnl=False)
    except ClassNotFound:
        return out

    line_idx = 0
    col_byte = 0
    for ttype, value in lex(text, pyg_lexer):
        if not value:
            continue
        style = _textarea_style_for_token(ttype)
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i:
                line_idx += 1
                col_byte = 0
            if not part:
                continue
            part_bytes = part.encode("utf-8")
            if style is not None:
                start = col_byte
                end = col_byte + len(part_bytes)
                out[line_idx].append((start, end, style))
            col_byte += len(part_bytes)
    return out


def apply_pygments_highlights(
    text_area: Any,
    text: str,
    *,
    lexer: Optional[str],
) -> None:
    """Заполнить `text_area._highlights` из pygments и перерисовать."""
    highlights = getattr(text_area, "_highlights", None)
    if highlights is None:
        return
    highlights.clear()
    mapped = pygments_highlight_map(text, lexer=lexer)
    for line_idx, spans in mapped.items():
        highlights[line_idx] = list(spans)
    line_cache = getattr(text_area, "_line_cache", None)
    if line_cache is not None and hasattr(line_cache, "clear"):
        line_cache.clear()
    refresh = getattr(text_area, "refresh", None)
    if callable(refresh):
        refresh()


def view_window_renderable(
    text: str,
    *,
    lexer: Optional[str],
) -> Union[Syntax, str]:
    """Окно файла → Syntax (если есть lexer) или plain str для Static.update."""
    if not lexer:
        return text
    return Syntax(
        text,
        lexer=lexer,
        theme=_VIEW_SYNTAX_THEME,
        background_color=BAND_BG,
        word_wrap=False,
        line_numbers=False,
    )


def parse_page_lines(raw: Any) -> Optional[int]:
    """None / omit / <=0 / bad → auto (None); иначе clamp 1…200."""
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return clamp_page_lines(n)


@dataclass(frozen=True)
class FileViewSettings:
    enabled: bool = True
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    # None = авто по высоте тела Screen; int = фиксированный размер страницы.
    page_lines: Optional[int] = None

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> FileViewSettings:
        if not data:
            return cls()
        try:
            max_bytes = int(data.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_FILE_BYTES
        if max_bytes <= 0:
            max_bytes = DEFAULT_MAX_FILE_BYTES
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_file_bytes=max_bytes,
            page_lines=parse_page_lines(data.get("page_lines")),
        )


@dataclass(frozen=True)
class ViewWindow:
    """Окно строк; start_line / end_line — 1-based включительно."""

    start_line: int
    end_line: int
    lines: tuple[str, ...]
    total_lines: Optional[int]


@dataclass(frozen=True)
class OpenOk:
    path: Path
    size_bytes: int
    total_lines: Optional[int]


@dataclass(frozen=True)
class OpenErr:
    message: str
    display_path: str = ""


OpenResult = OpenOk | OpenErr


class ViewPositionStore:
    """In-memory start_line по resolved path; жизнь = процесс TUI."""

    def __init__(self) -> None:
        self._pos: dict[str, int] = {}

    @staticmethod
    def key_for(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    def get(self, path: Path) -> Optional[int]:
        return self._pos.get(self.key_for(path))

    def set(self, path: Path, start_line: int) -> None:
        self._pos[self.key_for(path)] = max(1, int(start_line))


def resolve_view_path(raw: str, *, cwd: str) -> Path:
    """expanduser + relative→cwd; без проверки exists."""
    text = (raw or "").strip()
    p = Path(text).expanduser()
    if not p.is_absolute():
        p = Path(cwd or ".").expanduser() / p
    return p


def is_probably_binary(path: Path, *, probe_bytes: int = BINARY_PROBE_BYTES) -> bool:
    """Эвристика по NUL в первых probe_bytes (как bang truncate)."""
    try:
        with path.open("rb") as f:
            chunk = f.read(probe_bytes)
    except OSError:
        return False
    if not chunk:
        return False
    nul = chunk.count(b"\x00")
    return nul >= max(3, len(chunk) // 4)


def count_lines(path: Path) -> int:
    n = 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for _ in f:
            n += 1
    return n


def read_window(
    path: Path,
    *,
    start_line: int,
    line_count: int,
    total_lines: Optional[int] = None,
    max_line_chars: int = MAX_LINE_CHARS,
) -> ViewWindow:
    """Прочитать окно строк с 1-based start_line; не грузить весь файл."""
    start = max(1, int(start_line))
    count = max(1, int(line_count))
    out: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for _ in range(start - 1):
            if f.readline() == "":
                return ViewWindow(
                    start_line=start,
                    end_line=start - 1,
                    lines=(),
                    total_lines=total_lines,
                )
        for _ in range(count):
            line = f.readline()
            if line == "":
                break
            text = line.rstrip("\r\n")
            if max_line_chars > 0 and len(text) > max_line_chars:
                text = text[:max_line_chars] + "…"
            out.append(text)
    end = start + len(out) - 1 if out else start - 1
    return ViewWindow(
        start_line=start,
        end_line=end,
        lines=tuple(out),
        total_lines=total_lines,
    )


def clamp_start_line(start_line: int, *, total_lines: int, page: int) -> int:
    if total_lines <= 0:
        return 1
    page = max(1, page)
    max_start = max(1, total_lines - page + 1)
    return max(1, min(int(start_line), max_start))


def open_for_view(
    raw: str,
    *,
    cwd: str,
    settings: Optional[FileViewSettings] = None,
    count_eager_bytes: int = DEFAULT_COUNT_EAGER_BYTES,
) -> OpenResult:
    """Probe path для `/view`: Ok или Err с текстом для тела Screen."""
    settings = settings or FileViewSettings()
    text = (raw or "").strip()
    if not text:
        return OpenErr(message=MSG_USAGE)

    if not settings.enabled:
        return OpenErr(message=MSG_DISABLED, display_path=text)

    path = resolve_view_path(text, cwd=cwd)
    display = str(path)

    try:
        if not path.exists():
            return OpenErr(message=f"{MSG_NOT_FOUND}: {display}", display_path=display)
        if path.is_dir():
            return OpenErr(message=MSG_IS_DIR, display_path=display)
        if not path.is_file():
            return OpenErr(message=f"{MSG_NOT_FOUND}: {display}", display_path=display)
        size = path.stat().st_size
    except OSError as exc:
        return OpenErr(message=str(exc) or exc.__class__.__name__, display_path=display)

    if size > settings.max_file_bytes:
        mib = max(1, settings.max_file_bytes // (1024 * 1024))
        return OpenErr(
            message=f"{MSG_TOO_LARGE} (лимит {mib} MiB)",
            display_path=display,
        )

    try:
        if is_probably_binary(path):
            return OpenErr(message=MSG_BINARY, display_path=display)
    except OSError as exc:
        return OpenErr(message=str(exc) or exc.__class__.__name__, display_path=display)

    total: Optional[int] = None
    if size <= count_eager_bytes:
        try:
            total = count_lines(path)
        except OSError as exc:
            return OpenErr(message=str(exc) or exc.__class__.__name__, display_path=display)

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    return OpenOk(path=resolved, size_bytes=size, total_lines=total)


def format_range_label(
    *,
    start_line: int,
    end_line: int,
    total_lines: Optional[int],
    empty: bool = False,
) -> str:
    if empty:
        if total_lines is None:
            return "— / ?"
        return f"— / {total_lines}"
    if end_line < start_line:
        span = "—"
    else:
        span = f"{start_line}–{end_line}"
    if total_lines is None:
        return f"{span} / ?"
    return f"{span} / {total_lines}"
