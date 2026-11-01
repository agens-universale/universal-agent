"""Приглашение поля ввода — один знак: главное поле, фильтры, реплика."""

import re
from pathlib import Path

import gc_tui
from gc_tui.services.trace_glyphs import PROMPT
from gc_tui.services.trace_render import USER_PREFIX


def test_reply_mark_is_the_prompt():
    assert USER_PREFIX == PROMPT


def test_no_input_prompt_spells_its_own_glyph():
    """Поля `*-prompt` берут `PROMPT`, а не литерал (старый `> ` жил в
    фильтрах `/sessions`, `/skills`, `/config`)."""
    literal = re.compile(r'Static\(\s*"[^"]*",\s*id="[\w-]*prompt"')
    root = Path(gc_tui.__file__).parent
    hits = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if literal.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
