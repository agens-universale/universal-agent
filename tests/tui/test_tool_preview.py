"""extract_preview_lines: 3 строки контента + remaining count из tool-output payload."""
import json

from tui.services.tool_preview import extract_preview_lines


def test_json_dict_with_content_multiline():
    out = json.dumps({"ok": True, "content": "line1\nline2\nline3\nline4\nline5"})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["line1", "line2", "line3"]
    assert remaining == 2


def test_json_dict_with_content_short():
    out = json.dumps({"ok": True, "content": "only one"})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["only one"]
    assert remaining == 0


def test_json_dict_with_entries_list():
    out = json.dumps({"ok": True, "entries": ["a", "b", "c", "d", "e"]})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["a", "b", "c"]
    assert remaining == 2


def test_json_dict_with_matches():
    out = json.dumps({"ok": True, "matches": [
        {"file": "a.py", "line": 10},
        {"file": "b.py", "line": 20},
    ]})
    lines, remaining = extract_preview_lines(out)
    # match-описания форматируются как "file:line"
    assert lines == ["a.py:10", "b.py:20"]
    assert remaining == 0


def test_json_dict_with_stdout_string():
    out = json.dumps({"ok": True, "stdout": "exec line1\nexec line2"})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["exec line1", "exec line2"]
    assert remaining == 0


def test_json_dict_with_stdout_list():
    out = json.dumps({"ok": True, "stdout": ["one", "two", "three", "four"]})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["one", "two", "three"]
    assert remaining == 1


def test_plain_string_multiline():
    out = "raw\nplain\ntext\nfourth"
    lines, remaining = extract_preview_lines(out)
    assert lines == ["raw", "plain", "text"]
    assert remaining == 1


def test_plain_string_short():
    out = "one short line"
    lines, remaining = extract_preview_lines(out)
    assert lines == ["one short line"]
    assert remaining == 0


def test_write_style_ok_no_content():
    """Write-результаты ({ok: true} без content/entries/...) → preview не нужно."""
    out = json.dumps({"ok": True})
    lines, remaining = extract_preview_lines(out)
    assert lines == []
    assert remaining == 0


def test_invalid_json_falls_back_to_plain():
    """Невалидный JSON → trackируется как plain string."""
    out = "{not really json: at all"
    lines, remaining = extract_preview_lines(out)
    assert lines == ["{not really json: at all"]
    assert remaining == 0


def test_empty_string():
    lines, remaining = extract_preview_lines("")
    assert lines == []
    assert remaining == 0


def test_json_list_wrapping_single_dict_unwraps():
    """Некоторые тулы возвращают [{ok, content, ...}] — список-обёртка вокруг dict'а.
    Должны разворачивать к первому dict'у и извлекать content нормально."""
    out = json.dumps([{"ok": True, "content": "line1\nline2\nline3\nline4"}])
    lines, remaining = extract_preview_lines(out)
    assert lines == ["line1", "line2", "line3"]
    assert remaining == 1


def test_json_list_multiple_dicts_uses_first():
    """Если список содержит несколько dict'ов — берём первый (стартовая точка)."""
    out = json.dumps([
        {"ok": True, "content": "first\nblock"},
        {"ok": True, "content": "second-ignored"},
    ])
    lines, remaining = extract_preview_lines(out)
    assert lines == ["first", "block"]
    assert remaining == 0


def test_json_dict_with_message_fallback():
    """Tool-ошибка {type, message} (execute_command non_zero_exit) → показываем message."""
    out = json.dumps({"type": "non_zero_exit", "message": "Command exited with status 1"})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["Command exited with status 1"]
    assert remaining == 0


def test_json_dict_with_error_fallback():
    """Ошибка с {ok: false, error: '...'} → показываем error."""
    out = json.dumps({"ok": False, "error": "File not found: /tmp/x.py"})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["File not found: /tmp/x.py"]
    assert remaining == 0


def test_double_encoded_json_string_unwraps():
    """Pipeline иногда делает json.dumps дважды → output это JSON-строка,
    после первого loads даёт строку, не dict. Пробуем декодировать ещё раз."""
    # Первый dumps → строка `'[{"tool":...,"content":"line1\\nline2"}]'`
    inner = json.dumps([{"tool": "read_file", "ok": True, "content": "line1\nline2\nline3"}])
    # Второй dumps → `'"[{...}]"'` (JSON-строка, содержащая JSON-список)
    outer = json.dumps(inner)
    lines, remaining = extract_preview_lines(outer)
    assert lines == ["line1", "line2", "line3"]
    assert remaining == 0


def test_stored_tool_output_envelope_unwraps_to_inner_content():
    """gc-cli оборачивает большие outputs в {type:'stored_tool_output', content:'<JSON-string of inner result>'}.
    Должны развернуть envelope и взять content из inner-результата."""
    inner = {
        "tool": "read_file", "ok": True,
        "content": "real-line1\nreal-line2\nreal-line3\nreal-line4",
    }
    envelope = {
        "type": "stored_tool_output", "ok": True, "stored": True,
        "output_ref": "abc-123",
        "source_tool": "read_file",
        "content": json.dumps(inner, ensure_ascii=False),
        "total_chars": 100,
    }
    out = json.dumps(envelope)
    lines, remaining = extract_preview_lines(out)
    assert lines == ["real-line1", "real-line2", "real-line3"]
    assert remaining == 1


