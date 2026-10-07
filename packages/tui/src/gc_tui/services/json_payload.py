"""render_payload: единый рендер «развёрнутого» JSON-like payload.

Используется ToolCallDetailScreen для CALL и OUTPUT секций.
Payload рекурсивно разворачивается (unfold_payload): JSON-в-строке заменяется
распарсенным значением, чтобы вложенный экранированный JSON (stored_tool_output.content
и т.п.) не оставался однострочной колбасой. Многострочные строковые значения
рендерятся блоком с реальными переносами и левой полосой ``│ ``; JSON-каркас —
pretty-print + rich.syntax(solarized-dark). Markdown не интерпретируем:
отладочный экран показывает сырые данные.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from rich.syntax import Syntax
from rich.text import Text
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.palette import BLOCK_BG, CODE_BLOCK

_MAX_UNFOLD_DEPTH = 5


class PayloadBlocks(Vertical):
    """Контейнер блоков payload: каждый блок — свой Static с Text-рендером.

    Text-рендер — контракт селектируемости: Widget.get_selection отдаёт текст
    только для Text/Content; Syntax/Group дают None, и Textual молча исключает
    виджет из мышиного выделения (главный трейс селектируем именно потому,
    что его строки рендерятся в rich.Text).
    """

    DEFAULT_CSS = """
    PayloadBlocks { height: auto; width: 1fr; }
    PayloadBlocks > Static { height: auto; width: 1fr; background: $block-bg; }
    """


def unfold_payload(content: Any, max_depth: int = _MAX_UNFOLD_DEPTH) -> Any:
    """Рекурсивно развернуть JSON-в-строке.

    Строка, парсящаяся в dict/list, заменяется распарсенным значением (и так
    вглубь, до max_depth). Прочие значения возвращаются как есть. Экран события
    — отладочный просмотр: разворачивание НЕ теряет данные, только форму."""
    if max_depth <= 0:
        return content
    if isinstance(content, str):
        s = content.strip()
        if s.startswith(("{", "[")):
            try:
                parsed = json.loads(s)
            except ValueError:
                parsed = _parse_truncated_json(s)
                if parsed is None:
                    return content
            if isinstance(parsed, (dict, list)):
                return unfold_payload(parsed, max_depth - 1)
        return content
    if isinstance(content, dict):
        return {
            k: unfold_payload(v, max_depth - 1) for k, v in content.items()
        }
    if isinstance(content, list):
        return [unfold_payload(v, max_depth - 1) for v in content]
    return content


# Хвостовые обрезки оборванного JSON (все три — только НЕ-скобочные символы,
# так что стек закрывашек после стрижки не меняется).
_BARE_TAIL = re.compile(r'[^\s,:{}\[\]"]+$')  # голый литерал: 12 / tru / nul
_KEY_TAIL = re.compile(
    r'"(?:[^"\\]|\\.)*"\s*:?\s*$'
)  # висящий ключ: "trunca" / "count":
_SEP_TAIL = re.compile(r"[,:]\s*$")  # висящий разделитель


def _parse_truncated_json(s: str) -> Optional[Any]:
    """Best-effort парс JSON, обрезанного на полуслове (stored_tool_output и т.п.).

    Escape-aware скан считает открытые скобки и состояние «внутри строки», после
    чего оборванная строка/скобки закрываются, а недописанный хвост (голый
    литерал, висящий ключ, разделитель) отбрасывается. Ничего не выдумывается —
    только закрытие структур и стрижка хвоста; о самом факте обрезки говорит
    конверт (``"truncated": true``). Не похоже на обрыв (мусор, дисбаланс) → None.
    """
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None  # дисбаланс — это не обрыв, чинить нечего
            stack.pop()
    if not stack and not in_str:
        return None  # сбалансирован, но не парсится — не наш случай
    base = s[:-1] if esc else s  # оборванная escape-последовательность
    if in_str:
        # обрыв посреди строки: закрытая строка — валидное значение, текст до
        # места обрыва сохраняется целиком; фолбэк — обрыв посреди ключа
        base += '"'
        candidates = [base, _strip_dangling(base)]
    else:
        # обрыв в структурной позиции: голый литерал на конце всегда подозрителен
        # (12 могло быть 125) — стрижём сразу; фолбэк добирает ключ/разделитель
        candidates = [_strip_bare_literal(base), _strip_dangling(base)]
    closers = "".join(reversed(stack))
    for cand in candidates:
        try:
            return json.loads(cand + closers)
        except ValueError:
            continue
    return None


def _strip_bare_literal(t: str) -> str:
    """Срезать голый литерал на конце (недописанное число / tru / nul).

    true/false/null — завершённые токены (длиннее не бывают), их не трогаем."""
    t = t.rstrip()
    m = _BARE_TAIL.search(t)
    if m and m.group(0) not in ("true", "false", "null"):
        t = t[: m.start()].rstrip()
    return t


def _strip_dangling(t: str) -> str:
    """Срезать недописанный хвост: голый литерал → висящий ключ → разделитель."""
    t = _strip_bare_literal(t)
    t = _KEY_TAIL.sub("", t).rstrip()
    t = _SEP_TAIL.sub("", t).rstrip()
    return t


def render_payload(content: Any) -> Widget:
    """Сериализовать и отрендерить payload как Widget.

    - dict / list (в т.ч. полученные разворачиванием JSON-строки) → render_unfolded
    - прочие строки → plain Static
    - другое → str(content) как plain
    """
    data = unfold_payload(content)
    if isinstance(data, (dict, list)):
        return render_unfolded(data)
    if isinstance(data, str):
        return Static(Text(data, style=CODE_BLOCK))
    return Static(Text(str(data), style=CODE_BLOCK))


def render_unfolded(data: dict | list) -> Widget:
    """Отрендерить развёрнутую структуру.

    JSON-каркас идёт через Syntax; многострочные строковые значения (с ``\\n``)
    вынимаются из каркаса и рендерятся отдельными Text-блоками с реальными
    переносами: каждая строка с префиксом ``│ `` и отступом уровня ключа.
    """
    blocks: dict[str, str] = {}
    # uuid-суффикс: без него токен-плейсхолдер мог совпасть с literal-текстом
    # payload (строковое значение, содержащее буквально "@@GC-MLBLOCK-0@@"),
    # ложно триггеря матч по подстроке ниже.
    token_suffix = uuid.uuid4().hex[:8]

    def _extract(value: Any) -> Any:
        if isinstance(value, str) and "\n" in value:
            token = f"@@GC-MLBLOCK-{len(blocks)}-{token_suffix}@@"
            blocks[token] = value
            return token
        if isinstance(value, dict):
            return {k: _extract(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_extract(v) for v in value]
        return value

    skeleton = _extract(data)
    pretty = json.dumps(skeleton, indent=2, ensure_ascii=False)
    if not blocks:
        return PayloadBlocks(Static(_highlighted(pretty)))

    renderables = []
    json_buf: list[str] = []

    def _flush_json() -> None:
        if json_buf:
            renderables.append(_highlighted("\n".join(json_buf)))
            json_buf.clear()

    for line in pretty.splitlines():
        # Матч только целой закавыченной формы токена — иначе однострочное
        # строковое значение, содержащее literal-текст токена, ложно матчится.
        token = next((t for t in blocks if f'"{t}"' in line), None)
        if token is None:
            json_buf.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        head, _, tail = line.partition(f'"{token}"')
        head = head.rstrip()  # '  "content":' | '' (элемент списка)
        tail = tail.strip()  # ',' (не последний элемент/поле) | '' (последний)
        if head:
            json_buf.append(head)
        _flush_json()
        renderables.append(_multiline_block(blocks[token], indent))
        if tail:
            json_buf.append(" " * indent + tail)
    _flush_json()
    return PayloadBlocks(*[Static(r) for r in renderables])


def _multiline_block(value: str, indent: int) -> Text:
    prefix = " " * indent + "│ "
    text = Text()
    for line in value.replace("\r\n", "\n").split("\n"):
        text.append(prefix + line + "\n", style=f"{CODE_BLOCK} on {BLOCK_BG}")
    return text


def _highlighted(pretty_json: str) -> Text:
    # Syntax.highlight -> уже раскрашенный Text той же темы; сам Syntax как
    # renderable не годится: get_selection требует Text-рендер (см. PayloadBlocks).
    # word_wrap=True: длинные строки JSON-payload (stdout/stderr из execute_command,
    # arguments с длинными command, content из read_file) переносятся на следующую
    # визуальную линию, иначе обрезаются за правым краем без горизонтального скролла.
    # Альтернативы темы (pygments built-in, для тёмного фона):
    #   приглушённые: solarized-dark (текущее), nord, gruvbox-dark, zenburn,
    #                 one-dark, material, paraiso-dark, lightbulb, stata-dark
    #   контрастные:  monokai, dracula, github-dark, native, fruity, inkpot
    # Если хочется почти-без-цвета: bw (black/white), friendly_grayscale.
    syntax = Syntax(
        "",
        lexer="json",
        theme="solarized-dark",  # solarized-dark | monokai | nord
        background_color=BLOCK_BG,
        word_wrap=True,
    )
    text = syntax.highlight(pretty_json)
    text.rstrip()  # highlight добавляет хвостовой \n -> лишняя пустая строка
    return text
