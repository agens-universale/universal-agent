"""render_payload: единый рендер «развёрнутого» JSON-like payload.

Используется ToolCallDetailScreen для CALL и OUTPUT секций.
Pretty-print + rich.syntax(solarized-dark) для JSON, plain Static для всего остального.
"""
from __future__ import annotations

import json
from typing import Any

from rich.syntax import Syntax
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static

from tui.palette import BAND_BG, CODE_BLOCK


def render_payload(content: Any) -> Widget:
    """Сериализовать и отрендерить payload как Widget.

    - dict / list → pretty-print + Syntax(json, solarized-dark, bg=$band-bg)
    - str: try json.loads; если ok → pretty-print + Syntax; иначе plain Static
    - другое → str(content) как plain
    """
    if isinstance(content, (dict, list)):
        pretty = json.dumps(content, indent=2, ensure_ascii=False)
        return _syntax_widget(pretty)

    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return Static(Text(content, style=CODE_BLOCK))
        if isinstance(parsed, (dict, list)):
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            return _syntax_widget(pretty)
        return Static(Text(content, style=CODE_BLOCK))

    return Static(Text(str(content), style=CODE_BLOCK))


def _syntax_widget(pretty_json: str) -> Static:
    # word_wrap=True: длинные строки JSON-payload (stdout/stderr из execute_command,
    # arguments с длинными command, content из read_file) переносятся на следующую
    # визуальную линию, иначе обрезаются за правым краем без горизонтального скролла.
    # Альтернативы темы (pygments built-in, для тёмного фона):
    #   приглушённые: solarized-dark (текущее), nord, gruvbox-dark, zenburn,
    #                 one-dark, material, paraiso-dark, lightbulb, stata-dark
    #   контрастные:  monokai, dracula, github-dark, native, fruity, inkpot
    # Если хочется почти-без-цвета: bw (black/white), friendly_grayscale.
    syntax = Syntax(
        pretty_json,
        lexer="json",
        theme="solarized-dark",  # solarized-dark | monokai | nord
        background_color=BAND_BG,
        word_wrap=True,
    )
    return Static(syntax)
