"""CLI `--profile`/`--level` + легаси-форс `--model` (Task 10, план «Профили v2, волна A»).

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-10-brief.md,
docs/specs/2026-07-23-profiles-design.md §2, §12.1.
"""

from pathlib import Path

import pytest
import yaml
from gc_core.config import (
    apply_cli_overrides,
    cli_forces_legacy_model,
    load_settings,
)
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.model_router import ModelRouter
from gc_testkit.settings import make_settings


def _write_yaml(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


_MINIMAL_BASE = {
    "backend": {
        "api_mode": "chat_completions",
        "completions_url": "http://127.0.0.1:8014/v1/chat/completions",
    },
}


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Изолировать env_vars singleton + платформу (см. tests/config/test_profile_loader.py)."""
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path / "gc_home")
    monkeypatch.setattr(env_vars, "env", fake)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    return project_dir


# --- arg_parser: именованные флаги --profile/--level ------------------------------


def _parse(argv, monkeypatch):
    """arg_parser парсит sys.argv на импорте — подменяем argv и перезагружаем модуль."""
    monkeypatch.setattr("sys.argv", argv)
    import importlib

    from gc_cli import arg_parser as ap_mod

    importlib.reload(ap_mod)
    return ap_mod


def test_profile_flag_parsed(monkeypatch):
    ap = _parse(["gc-cli", "-q", "hi", "--profile", "work"], monkeypatch)
    assert ap.run_args.profile == "work"


def test_profile_flag_default_is_none(monkeypatch):
    ap = _parse(["gc-cli", "-q", "hi"], monkeypatch)
    assert ap.run_args.profile is None


def test_level_flag_parsed_and_merged_into_overrides(monkeypatch):
    ap = _parse(["gc-cli", "-q", "hi", "--level", "light"], monkeypatch)
    assert ap.run_args.level == "light"
    assert ap.extra_config_overrides.get("level") == "light"


def test_level_flag_parsed_and_merged_into_overrides_auto(monkeypatch):
    ap = _parse(["gc-cli", "-q", "hi", "--level", "auto"], monkeypatch)
    assert ap.run_args.level == "auto"
    assert ap.extra_config_overrides.get("level") == "auto"


def test_level_flag_absent_does_not_add_override(monkeypatch):
    ap = _parse(["gc-cli", "-q", "hi"], monkeypatch)
    assert ap.run_args.level is None
    assert "level" not in ap.extra_config_overrides


def test_model_and_completions_url_flags_land_on_their_paths(monkeypatch):
    # Объявленные флаги — не плоские имена: путь задан рядом с флагом, и
    # легаси-форс узнаёт их по слою `cli` этих путей.
    ap = _parse(
        [
            "gc-cli",
            "-q",
            "hi",
            "--model",
            "X",
            "--completions_url",
            "http://x/v1",
        ],
        monkeypatch,
    )
    assert ap.extra_config_overrides == {
        "agent.model": "X",
        "backend.completions_url": "http://x/v1",
    }
    loaded = apply_cli_overrides(
        make_settings({"models": {"flash": {}}}),
        extra_overrides=ap.extra_config_overrides,
    )
    assert cli_forces_legacy_model(loaded) is True


def test_dotted_param_and_flat_name_hint(monkeypatch):
    ap = _parse(
        ["gc-cli", "-q", "hi", "--agent.max_steps", "50", "--max_steps", "7"],
        monkeypatch,
    )
    assert ap.extra_config_overrides == {
        "agent.max_steps": 50,
        "max_steps": 7,
    }
    from gc_core.config_modules.path_hints import UnknownSettingError

    with pytest.raises(UnknownSettingError) as exc:
        apply_cli_overrides(
            make_settings(), extra_overrides=ap.extra_config_overrides
        )
    assert "--max_steps" in str(exc.value)
    assert "--agent.max_steps" in str(exc.value)
    assert "--agent.max_steps" in str(exc.value)


def test_level_flag_rejects_invalid_choice(monkeypatch):
    monkeypatch.setattr("sys.argv", ["gc-cli", "-q", "hi", "--level", "bogus"])
    import importlib

    with pytest.raises(SystemExit):
        importlib.reload(importlib.import_module("gc_cli.arg_parser"))


# --- config.py: происхождение CLI-правок + детект легаси-форса --------------------


def test_apply_cli_overrides_tracks_explicit_keys():
    loaded = make_settings({"models": {"flash": {}}})
    overridden = apply_cli_overrides(
        loaded, extra_overrides={"agent.model": "X"}
    )
    assert overridden.provenance.source("agent.model").layer == "cli"
    # Не заданное CLI — не помечено как cli; исходный объект не изменён.
    assert overridden.provenance.source("level").layer != "cli"
    assert loaded.provenance.source("agent.model").layer != "cli"


def test_apply_cli_overrides_accumulates_explicit_keys_across_calls():
    loaded = make_settings({"models": {"flash": {}}})
    loaded = apply_cli_overrides(loaded, extra_overrides={"agent.model": "X"})
    loaded = apply_cli_overrides(loaded, extra_overrides={"level": "light"})
    assert loaded.provenance.source("agent.model").layer == "cli"
    assert loaded.provenance.source("level").layer == "cli"


def test_force_legacy_true_when_model_override_and_registry_nonempty():
    loaded = make_settings({"models": {"flash": {}}})
    loaded = apply_cli_overrides(loaded, extra_overrides={"agent.model": "X"})
    assert cli_forces_legacy_model(loaded) is True


def test_force_legacy_true_when_completions_url_override_and_registry_nonempty():
    loaded = make_settings({"models": {"flash": {}}})
    loaded = apply_cli_overrides(
        loaded, extra_overrides={"backend.completions_url": "http://x/v1"}
    )
    assert cli_forces_legacy_model(loaded) is True


def test_force_legacy_false_when_registry_empty():
    loaded = make_settings({"models": {}})
    loaded = apply_cli_overrides(loaded, extra_overrides={"agent.model": "X"})
    assert cli_forces_legacy_model(loaded) is False


def test_force_legacy_false_without_model_or_url_override():
    loaded = make_settings({"models": {"flash": {}}})
    loaded = apply_cli_overrides(
        loaded, extra_overrides={"agent.max_steps": 5}
    )
    assert cli_forces_legacy_model(loaded) is False


def test_force_legacy_false_when_no_cli_overrides_at_all():
    loaded = make_settings({"models": {"flash": {}}})
    assert cli_forces_legacy_model(loaded) is False


# --- Интеграция: --profile активирует поверх конфиг-указателя ---------------------


def test_profile_override_wins_over_config_pointer(isolated_cwd, tmp_path):
    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)
    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "other"})
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "other.yml",
        {"agent": {"model": "other-model"}},
    )
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml",
        {"agent": {"model": "work-model"}},
    )

    loaded = load_settings(str(base_config), profile_override="work")

    assert loaded.profile_meta.active_name == "work"
    assert loaded.settings.agent.model == "work-model"


def test_flat_profile_key_names_active_profile_not_config_pointer(
    isolated_cwd, tmp_path
):
    """Плоский `profile` — ДЕЙСТВУЮЩИЙ профиль, а не указатель из файлов.

    Документу профиля запрещено нести ключ `profile:`, поэтому после мержа он
    остаётся пойнтером config.yml — витрины, читающие плоский ключ (например
    `onboarding.overriding_layer_note`), называли бы чужое имя."""
    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)
    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "home"})
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "home.yml",
        {"agent": {"model": "home-model"}},
    )
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml",
        {"agent": {"model": "work-model"}},
    )

    loaded = load_settings(str(base_config), profile_override="work")
    assert (
        loaded.runtime.active_profile
        == loaded.profile_meta.active_name
        == "work"
    )

    # Сессионное снятие профиля (§9.1) — тоже действующее состояние, не пойнтер.
    cleared = load_settings(str(base_config), profile_override="")
    assert (
        cleared.runtime.active_profile == ""
        and cleared.profile_meta.active_name is None
    )

    # Без override плоский ключ по-прежнему равен указателю конфига.
    plain = load_settings(str(base_config))
    assert (
        plain.runtime.active_profile
        == plain.profile_meta.active_name
        == "home"
    )


# --- Интеграция: --level перекрывает конфиг и профиль ------------------------------


def test_level_override_wins_over_config_and_profile(isolated_cwd, tmp_path):
    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)
    _write_yaml(
        isolated_cwd / ".gc" / "config.yml",
        {"profile": "work", "level": "max"},
    )
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml", {"level": "light"}
    )

    loaded = load_settings(str(base_config))
    assert (
        loaded.settings.level == "light"
    )  # профиль побеждает конфиг-указатель (ось 1)

    loaded = apply_cli_overrides(loaded, extra_overrides={"level": "default"})
    assert loaded.settings.level == "default"  # CLI побеждает профиль


# --- Интеграция: --model при непустом реестре -> force_legacy=True, security профиля жива --


def test_model_override_with_registry_forces_legacy_all_consumers_keeps_profile_security(
    isolated_cwd, tmp_path
):
    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)
    _write_yaml(
        isolated_cwd / ".gc" / "config.yml",
        {
            "profile": "work",
            "models": {"flash": {}},
            "slots": {"default": "flash"},
            "assign": {
                "finish_gate": "flash",
                "subagents": {"subagent_planner": "flash"},
            },
        },
    )
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml",
        {"security": {"write_guard": {"enabled": True}}},
    )

    loaded = apply_cli_overrides(
        load_settings(str(base_config)),
        extra_overrides={"agent.model": "forced-model"},
    )

    assert cli_forces_legacy_model(loaded) is True
    # Профиль-слой обычных секций (гварды) остаётся в стопке — низкоуровневый
    # флаг модели не имеет права молча снимать security-настройки профиля.
    assert loaded.settings.security.write_guard.enabled is True

    router = ModelRouter(
        loaded, level_provider=lambda: "default", force_legacy=True
    )

    main_resolved = router.resolve("main")
    assert main_resolved.alias is None
    assert main_resolved.model == "forced-model"
    assert main_resolved.source == "легаси-форс --model"

    finish_gate_resolved = router.resolve("finish_gate")
    assert finish_gate_resolved.alias is None
    assert finish_gate_resolved.model == "forced-model"

    subagent_resolved = router.resolve("subagent:subagent_planner")
    assert subagent_resolved.alias is None
    assert subagent_resolved.model == "forced-model"


def test_model_override_without_registry_is_plain_legacy_not_forced(
    isolated_cwd, tmp_path
):
    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    loaded = apply_cli_overrides(
        load_settings(str(base_config)),
        extra_overrides={"agent.model": "forced-model"},
    )

    assert cli_forces_legacy_model(loaded) is False

    router = ModelRouter(
        loaded,
        level_provider=lambda: "default",
        force_legacy=cli_forces_legacy_model(loaded),
    )
    resolved = router.resolve("main")
    assert resolved.model == "forced-model"
    assert resolved.source == "легаси"


# --- --models: CLI-реестр доезжает до Router В ТОЙ ЖЕ форме, что и файловый ---------


@pytest.mark.parametrize("token", ["sk-own", "Bearer sk-own"])
def test_cli_models_override_carries_its_own_token_to_the_router(token):
    """`--models` кладётся в плоский `config_dict` МИМО схемы, а Router читает
    СЕРИАЛИЗОВАННУЮ запись (`authorization_header`, `config._serialize_model_entry`) —
    сырой `token` из CLI до него не доезжал вовсе, и запись со своим адресом уходила на
    него с ОБЩИМ ключом. Оверрайд обязан приводиться к той же форме, что кладёт
    `flatten_app_config`, иначе правило пары на `--models` декоративно: пара в строке
    есть, а до сети доезжает половина."""
    loaded = make_settings(
        {
            "backend.completions_url": "https://common/v1/chat/completions",
            "backend.chat_completion_headers": {
                "Authorization": "Bearer sk-common"
            },
        }
    )
    cfg = apply_cli_overrides(
        loaded,
        extra_overrides={
            "models": {
                "gc": {
                    "url": "https://own/v1/chat/completions",
                    "token": token,
                }
            },
        },
    )

    resolved = ModelRouter(cfg, level_provider=lambda: "default").resolve(
        "main"
    )

    assert resolved.url == "https://own/v1/chat/completions"
    assert resolved.authorization == token
