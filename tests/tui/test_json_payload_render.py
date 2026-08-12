"""Рендер payload: вложенный JSON-в-строке разворачивается, \\n — реальные переносы.

Все фикстуры — РЕАЛЬНЫЕ записи из живых тредов (`~/.gc/projects/*/threads/`),
не синтетика (правило real-payload-first). Их три, потому что ни одна реальная
запись не покрывает все кейсы сразу:

- ``stored_tool_output_truncated.json`` — запись stored_tool_output из треда
  ``data_cli_1319dcec...`` (source_tool=fs_list_files legacy): поле ``content`` —
  ОБОРВАННЫЙ на inline-лимите (10000 симв.) JSON, который не парсится.
  В реальных данных content у stored_tool_output всегда усечён (стор срабатывает
  только на больших выводах) — проверяем, что разворачивание не падает и
  честно оставляет обрывок строкой.
- ``intent_log_nested.json`` — запись session_read_user_intent_log
  (тред ``data_cli_8fa96000...``): поле ``content`` — ЦЕЛАЯ экранированная
  JSON-строка → единственный живой пример разворачивания вложенного JSON в dict.
- ``fs_read_file_multiline.json`` — запись fs_read_file из того же треда
  ``data_cli_1319dcec...``: поле ``content`` — многострочный текст файла
  (24 реальных ``\\n``) → живой пример блочного рендера с реальными переносами.
"""
import json
import uuid
from pathlib import Path

import pytest

pytest.importorskip("textual")

import tui.services.json_payload as json_payload_module
from tui.services.json_payload import render_payload, unfold_payload

_FIXTURES = Path(__file__).parent / "fixtures"
STORED_TRUNCATED = (_FIXTURES / "stored_tool_output_truncated.json").read_text(encoding="utf-8")
INTENT_LOG = (_FIXTURES / "intent_log_nested.json").read_text(encoding="utf-8")
FS_READ_MULTILINE = (_FIXTURES / "fs_read_file_multiline.json").read_text(encoding="utf-8")

_BACKSLASH_N = chr(92) + "n"  # литеральные два символа \n (не перевод строки)


def _plain_text(widget) -> str:
    """Извлечь рендеримый текст из виджета render_payload.

    render_payload для dict/list возвращает контейнер PayloadBlocks с дочерними
    Static (каждый рендерится в rich.Text — это контракт селектируемости);
    для строк — одиночный Static(Text).
    """
    def _one(w) -> str:
        content = getattr(w, "content", "")
        if hasattr(content, "plain"):
            return content.plain
        return str(content)

    children = list(getattr(widget, "_pending_children", []) or widget.children)
    if not children:
        return _one(widget)
    # "": каждая часть несёт свои переносы; склейка через "\n" вставляла бы
    # лишнюю пустую строку на стыке (см. прежний комментарий).
    return "".join(_one(c) for c in children)


# --- unfold_payload: чистая функция, без Textual ---


def test_unfold_parses_top_level_string():
    data = unfold_payload(STORED_TRUNCATED)
    assert isinstance(data, dict)
    assert data["type"] == "stored_tool_output"


def test_unfold_truncated_content_repaired_to_dict():
    """Оборванный JSON внутри content чинится для показа (закрываются строка
    и скобки, оборванный хвост отбрасывается) → разворачивается как dict,
    а не остаётся однострочной экранированной колбасой."""
    data = unfold_payload(STORED_TRUNCATED)
    inner = data["content"]
    assert isinstance(inner, dict)
    assert inner["tool"] == "fs_list_files"
    assert isinstance(inner["entries"], list)
    assert inner["entries"], "распарсенные элементы до места обрыва сохранены"


