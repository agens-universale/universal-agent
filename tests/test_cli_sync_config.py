"""gc-cli --sync-config: цель записи и отчёт."""
import sys

# arg_parser.py парсит sys.argv на import — подменяем argv до импорта cli.
sys.argv = ["gc-cli", "--sync-config"]

import config_sync  # noqa: E402
import cli  # noqa: E402


def _fake_run_args(config=None):
    return type("A", (), {"config": config, "sync_config": True})()


def test_run_sync_config_refuses_bundled_template(monkeypatch, capsys):
    """M9a: bundled-шаблон → отдельное сообщение (не «не найден»)."""
    from config import _default_config_path
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args())
    monkeypatch.setattr(cli, "user_config_path", lambda: __import__("pathlib").Path(_default_config_path()))
    assert cli._run_sync_config() == 1
    err = capsys.readouterr().err
    assert "пакетный шаблон" in err
    assert "не правим" in err


def test_run_sync_config_refuses_missing_file(monkeypatch, tmp_path, capsys):
    """M9a: файл не существует → сообщение об отсутствии (не «пакетный шаблон»)."""
    missing = tmp_path / "nonexistent.yml"
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(missing)))
    assert cli._run_sync_config() == 1
    err = capsys.readouterr().err
    assert "не найден" in err
    assert "--init-config" in err


def test_run_sync_config_refuses_project_local(monkeypatch, tmp_path, capsys):
    """I5: project-local .gc/config.yml → отказ с пояснением."""
    gc_dir = tmp_path / ".gc"
    gc_dir.mkdir()
    local_cfg = gc_dir / "config.yml"
    local_cfg.write_text("backend:\n  completions_url: \"http://x\"\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(local_cfg)))
    # project_local_config_path должна вернуть путь к этому файлу
    from config_modules import env_vars
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: str(local_cfg))
    assert cli._run_sync_config() == 1
    err = capsys.readouterr().err
    assert "project-local" in err
    assert "overlay" in err


def test_run_sync_config_raises_yaml_error_returns_1(monkeypatch, tmp_path, capsys):
    """I3: битый YAML → return 1, stderr «НЕ обновлён», без трейсбека."""
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend:\n  bad: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(cfg)))
    assert cli._run_sync_config() == 1
    err = capsys.readouterr().err
    assert "НЕ обновлён" in err


def test_run_sync_config_happy_path(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend:\n  completions_url: \"http://x\"\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(cfg)))
    called = {}

    def _fake_sync(path, template_path=None):
        called["path"] = str(path)
        return config_sync.SyncReport(3, 1, [], ["old_key"], [], str(cfg) + ".bak-x")

    monkeypatch.setattr(cli.config_sync, "sync_config", _fake_sync)
    assert cli._run_sync_config() == 0
    out = capsys.readouterr().err
    assert called["path"] == str(cfg)
    assert "перенесено: 3" in out and "old_key" in out
