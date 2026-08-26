"""§3: межслойная половина пары. Каждый слой проверяется ДО слияния — иначе профиль или
проектный конфиг, переопределив один адрес, уносит токен базы на чужой хост, а проверка
итогового документа видит полную пару и молчит."""
from pathlib import Path

import pytest
import yaml

from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Изолировать env_vars singleton + платформу (тот же паттерн, что в
    tests/config/test_profile_loader.py) — иначе managed_config_path() читает
    реальное окружение машины."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch, isolated_env):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    return project_dir


def _write_yaml(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


_BASE_WITH_A_WHOLE_PAIR = {
    "backend": {"api_mode": "chat_completions",
                "completions_url": "http://127.0.0.1:8014/v1/chat/completions"},
    "models": {"gc": {"url": "https://good/v1/chat/completions", "token": "sk-base"}},
}


def test_layer_with_half_a_pair_is_rejected_with_the_file_path():
    from gc_core.config_modules.model_registry import validate_registry_pairs
    raw = {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}}
    with pytest.raises(ValueError, match=r"/proj/\.gc/config\.yml.*models\.gc"):
        validate_registry_pairs(raw, source="/proj/.gc/config.yml")


def test_layer_with_a_whole_pair_passes():
    from gc_core.config_modules.model_registry import validate_registry_pairs
    validate_registry_pairs(
        {"models": {"gc": {"url": "https://h/v1", "token": "sk"}}}, source="/p/config.yml")


def test_explicit_null_is_not_a_key():
    """`url: null` в сыром документе — это НЕ «адрес задан»: слой снимает адрес, и токен
    без него становится половиной (§3, третий случай дизъюнкции)."""
    from gc_core.config_modules.model_registry import validate_registry_pairs
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        validate_registry_pairs({"models": {"gc": {"url": None, "token": "sk"}}})


def test_document_without_registry_passes():
    from gc_core.config_modules.model_registry import validate_registry_pairs
    validate_registry_pairs({"backend": {"completions_url": "https://h/v1"}})


def test_cli_models_override_goes_through_the_rule():
    """`--models` задаёт весь реестр строкой и идёт мимо схемы (плоский config_dict
    заполняется после валидации) — правило прогоняется явно, иначе CLI обходит его."""
    from gc_core.config import apply_cli_overrides
    cfg = {}
    with pytest.raises(ValueError, match="--models.*задан свой адрес"):
        apply_cli_overrides(cfg, extra_overrides={"models": {"gc": {"url": "https://evil/v1"}}})


def test_project_layer_cannot_take_the_base_token_to_its_own_url(isolated_cwd, tmp_path):
    """§9.7, главный сценарий правила: слои сливаются `deep_merge` рекурсивно по ключам,
    поэтому проектный `.gc/config.yml` с ОДНИМ url поверх полной пары базы даёт итоговую
    запись с полной парой — токен базы уходит на адрес проекта, и проверка итогового
    документа молчит. Ловит это только точка B, и она обязана быть подключена в загрузчике
    (файл приезжает с клоном репозитория — сам пользователь его не писал)."""
    from gc_core.config import load_config_ex

    base = tmp_path / "base_config.yml"
    _write_yaml(base, _BASE_WITH_A_WHOLE_PAIR)
    _write_yaml(isolated_cwd / ".gc" / "config.yml",
                {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}})

    with pytest.raises(ValueError,
                       match=r"\.gc[\\/]config\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))


def test_profile_layer_cannot_take_the_base_token_to_its_own_url(isolated_cwd, tmp_path):
    """§9.8, тот же сценарий носителем-профилем: контент профиля мержится ПОВЕРХ всех
    файловых слоёв, значит и он способен подменить один адрес под чужим токеном."""
    from gc_core.config import load_config_ex

    base = tmp_path / "base_config.yml"
    _write_yaml(base, dict(_BASE_WITH_A_WHOLE_PAIR, profile="work"))
    _write_yaml(isolated_cwd / ".gc" / "profiles" / "work.yml",
                {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}})

    with pytest.raises(ValueError, match=r"work\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))


def test_managed_layer_cannot_take_the_base_token_to_its_own_url(isolated_cwd, tmp_path):
    """§9.8, третий слой: machine-managed оверлей (`config.local.yml`, его пишет
    `gc-mcp install`) идёт тем же `deep_merge` и правилу не подсуден иначе, чем своей
    проверкой до слияния."""
    from gc_core.config import load_config_ex
    from gc_core.mcp_runtime.paths import managed_config_path

    base = tmp_path / "base_config.yml"
    _write_yaml(base, _BASE_WITH_A_WHOLE_PAIR)
    _write_yaml(managed_config_path(),
                {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}})

    with pytest.raises(ValueError, match=r"config\.local\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))
