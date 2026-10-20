"""gc-cli на хранилище из ``storage:`` config.yml.

Нефайловая часть открывается через gc-stores до хода и закрывается после;
пакета нет или подключение негодно — код 1 и подсказка, а не трейсбек.
"""

import sys

# `import cli` парсит sys.argv на импорте (-q обязателен) — фиксируем валидный argv.
sys.argv = ["gc-cli", "-q", "x"]

import sqlite3

import pytest
import yaml
from gc_cli import cli
from gc_testkit import scripted_model


def _config(tmp_path, storage: dict) -> str:
    path = tmp_path / "config.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "backend": {
                    "api_mode": "chat_completions",
                    "completions_url": (
                        "http://scripted.invalid/v1/chat/completions"
                    ),
                    "chat_completion_kwargs": {"stream": False},
                },
                "agent": {
                    "model": "scripted",
                    "max_steps": 3,
                    "use_local_subagents": False,
                    "strict_turn_finish_validation": False,
                },
                "logging": {"save_logs": False, "console_trace": False},
                "paths": {"threads_data_path": str(tmp_path / "threads")},
                "storage": storage,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def run_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    from gc_core import upgrade_gate

    monkeypatch.setattr(upgrade_gate, "detect_upgrade", lambda path=None: None)
    monkeypatch.setattr(
        upgrade_gate, "mark_current_run", lambda path=None: None
    )

    async def _run(config: str, *argv: str) -> int:
        monkeypatch.setattr(
            cli.arg_parser,
            "run_args",
            cli.arg_parser.parser.parse_args(
                ["-q", "сделай", "--config", config, *argv]
            ),
        )
        return await cli.arun_cli()

    return _run


async def test_turn_is_written_to_the_configured_database(
    run_cli, monkeypatch, tmp_path
):
    scripted_model.install(monkeypatch)
    db = tmp_path / "t.db"
    config = _config(
        tmp_path, {"threads": {"type": "sqlite", "path": str(db)}}
    )
    assert await run_cli(config, "-t", "t-cli") == 0
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT system, thread_id FROM gc_threads"
        ).fetchall()
    assert rows == [("cli", "t-cli")]
    threads_dir = tmp_path / "threads"
    assert not threads_dir.exists() or not any(
        p.name.startswith("data_") for p in threads_dir.rglob("*")
    )


async def test_without_gc_stores_the_hint_names_the_package(
    run_cli, monkeypatch, tmp_path, capsys
):
    monkeypatch.setitem(sys.modules, "gc_stores.configured", None)
    config = _config(
        tmp_path,
        {"threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}},
    )
    assert await run_cli(config) == 1
    err = capsys.readouterr().err
    assert "gc-stores" in err and "storage.threads" in err


async def test_bad_storage_is_refused_with_the_reason(
    run_cli, tmp_path, capsys
):
    config = _config(tmp_path, {"threads": {"type": "sqlite"}})
    assert await run_cli(config) == 1
    err = capsys.readouterr().err
    assert "storage.threads" in err and "path" in err