def test_unfold_truncated_readfile_markdown_content():
    """Кейс со скриншота: stored_tool_output, content = JSON read_file-конверта
    с markdown-текстом, обрезанный посреди текста. После починки — dict, а
    markdown внутри — строка с реальными переносами (для multiline-блока)."""
    md = "# SPEC.md — Унифицированный CLI\n\n## 1. Objective\nОбъединить `chat.py`,\n" * 40
    inner = json.dumps({"tool": "fs_read_file", "ok": True,
                        "path": "D:\\dev\\SPEC.md", "content": md},
                       ensure_ascii=False)
    envelope = {"type": "stored_tool_output", "ok": True, "truncated": True,
                "content": inner[:len(inner) // 2]}   # обрыв посреди markdown
    data = unfold_payload(json.dumps(envelope, ensure_ascii=False))
    repaired = data["content"]
    assert isinstance(repaired, dict)
    assert repaired["tool"] == "fs_read_file"
    assert "\n" in repaired["content"]           # реальные переносы, не \\n
    assert repaired["content"].startswith("# SPEC.md")


def test_unfold_truncated_mid_key_and_mid_number():
    """Обрыв на середине ключа / числа: хвост отбрасывается, остальное парсится."""
    mid_key = '{"ok": true, "count": 200, "trunca'
    data = unfold_payload(mid_key)
    assert data == {"ok": True, "count": 200}
    mid_number = '{"ok": true, "count": 12'
    data = unfold_payload(mid_number)
    assert data == {"ok": True}


def test_unfold_unrepairable_stays_string():
    """Строка, похожая на JSON, но не являющаяся им (не обрыв, а мусор) —
    честно остаётся строкой."""
    garbage = '{"a": 1} trailing garbage}'
    assert unfold_payload(garbage) == garbage


def test_unfold_expands_nested_json_string():
    """Ключевой кейс со скриншота: content — строка с целым JSON внутри → dict."""
    data = unfold_payload(INTENT_LOG)
    assert isinstance(data["content"], dict)  # был str "{\"turn\": ...}\n"
    assert "turn" in data["content"]


def test_unfold_depth_limit():
    """Глубже max_depth не разворачиваем (защита от патологий)."""
    nested = '"x"'
    for _ in range(8):
        nested = json.dumps({"v": nested})
    data = unfold_payload(nested, max_depth=2)
    # на глубине 2 значение остаётся строкой, а не продолжает разворачиваться
    d, depth = data, 0
    while isinstance(d, dict):
        d, depth = d["v"], depth + 1
    assert depth <= 3


def test_unfold_plain_dict_unchanged():
    """Обычный плоский payload — как раньше, без сюрпризов."""
    src = {"name": "exec_fetch_url", "status": "completed", "n": 5, "ok": True}
    assert unfold_payload(src) == src


def test_unfold_non_json_string_stays_string():
    assert unfold_payload("просто текст, не JSON") == "просто текст, не JSON"


# --- render_payload: развёрнутый рендер ---


def test_render_truncated_stored_output_unfolded():
    """Каркас stored_tool_output развёрнут как JSON, без падений,
    без литеральных \\n в plain-тексте."""
    w = render_payload(STORED_TRUNCATED)
    text = _plain_text(w)
    assert "stored_tool_output" in text
    assert '"reason"' in text
    assert "\n" in text  # pretty-print: реальные переводы строк
    assert _BACKSLASH_N not in text


def test_render_nested_json_expanded():
    """Вложенный JSON виден развёрнутым, а не экранированной колбасой."""
    w = render_payload(INTENT_LOG)
    text = _plain_text(w)
    assert '"turn"' in text  # ключ вложенного dict — как обычный JSON-ключ
    assert chr(92) + '"turn' not in text  # не осталось экранированной формы \"turn
    assert _BACKSLASH_N not in text


def test_render_multiline_text_has_real_newlines():
    """Многострочное текстовое значение рендерится блоком с реальными переносами
    (в plain-тексте виджета есть построчный текст файла и НЕТ литеральных \\n)."""
    w = render_payload(FS_READ_MULTILINE)
    text = _plain_text(w)
    assert _BACKSLASH_N not in text  # литеральных \n не осталось
    # строки файла — реальные строки рендера, с блочным маркером
    assert any(line.endswith("import os") for line in text.splitlines())
    assert "│ " in text  # левая полоса блочного рендера


def test_render_multiline_block_lines_prefixed():
    """Каждая строка multiline-блока идёт с полосой │ и отступом уровня ключа."""
    w = render_payload(FS_READ_MULTILINE)
    text = _plain_text(w)
    content_lines = [ln for ln in text.splitlines() if "│ " in ln]
    inner = json.loads(FS_READ_MULTILINE)
    # блок покрывает все строки многострочного content
    assert len(content_lines) >= inner["content"].count("\n")


def test_render_no_false_token_match_without_quote_boundary(monkeypatch):
    """Literal-текст, содержащий подстроку токен-плейсхолдера БЕЗ окружающих
    кавычек, не должен ложно матчиться на своей строке каркаса и путать
    реальный multiline-блок (фикс: матч только целой закавыченной формы)."""
    fixed_uuid = uuid.UUID(int=0)  # .hex[:8] -> "00000000", детерминируем токен
    monkeypatch.setattr(json_payload_module.uuid, "uuid4", lambda: fixed_uuid)

    data = {
        "literal": "prefix @@GC-MLBLOCK-0-00000000@@ suffix",
        "real": "line1\nline2",
    }
    w = render_payload(data)
    text = _plain_text(w)
    assert "prefix @@GC-MLBLOCK-0-00000000@@ suffix" in text
    assert text.count("│ line1") == 1  # блок не задублирован
    assert "│ line2" in text


def test_render_multiline_block_preserves_trailing_comma():
    """Хвост строки каркаса после multiline-значения (запятая для не
    последнего поля) не теряется — иначе JSON-каркас после блока рвётся."""
    data = {"content": "line1\nline2", "status": "ok"}
    w = render_payload(data)
    text = _plain_text(w)
    assert "│ line2" in text
    tail_and_after = text.split("│ line2", 1)[1]
    assert "," in tail_and_after  # запятая после блока сохранена
    assert '"status"' in text
    assert '"ok"' in text


def test_render_multiline_block_normalizes_crlf():
    """CRLF внутри multiline-значения не оставляет висячий \\r в строке блока."""
    data = {"content": "line1\r\nline2\r\n"}
    w = render_payload(data)
    text = _plain_text(w)
    assert "\r" not in text
    assert "│ line1" in text
    assert "│ line2" in text


# --- селектируемость: get_selection обязан отдавать текст (не None) ---


def test_payload_blocks_children_render_to_text():
    """Контракт селектируемости: каждый дочерний Static рендерится в rich.Text
    (Widget.get_selection отдаёт None для не-Text рендера — Syntax/Group)."""
    from rich.text import Text as RichText
    w = render_payload(INTENT_LOG)
    children = list(getattr(w, "_pending_children", []) or w.children)
    assert children, "render_payload(dict) должен вернуть контейнер с блоками"
    for child in children:
        assert isinstance(child.content, RichText), type(child.content)


def test_payload_skeleton_keeps_highlighting():
    """Подсветка не потеряна: у Text JSON-каркаса есть непустые style-spans."""
    w = render_payload(INTENT_LOG)
    children = list(getattr(w, "_pending_children", []) or w.children)
    skeleton = children[0].content
    assert skeleton.spans, "Syntax.highlight должен дать стилевые spans"


def test_multiline_payload_selectable_blocks():
    """Payload с multiline-контентом: и каркас, и текст-блок — Text-рендер."""
    from rich.text import Text as RichText
    w = render_payload(FS_READ_MULTILINE)
    children = list(getattr(w, "_pending_children", []) or w.children)
    assert len(children) >= 2  # каркас-голова, multiline-блок, (хвост)
    assert all(isinstance(c.content, RichText) for c in children)
