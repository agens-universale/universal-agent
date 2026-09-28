"""render_payload: JSON → Syntax(monokai), plain → Static(моноцвет)."""

import json

import pytest


def _content_text(widget) -> str:
    """Извлечь рендеримый текст из виджета render_payload без App-контекста.

    render_payload для dict/list возвращает контейнер PayloadBlocks с дочерними
    Static (каждый рендерится в rich.Text — контракт селектируемости); для
    строк/прочего — одиночный Static(Text).
    """

    def _one(w) -> str:
        content = getattr(w, "content", "")
        if hasattr(content, "plain"):
            return content.plain
        return str(content)

    children = list(
        getattr(widget, "_pending_children", []) or widget.children
    )
    if not children:
        return _one(widget)
    return "".join(_one(c) for c in children)


def test_dict_returns_widget_with_json_content():
    pytest.importorskip("textual")
    from gc_tui.services.json_payload import render_payload

    w = render_payload({"ok": True, "count": 5})
    rendered = _content_text(w)
    assert "ok" in rendered
    assert "count" in rendered


def test_list_returns_widget_with_json_content():
    pytest.importorskip("textual")
    from gc_tui.services.json_payload import render_payload

    w = render_payload([1, 2, 3])
    rendered = _content_text(w)
    assert "1" in rendered
    assert "2" in rendered
    assert "3" in rendered


def test_string_valid_json_pretty_prints():
    pytest.importorskip("textual")
    from gc_tui.services.json_payload import render_payload

    raw = json.dumps({"a": 1, "b": [10, 20]})
    w = render_payload(raw)
    rendered = _content_text(w)
    # После pretty-print появляются переносы строк (indent=2)
    assert "a" in rendered and "b" in rendered


def test_string_plain_text_falls_back():
    pytest.importorskip("textual")
    from gc_tui.services.json_payload import render_payload

    raw = "this is not json at all"
    w = render_payload(raw)
    rendered = _content_text(w)
    assert "this is not json at all" in rendered


def test_string_empty_returns_widget():
    pytest.importorskip("textual")
    from gc_tui.services.json_payload import render_payload

    w = render_payload("")
    # Должен вернуть валидный widget, без exception. Контент может быть пустым.
    assert w is not None


def test_payload_blocks_use_block_bg():
    """Поверхность payload — тот же оттенок, что у код-, таблица- и дифф-блоков.

    Фон красится в трёх местах: CSS контейнера, подсветка JSON-каркаса
    (`Syntax(background_color=…)`) и многострочные блоки. Разъедься они — и под
    одним рендером лежали бы две подложки разной глубины, поэтому проверяются
    все три разом: единственный фон рендера и есть контракт.
    """
    pytest.importorskip("textual")
    from rich.console import Console

    from gc_tui import palette
    from gc_tui.services.json_payload import PayloadBlocks, render_payload

    console = Console()
    widget = render_payload({"content": "первая\nвторая", "ok": True})
    children = list(getattr(widget, "_pending_children", []) or widget.children)
    backgrounds = {
        text.get_style_at_offset(console, offset).bgcolor
        for text in (child.content for child in children)
        for offset in range(len(text.plain))
    }

    assert {bg.name for bg in backgrounds if bg} == {palette.BLOCK_BG}
    assert "$block-bg" in PayloadBlocks.DEFAULT_CSS
