"""`--reset-config` + доработка writer (спека `docs/specs/2026-07-23-profiles-design.md`
§11): подмена config.yml пакетным умолчанием, бэкап старой версии, профили и
сессионные оверрайды не трогаются; `is_unconfigured` расширен реестром моделей;
`onboarding_config_writer.ensure_pointer_anchor` — топ-уровневый якорь `profile:`.

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-14-brief.md.
"""
import sys
from pathlib import Path

import pytest

import config_sync
import onboarding
import onboarding_config_writer as ocw


# --- config_sync.reset_config: бэкап + содержимое = шаблон + профили пережили ---

def test_reset_config_replaces_with_template_and_backs_up_old(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text('profile: "work"\nbackend:\n  completions_url: "http://real/v1"\n',
                    encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text('profile: ""\nlevel: default\nbackend:\n  completions_url: "http://sentinel"\n',
                        encoding="utf-8")

    report = config_sync.reset_config(cfg, template, timestamp="20260727-1200")

    assert cfg.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    backup = cfg.parent / "backups" / "config.yml.bak-20260727-1200"
    assert str(backup) == report.backup_path
    assert 'completions_url: "http://real/v1"' in backup.read_text(encoding="utf-8")


def test_reset_config_does_not_touch_profile_files(tmp_path):
    """Профили (`~/.gc/profiles/*`) переживают сброс базового config.yml — reset_config
    трогает ровно один файл (`path`), профили — соседние файлы в другой директории."""
    cfg = tmp_path / "config.yml"
    cfg.write_text('profile: "work"\nbackend:\n  completions_url: "http://real/v1"\n',
                    encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text('profile: ""\n', encoding="utf-8")
    profile_file = tmp_path / "profiles" / "work.yml"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_text("models:\n  flash:\n    url: http://x\n", encoding="utf-8")
    profile_text_before = profile_file.read_text(encoding="utf-8")

    config_sync.reset_config(cfg, template, timestamp="20260727-1201")

    assert profile_file.read_text(encoding="utf-8") == profile_text_before


def test_reset_config_prunes_old_backups(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend: {}\n", encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text("backend: {}\n", encoding="utf-8")
    for i in range(12):
        config_sync.reset_config(cfg, template, timestamp=f"2026072{i:02d}-0000")
        cfg.write_text("backend: {}\n", encoding="utf-8")   # переписан на прежнее значение для след. итерации
    baks = sorted((cfg.parent / "backups").glob("config.yml.bak-*"))
    assert len(baks) == 10          # keep=10, старые вычищены


# --- CLI --reset-config: y/N, отказ без TTY, отказы на bundled/отсутствующий файл ---

sys.argv = ["gc-cli", "--reset-config"]
import cli  # noqa: E402


def _fake_run_args(config=None):
    return type("A", (), {"config": config, "reset_config": True})()


def test_run_reset_config_refuses_without_tty(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend: {}\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(cfg)))
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    called = []
    monkeypatch.setattr(cli.config_sync, "reset_config", lambda *a, **k: called.append(1))
    assert cli._run_reset_config() == 1
    err = capsys.readouterr().err
    assert "TTY" in err
    assert called == []


def test_run_reset_config_y_confirms_and_resets(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend: {}\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(cfg)))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "y")
    called = {}

    def _fake_reset(path, template_path=None):
        called["path"] = str(path)
        return config_sync.ResetReport(str(cfg) + ".bak-x")

    monkeypatch.setattr(cli.config_sync, "reset_config", _fake_reset)
    assert cli._run_reset_config() == 0
    err = capsys.readouterr().err
    assert called["path"] == str(cfg)
    assert "сброшен" in err and ".bak-x" in err


def test_run_reset_config_n_cancels_without_reset(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend: {}\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(cfg)))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    called = []
    monkeypatch.setattr(cli.config_sync, "reset_config", lambda *a, **k: called.append(1))
    assert cli._run_reset_config() == 0
    err = capsys.readouterr().err
    assert "отменено" in err
    assert called == []


def test_run_reset_config_refuses_project_local(monkeypatch, tmp_path, capsys):
    """Зеркало test_run_sync_config_refuses_project_local (tests/test_cli_sync_config.py):
    таргет резолвится в project-local .gc/config.yml (частичный overlay) → отказ,
    не бэкап-и-подмена целиком (см. cli.py:_run_reset_config, гвард перед сбросом)."""
    gc_dir = tmp_path / ".gc"
    gc_dir.mkdir()
    local_cfg = gc_dir / "config.yml"
    local_cfg.write_text("backend:\n  completions_url: \"http://x\"\n", encoding="utf-8")
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(local_cfg)))
    import config as config_mod
    monkeypatch.setattr(config_mod, "_project_local_config_path", lambda: str(local_cfg))
    called = []
    monkeypatch.setattr(cli.config_sync, "reset_config", lambda *a, **k: called.append(1))
    assert cli._run_reset_config() == 1
    err = capsys.readouterr().err
    assert "project-local" in err
    assert "overlay" in err
    assert called == []


