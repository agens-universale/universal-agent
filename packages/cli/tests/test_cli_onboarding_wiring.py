import sys

# `import cli` parses sys.argv at import time; pin a valid argv so this file
# runs deterministically under any pytest invocation.
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import cli
from gc_core.settings import settings_from_flat


def _stub_repair_load(monkeypatch, config):
    """Лестница загрузки «чинилки» подменяется одной ступенью «всё хорошо»:
    предмет этих тестов — разводка мастера, а не загрузка конфига (её ступени
    проверяет tests/test_config_repair_ladder.py на настоящих файлах)."""
    from gc_core import onboarding

    monkeypatch.setattr(
        onboarding,
        "load_config_for_repair",
        lambda: onboarding.RepairLoad(
            config, None, onboarding.REPAIR_STAGE_FULL, None
        ),
    )


def test_run_init_calls_wizard_review_all_when_interactive(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_bootstrap_user_config", lambda: True)
    monkeypatch.setattr(
        cli, "user_config_path", lambda: tmp_path / "config.yml"
    )
    _stub_repair_load(monkeypatch, {"model": "m"})
    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(review_all=review_all) or True
        ),
    )
    assert cli._run_init() == 0
    assert seen["review_all"] is True


def test_run_init_passive_when_not_interactive(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_bootstrap_user_config", lambda: True)
    monkeypatch.setattr(
        cli, "user_config_path", lambda: tmp_path / "config.yml"
    )
    _stub_repair_load(
        monkeypatch,
        {"completions_url": "http://your-backend:PORT/v1/chat/completions"},
    )
    assert cli._run_init() == 0
    assert "config.yml" in capsys.readouterr().err


def test_run_init_no_mcp(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_bootstrap_user_config", lambda: True)
    monkeypatch.setattr(
        cli, "user_config_path", lambda: tmp_path / "config.yml"
    )
    _stub_repair_load(monkeypatch, {"model": "m"})
    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(mcp=mcp, review_all=review_all) or True
        ),
    )
    assert cli._run_init() == 0
    assert seen["mcp"] is None and seen["review_all"] is True


def test_run_init_returns_1_when_wizard_leaves_config_unconfigured(
    monkeypatch, tmp_path
):
    """run() отдаёт False и когда пользователь прошёл мастер, но конфиг остался
    ненастроенным (пропущенные поля, отказ записи, файл так и не перечитался) —
    _run_init() раньше отбрасывал этот результат и всегда рапортовал 0."""
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_bootstrap_user_config", lambda: True)
    monkeypatch.setattr(
        cli, "user_config_path", lambda: tmp_path / "config.yml"
    )
    _stub_repair_load(monkeypatch, {"model": "m"})
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            False
        ),
    )
    assert cli._run_init() == 1


def test_main_no_auto_wizard(monkeypatch, tmp_path, capsys):
    # неполный конфиг + интерактив → визард НЕ вызывается, печатается пассивный хинт
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        type(
            "A",
            (),
            {
                "init_config": False,
                "init": False,
                "config": None,
                "write_guard": None,
                "no_write_guard": False,
                "command_moderation": None,
                "read_guard": None,
                "no_read_guard": False,
                "read_guard_mode": None,
                "agentsmd_override": False,
                "skill": None,
                "profile": None,
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda *a, **k: settings_from_flat(
            {"completions_url": "http://your-backend:PORT/v1/chat/completions"}
        ),
    )
    monkeypatch.setattr(cli, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    monkeypatch.setattr(
        cli, "user_config_path", lambda: tmp_path / "config.yml"
    )
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("run вызван")),
    )
    assert cli.main() == 0
    assert "config.yml" in capsys.readouterr().err


def test_unconfigured_with_structured_format_exits_1(
    monkeypatch, tmp_path, capsys
):
    """Флаг формы на ненастроенном конфиге — отказ, не пустой успех."""
    fmt = tmp_path / "format.md"
    fmt.write_text("верни JSON\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        type(
            "A",
            (),
            {
                "init_config": False,
                "init": False,
                "config": None,
                "write_guard": None,
                "no_write_guard": False,
                "command_moderation": None,
                "read_guard": None,
                "no_read_guard": False,
                "read_guard_mode": None,
                "agentsmd_override": False,
                "skill": None,
                "profile": None,
                "structured_output_format": str(fmt),
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda *a, **k: settings_from_flat(
            {"completions_url": "http://your-backend:PORT/v1/chat/completions"}
        ),
    )
    monkeypatch.setattr(cli, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    monkeypatch.setattr(
        cli, "user_config_path", lambda: tmp_path / "config.yml"
    )
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("run вызван")),
    )
    assert cli.main() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "config.yml" in err
