"""gc-cli --check-update / --update: проверка обновлений и detached-обновление."""

import sys

# arg_parser.py парсит sys.argv на import — подменяем argv до импорта cli.
sys.argv = ["gc-cli", "--check-update"]

from gc_cli import cli
from gc_core.updater.check import CheckResult
from gc_core.updater.source import UpdateSource


def _fake_run_args(config=None):
    return type("A", (), {"config": config})()


def test_check_update_reports_update_available(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.updater_source,
        "resolved_source",
        lambda: UpdateSource(url="https://x/y", ref="master", commit="old"),
    )
    monkeypatch.setattr(
        cli.updater_check,
        "check_for_update",
        lambda src: CheckResult("update", "new", "доступна новая версия"),
    )
    assert cli._run_check_update() == 0
    err = capsys.readouterr().err
    assert "доступна новая версия" in err
    # ASCII-safe маркеры вместо глифов вне cp1251 (✓/✗/⚠ не кодируются в cp1251).
    assert "⚠" not in err and "✓" not in err and "✗" not in err


def test_check_update_reports_current(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.updater_source,
        "resolved_source",
        lambda: UpdateSource(url="https://x/y", ref="master", commit="c"),
    )
    monkeypatch.setattr(
        cli.updater_check,
        "check_for_update",
        lambda src: CheckResult(
            "current", "c", "установлена последняя версия"
        ),
    )
    assert cli._run_check_update() == 0
    err = capsys.readouterr().err
    assert "установлена последняя версия" in err
    assert "✓" not in err


def test_check_update_reports_unknown(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.updater_source,
        "resolved_source",
        lambda: UpdateSource(url="https://x/y", ref="master", commit="c"),
    )
    monkeypatch.setattr(
        cli.updater_check,
        "check_for_update",
        lambda src: CheckResult(
            "unknown", None, "не удалось связаться с источником"
        ),
    )
    assert cli._run_check_update() == 0
    err = capsys.readouterr().err
    assert "не удалось связаться с источником" in err
    assert "⚠" not in err


def test_check_update_dev_source_reports_git(monkeypatch, capsys):
    monkeypatch.setattr(cli.updater_source, "resolved_source", lambda: None)
    assert cli._run_check_update() == 0
    err = capsys.readouterr().err
    assert "git" in err


def test_update_dev_source_does_not_spawn(monkeypatch, capsys):
    monkeypatch.setattr(cli.updater_source, "resolved_source", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        cli.updater_runner,
        "spawn_detached_update",
        lambda *a, **k: spawned.setdefault("called", True),
    )
    assert cli._run_update() == 0
    assert "called" not in spawned
    err = capsys.readouterr().err
    assert "git" in err


def test_update_builds_command_and_spawns(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        cli.arg_parser, "run_args", _fake_run_args(config=str(cfg))
    )
    monkeypatch.setattr(
        cli.updater_source,
        "resolved_source",
        lambda: UpdateSource(url="https://x/y", ref="master", commit="old"),
    )
    monkeypatch.setattr(
        cli.updater_check, "extra_args", lambda config: ["--system-certs"]
    )
    spawned = {}
    monkeypatch.setattr(
        cli.updater_runner,
        "spawn_detached_update",
        lambda command, **kw: spawned.update(command=command, kw=kw),
    )
    assert cli._run_update() == 0
    assert spawned["command"][-1] == "--system-certs"
    # Цель переустановки — ровно один аргумент. С 0.3.0 это не обязательно голый
    # git+URL: если в установке есть extras, аргумент принимает форму PEP 508
    # `gc-ua[…] @ git+URL`, иначе `--update` терял бы профиль пользователя
    # (регрессия закреплена в tests/updater/test_runner.py).
    target = [a for a in spawned["command"] if "git+https://x/y@master" in a]
    assert len(target) == 1, spawned["command"]
    assert spawned["kw"]["parent_pid"] == cli.os.getpid()
    assert spawned["kw"]["restart_cmd"] is None
    err = capsys.readouterr().err
    assert "gc-cli" in err or "gc-ua" in err


def test_update_message_uses_location_phrase(monkeypatch, tmp_path, capsys):
    """I4: сообщение о запуске установки берёт формулировку места из единого владельца
    (updater.runner.install_location_phrase), а не хардкодит «в отдельном окне» —
    на POSIX там просто нет никакого окна (spawn без консоли)."""
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        cli.arg_parser, "run_args", _fake_run_args(config=str(cfg))
    )
    monkeypatch.setattr(
        cli.updater_source,
        "resolved_source",
        lambda: UpdateSource(url="https://x/y", ref="master", commit="old"),
    )
    monkeypatch.setattr(
        cli.updater_runner, "spawn_detached_update", lambda *a, **k: None
    )
    monkeypatch.setattr(
        cli.updater_runner, "install_location_phrase", lambda: "в фоне"
    )
    assert cli._run_update() == 0
    err = capsys.readouterr().err
    assert "в фоне" in err
    assert "в отдельном окне" not in err