def test_stored_tool_output_envelope_truncated_inner_extracts_via_regex():
    """Реальный случай gc-cli: envelope.content — обрезанная JSON-строка inner-результата
    (limit=20000 chars), json.loads падает. Должны извлечь "content" поле regex'ом
    и unescape'нуть \\n → реальные переносы строк."""
    # Симулируем: inner_str — обрезанный JSON с реальным `\\n` (literal) внутри content.
    truncated_inner = (
        '{"tool": "read_file", "ok": true, "path": "/tmp/x.md", '
        '"content": "> привет\\n> Привет! Я ассистент\\n## Какие у тебя умения?\\n'
        '*Я специализируюсь на... [и тут обрезано без закрывающей "]'
    )
    envelope = {
        "type": "stored_tool_output", "source_tool": "read_file",
        "content": truncated_inner,
        "total_chars": 21000, "truncated": True,
    }
    out = json.dumps(envelope)
    lines, remaining = extract_preview_lines(out)
    assert lines[0] == "> привет"
    assert lines[1] == "> Привет! Я ассистент"
    assert lines[2] == "## Какие у тебя умения?"


def test_stored_tool_output_envelope_truncated_matches_via_partial_parser():
    """search_files в envelope: inner truncated посреди очередного match-объекта.
    Generic partial-parser должен закрыть скобки и вернуть частичный dict с matches."""
    truncated_inner = (
        '{"tool": "search_files", "ok": true, "matches": ['
        '{"file": "D:\\\\src\\\\a.py", "line": 23, "line_content": "...", "context": "..."},'
        '{"file": "D:\\\\src\\\\b.py", "line": 100, "line_content": "...", "context": "..."},'
        '{"file": "D:\\\\src\\\\c.py", "line": 5, "line_content": "...", "context": "..."},'
        '{"file": "D:\\\\src\\\\d.py", "line": 7, "line_content": "...", "context": ""'
        # обрезано посреди следующего match'а без закрывающих "]}"
    )
    envelope = {
        "type": "stored_tool_output", "source_tool": "search_files",
        "content": truncated_inner,
        "truncated": True,
    }
    out = json.dumps(envelope)
    lines, remaining = extract_preview_lines(out)
    # partial-parser закрывает скобки → parsed dict содержит первые 3 ПОЛНЫХ match'а
    # (4-й обрезан и отсекается). Existing _from_value делает file:line.
    assert lines == [
        "D:\\src\\a.py:23",
        "D:\\src\\b.py:100",
        "D:\\src\\c.py:5",
    ]
    assert remaining == 0  # всего 3 завершённых match'а, после parser — больше ничего


def test_stored_tool_output_envelope_truncated_entries_via_partial_parser():
    """list_files в envelope: inner truncated посреди очередного entry.
    Тот же generic partial-parser — закрывает скобки, entries обрабатываются
    через _from_value (формирует JSON-строки для dict'ов без file/line)."""
    truncated_inner = (
        '{"tool": "list_files", "ok": true, "entries": ['
        '{"path": "D:\\\\projects\\\\a", "type": "directory", "is_hidden": false},'
        '{"path": "D:\\\\projects\\\\b", "type": "directory", "is_hidden": false},'
        '{"path": "D:\\\\projects\\\\c", "type": "file", "is_hidden": false},'
        '{"path": "D:\\\\projects\\\\d", "type": "directory", "is_h'
        # обрезано посреди очередного entry без закрывающих "}]}"
    )
    envelope = {
        "type": "stored_tool_output", "source_tool": "list_files",
        "content": truncated_inner,
        "truncated": True,
    }
    out = json.dumps(envelope)
    lines, remaining = extract_preview_lines(out)
    # 3 полных entry; 4-й обрезан → отсекается partial-parser'ом.
    # _from_value для list[dict] без file/line делает json.dumps(item).
    assert len(lines) == 3
    for ln in lines:
        # Каждая строка — JSON-репр entry с path/type/is_hidden.
        assert '"path"' in ln
        assert '"type"' in ln
    assert remaining == 0


def test_stored_tool_output_envelope_inner_non_json_falls_back_to_envelope_content():
    """Если content envelope'а — не JSON (а plain text) — оставляем как есть и
    показываем как обычные строки."""
    envelope = {
        "type": "stored_tool_output", "ok": True,
        "content": "plain line1\nplain line2\nplain line3",
    }
    out = json.dumps(envelope)
    lines, remaining = extract_preview_lines(out)
    assert lines == ["plain line1", "plain line2", "plain line3"]
    assert remaining == 0


def test_content_key_wins_over_message():
    """Когда есть и content, и message — content приоритетнее (он основной)."""
    out = json.dumps({"ok": True, "content": "real content\nhere", "message": "note"})
    lines, remaining = extract_preview_lines(out)
    assert lines == ["real content", "here"]
    assert remaining == 0