def test_run_reset_config_refuses_bundled_template(monkeypatch, capsys):
    from config import _default_config_path
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args())
    monkeypatch.setattr(cli, "user_config_path", lambda: Path(_default_config_path()))
    assert cli._run_reset_config() == 1
    err = capsys.readouterr().err
    assert "нечего сбрасывать" in err


def test_run_reset_config_refuses_missing_file(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "nonexistent.yml"
    monkeypatch.setattr(cli.arg_parser, "run_args", _fake_run_args(config=str(missing)))
    assert cli._run_reset_config() == 1
    err = capsys.readouterr().err
    assert "не найден" in err
    assert "--init-config" in err


# --- onboarding.is_unconfigured: реестр моделей закрывает требование даже с плейсхолдерами ---

def test_is_unconfigured_true_when_everything_placeholder():
    config = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
    }
    assert onboarding.is_unconfigured(config) is True


def test_is_unconfigured_false_when_registry_covers_model_placeholder():
    """Спека §11: заполненный merged-реестр (`models` непуст) закрывает требование
    МОДЕЛИ — плейсхолдер `agent.model` при нём не блокирует."""
    config = {
        "completions_url": "http://real/v1",
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"url": "http://x"}},
    }
    assert onboarding.is_unconfigured(config) is False


def test_is_unconfigured_true_when_registry_but_endpoint_placeholder():
    """…и только его: адрес/токен реестр не закрывает. Записи, которые пишет мастер
    (решение 10 волны B), наследуют url/токен от плоского `backend` — плейсхолдерный
    endpoint при живом реестре означает «нечем работать»."""
    config = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"url": "http://x"}},
    }
    assert onboarding.is_unconfigured(config) is True


def test_is_unconfigured_false_when_legacy_fields_filled():
    config = {
        "completions_url": "http://real/v1",
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": "real-model",
    }
    assert onboarding.is_unconfigured(config) is False


def test_is_unconfigured_true_when_empty_models_dict():
    """`models: {}` эквивалентен отсутствию секции (спека §1) — не закрывает требование."""
    config = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {},
    }
    assert onboarding.is_unconfigured(config) is True


# --- onboarding_config_writer.ensure_pointer_anchor: вставка + идемпотентность ---

_TEMPLATE = (
    "# Профили v2: указатели на активный профиль/уровень.\n"
    'profile: ""\n'
    "level: default\n"
    "backend:\n"
    '  completions_url: "http://sentinel"\n'
)


def test_ensure_pointer_anchor_inserts_when_missing(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text('backend:\n  completions_url: "http://real/v1"\n', encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text(_TEMPLATE, encoding="utf-8")

    ocw.ensure_pointer_anchor(cfg, template)

    out = cfg.read_text(encoding="utf-8")
    assert 'profile: ""' in out
    assert 'completions_url: "http://real/v1"' in out   # существующее содержимое цело


def test_ensure_pointer_anchor_is_idempotent(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text('backend:\n  completions_url: "http://real/v1"\n', encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text(_TEMPLATE, encoding="utf-8")

    ocw.ensure_pointer_anchor(cfg, template)
    once = cfg.read_text(encoding="utf-8")
    ocw.ensure_pointer_anchor(cfg, template)
    twice = cfg.read_text(encoding="utf-8")

    assert once == twice
    assert once.count("profile:") == 1


def test_ensure_pointer_anchor_noop_when_anchor_already_present(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text('profile: "work"\nbackend:\n  completions_url: "http://real/v1"\n',
                    encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text(_TEMPLATE, encoding="utf-8")

    ocw.ensure_pointer_anchor(cfg, template)

    assert cfg.read_text(encoding="utf-8") == (
        'profile: "work"\nbackend:\n  completions_url: "http://real/v1"\n')


def test_ensure_pointer_anchor_then_write_values_sets_value(tmp_path):
    """После вставки якоря обычный `write_values` находит его и правит значение."""
    cfg = tmp_path / "config.yml"
    cfg.write_text('backend:\n  completions_url: "http://real/v1"\n', encoding="utf-8")
    template = tmp_path / "config.default.yml"
    template.write_text(_TEMPLATE, encoding="utf-8")

    ocw.ensure_pointer_anchor(cfg, template)
    missing = ocw.write_values(cfg, {"profile": "work"})

    assert missing == []
    assert 'profile: "work"' in cfg.read_text(encoding="utf-8")


# --- create_pointer_file: единственный случай, когда writer создаёт файл ---

def test_create_pointer_file_writes_single_line(tmp_path):
    target = tmp_path / "project" / ".gc" / "config.yml"
    ocw.create_pointer_file(target, "work")
    assert target.read_text(encoding="utf-8") == 'profile: "work"\n'


def test_create_pointer_file_escapes_quotes(tmp_path):
    target = tmp_path / "config.yml"
    ocw.create_pointer_file(target, 'a"b')
    assert target.read_text(encoding="utf-8") == 'profile: "a\\"b"\n'
