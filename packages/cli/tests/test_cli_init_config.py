"""Проводка онбординга в CLI: `gc-cli --init` создаёт конфиг и не затирает чужой.

Жил в корневом tests/test_onboarding_wiring.py вместе с проверками TUI. Файл
выглядел кросс-пакетным по импортам, но взаимодействия cli ↔ tui в нём не было:
каждый фронтенд общается с ядром независимо. Разнесён по пакетам 18.08.2026.

Дополняет test_cli_onboarding_wiring.py — тот проверяет ВЫЗОВ визарда, этот
создание файла.
"""
import sys

# `import cli` парсит sys.argv на импорте (нужен -q или --init) — пиннем валидный.
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import cli
from gc_core.config_modules import env_vars


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(env_vars, "project_slug", lambda: "p")
    # Проектный слой заглушает autouse-фикстура `_isolated_config_layers`
    # (gc_testkit.plugin): она отдаёт слой, только если тест сам сдвинул cwd.
    # Алиас `config._project_local_config_path` develop снёс — подмена по нему
    # и до того НИЧЕГО не делала, а теперь ещё и падает AttributeError.


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
    # Новое поведение: не-TTY → setup_guide (путь к конфигу выводится в stderr)
    assert "config.yml" in capsys.readouterr().err
    assert "mine" in (cfgdir / "config.yml").read_text(encoding="utf-8")


