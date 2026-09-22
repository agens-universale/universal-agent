"""InputHistory — per-project история input-реплик (JSONL)."""
import json


def test_load_missing_file_size_zero(tmp_path):
    """Если файла нет — size=0, no errors."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    assert h.size == 0


def test_append_writes_file(tmp_path):
    """append добавляет запись в файл и в memory."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    h.append("hello")
    h.append("world")

    assert h.size == 2

    history_file = tmp_path / "input_history.jsonl"
    assert history_file.exists()
    lines = history_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "hello"
    assert json.loads(lines[1])["text"] == "world"


def test_append_dedup_adjacent(tmp_path):
    """Adjacent duplicates не сохраняются."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    h.append("hello")
    h.append("hello")
    h.append("world")
    h.append("hello")
    h.append("hello")
    assert h.size == 3


def test_load_persists_across_instances(tmp_path):
    """Новый instance видит то что было append'нуто старым."""
    from gc_tui.services.input_history import InputHistory

    h1 = InputHistory(tmp_path)
    h1.load()
    h1.append("first")
    h1.append("second")

    h2 = InputHistory(tmp_path)
    h2.load()
    assert h2.size == 2


def test_load_skips_corrupted_lines(tmp_path):
    """Битая JSON-строка не валит load."""
    from gc_tui.services.input_history import InputHistory

    history_file = tmp_path / "input_history.jsonl"
    history_file.write_text(
        '{"ts": "x", "text": "ok1"}\n'
        'not valid json\n'
        '{"ts": "y", "text": "ok2"}\n',
        encoding="utf-8",
    )
    h = InputHistory(tmp_path)
    h.load()
    assert h.size == 2


def test_append_caps_at_max_entries(tmp_path, monkeypatch):
    """Превышение MAX_ENTRIES → файл переписывается с tail-N."""
    from gc_tui.services.input_history import InputHistory

    monkeypatch.setattr(InputHistory, "MAX_ENTRIES", 5)
    monkeypatch.setattr(InputHistory, "REWRITE_EVERY", 2)

    h = InputHistory(tmp_path)
    h.load()
    for i in range(8):
        h.append(f"msg{i}")

    assert h.size == 5

    h2 = InputHistory(tmp_path)
    h2.load()
    assert h2.size == 5


def test_prev_from_none_returns_last(tmp_path):
    """prev(None) → (size-1, last_text)."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    h.append("a")
    h.append("b")
    h.append("c")

    result = h.prev(None)
    assert result == (2, "c")


def test_prev_walks_backwards(tmp_path):
    """prev(idx) → (idx-1, entries[idx-1])."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    h.append("a")
    h.append("b")
    h.append("c")

    assert h.prev(2) == (1, "b")
    assert h.prev(1) == (0, "a")
    assert h.prev(0) is None


def test_prev_from_virtual_position(tmp_path):
    """prev(size) (virtual end) → (size-1, last)."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    h.append("a")
    h.append("b")

    assert h.prev(2) == (1, "b")


def test_next_walks_forward(tmp_path):
    """next(idx) → (idx+1, entries[idx+1]) или None если за размером."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    h.append("a")
    h.append("b")
    h.append("c")

    assert h.next(0) == (1, "b")
    assert h.next(1) == (2, "c")
    assert h.next(2) is None


def test_prev_on_empty_history_returns_none(tmp_path):
    """prev(None) на пустой истории → None."""
    from gc_tui.services.input_history import InputHistory

    h = InputHistory(tmp_path)
    h.load()
    assert h.prev(None) is None
    assert h.next(None) is None
