"""gc-ua хранит только в файлах: ``storage:`` в базе — отказ при старте с
объяснением, TuiApp не создаётся."""

import pytest


def test_main_refuses_database_storage(monkeypatch, tmp_path, capsys):
    pytest.importorskip("textual")
    from gc_tui import app as tui_app

    created: list = []
    monkeypatch.setattr(tui_app, "TuiApp", lambda **kw: created.append(kw))
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "storage:\n  threads:\n    type: sqlite\n    path: t.db\n",
        encoding="utf-8",
    )
    from gc_tui import main

    assert main(argv=["--config", str(cfg_path), "--no-auto-mcp"]) == 1
    assert created == []
    err = capsys.readouterr().err
    assert "gc-ua" in err and "storage.threads" in err
