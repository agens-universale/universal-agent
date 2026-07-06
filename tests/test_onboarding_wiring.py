"""Проводка онбординга в контроллеры: --init и стоп-гайд первого запуска."""
import sys

# `import cli` парсит sys.argv на импорте (нужен -q или --init) — пиннем валидный.
sys.argv = ["gc-cli", "-q", "x"]

import cli  # noqa: E402
import config  # noqa: E402
import tui  # noqa: E402
from config_modules import env_vars  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(env_vars, "project_slug", lambda: "p")
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)


def test_cli_init_creates_config(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = cli._run_init()
    assert rc == 0
    assert (tmp_path / "gc" / "config.yml").is_file()
    assert "config.yml" in capsys.readouterr().err


def test_cli_init_does_not_clobber_existing(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    cfgdir = tmp_path / "gc"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.yml").write_text("agent:\n  model: mine\n", encoding="utf-8")
    rc = cli._run_init()
    assert rc == 0
    assert "уже есть" in capsys.readouterr().err
    assert "mine" in (cfgdir / "config.yml").read_text(encoding="utf-8")


def test_tui_init_creates_and_exits(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = tui.main(["--init"])
    assert rc == 0
    assert (tmp_path / "gc" / "config.yml").is_file()
    assert "config.yml" in capsys.readouterr().err


def test_tui_stop_guide_on_unconfigured(tmp_path, monkeypatch, capsys):
    # дефолтный путь (без --config), свежий конфиг с плейсхолдером →
    # стоп-гайд и выход 0, TUI не запускаем; болванка всё равно создаётся.
    _isolate(tmp_path, monkeypatch)
    rc = tui.main([])
    assert rc == 0
    assert "не настроен" in capsys.readouterr().err
    assert (tmp_path / "gc" / "config.yml").is_file()


def test_tui_proceeds_when_configured(tmp_path, monkeypatch):
    # настроенный конфиг → стоп-гайд НЕ срабатывает (идём дальше к TUI).
    _isolate(tmp_path, monkeypatch)
    cfgdir = tmp_path / "gc"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.yml").write_text(
        "backend:\n"
        "  completions_url: https://real/v1/chat/completions\n"
        "  chat_completion_headers:\n"
        "    Authorization: Bearer real-token\n",
        encoding="utf-8",
    )
    cfg = config.load_config(None)
    import onboarding
    assert onboarding.is_unconfigured(cfg) is False
