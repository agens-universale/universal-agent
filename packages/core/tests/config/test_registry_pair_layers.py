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
    "backend": {"api_mode": "chat_completions", "completions_url": "http://127.0.0.1:8014/v1/chat/completions"},
    "models": {"gc": {"url": "https://good/v1/chat/completions", "token": "sk-base"}},
}


def test_layer_with_half_a_pair_is_rejected_with_the_file_path():
    from gc_core.config_modules.model_registry import validate_registry_pairs

    raw = {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}}
    with pytest.raises(ValueError, match=r"/proj/\.gc/config\.yml.*models\.gc"):
        validate_registry_pairs(raw, source="/proj/.gc/config.yml")


def test_layer_with_a_whole_pair_passes():
    from gc_core.config_modules.model_registry import validate_registry_pairs

    validate_registry_pairs({"models": {"gc": {"url": "https://h/v1", "token": "sk"}}}, source="/p/config.yml")


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
    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}})

    with pytest.raises(ValueError, match=r"\.gc[\\/]config\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))


def test_profile_layer_cannot_take_the_base_token_to_its_own_url(isolated_cwd, tmp_path):
    """§9.8, тот же сценарий носителем-профилем: контент профиля мержится ПОВЕРХ всех
    файловых слоёв, значит и он способен подменить один адрес под чужим токеном."""
    from gc_core.config import load_config_ex

    base = tmp_path / "base_config.yml"
    _write_yaml(base, dict(_BASE_WITH_A_WHOLE_PAIR, profile="work"))
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml", {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}}
    )

    with pytest.raises(ValueError, match=r"work\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))


def test_managed_layer_cannot_take_the_base_token_to_its_own_url(isolated_cwd, tmp_path):
    """§9.8, третий слой: machine-managed оверлей (`config.local.yml`, его пишет
    `gc-mcp install`) идёт тем же `deep_merge` и правилу не подсуден иначе, чем своей
    проверкой до слияния."""
    from gc_core.config import load_config_ex
    from gc_core.config_modules.env_vars import managed_config_path

    base = tmp_path / "base_config.yml"
    _write_yaml(base, _BASE_WITH_A_WHOLE_PAIR)
    _write_yaml(managed_config_path(), {"models": {"gc": {"url": "https://evil/v1/chat/completions"}}})

    with pytest.raises(ValueError, match=r"config\.local\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))


def test_pre_write_validator_accepts_what_the_loader_accepts(tmp_path):
    """Settings пишет профиль через pre-write валидатор (`validate_profile_text` — только
    точка A), а следующий старт читает его загрузчиком (там ещё и точка B). Разъехавшиеся
    определения «адрес задан» дали бы записанный файл, который не грузится: `url: ""` для
    рантайма — «наследуется вся пара», и обе стороны обязаны сказать это одинаково."""
    from gc_core.config_modules.profiles import load_profile_document, validate_profile_text

    body = 'models:\n  gc:\n    url: ""\n'
    path = tmp_path / "work.yml"
    path.write_text(body, encoding="utf-8")

    validate_profile_text(body)  # pre-write: принимает
    assert load_profile_document(path)["models"]["gc"] == {"url": ""}


def test_pre_write_validator_rejects_what_the_loader_rejects(tmp_path):
    """Симметрия: `url: ""` со своим токеном — половина пары, и отказывают ОБЕ стороны
    одним текстом. Иначе Settings записывает файл, который следующий старт отвергает."""
    from gc_core.config_modules.profiles import load_profile_document, validate_profile_text

    body = 'models:\n  gc:\n    url: ""\n    token: sk-own\n'
    path = tmp_path / "work.yml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        validate_profile_text(body)
    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        load_profile_document(path)


def test_base_config_half_a_pair_names_its_own_file(isolated_cwd, tmp_path):
    """Проводка точки B на базовом слое. Половина пары в самом `config.yml` падает и без
    неё — точкой A на итоговом документе, — но БЕЗ пути файла. Слоёв у пользователя три
    (базовый, managed, проектный) плюс профиль, и «в каком файле» здесь половина ответа."""
    from gc_core.config import load_config_ex

    base = tmp_path / "base_config.yml"
    _write_yaml(
        base,
        {
            "backend": {"api_mode": "chat_completions", "completions_url": "http://127.0.0.1:8014/v1/chat/completions"},
            "models": {"gc": {"url": "https://evil/v1/chat/completions"}},
        },
    )

    with pytest.raises(ValueError, match=r"base_config\.yml: models\.gc: задан свой адрес"):
        load_config_ex(str(base))


def test_layer_removing_only_the_address_is_caught_on_the_merged_document(isolated_cwd, tmp_path):
    """§9.5, мина 1 через явный `null`: слой пишет `url: null` поверх базовой пары. САМ
    слой законен — он ничего не задаёт, и проверка слоя молчит правильно; половина
    рождается только в мерже, где остался свой токен базы без адреса. Ловит её точка A на
    итоговом документе — то есть точки не дублируют друг друга, а закрывают разное."""
    from gc_core.config import load_config_ex

    base = tmp_path / "base_config.yml"
    _write_yaml(base, _BASE_WITH_A_WHOLE_PAIR)
    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"models": {"gc": {"url": None}}})

    with pytest.raises(ValueError, match="задан свой токен, но не задан адрес"):
        load_config_ex(str(base))


def test_weakened_transport_notice_reaches_the_startup_notices(isolated_cwd, tmp_path):
    """§7: запись без своего подключения работает ОБЩИМ токеном и при этом вправе
    ослабить транспорт под собой. Это notice, а не отказ, — значит он обязан ДОЕХАТЬ:
    канал стартовых предупреждений один (`compat_notices`), и сказанное валидатором
    в пустоту равно несказанному."""
    from gc_core.config import load_config_ex

    base = tmp_path / "base_config.yml"
    _write_yaml(
        base,
        {
            "backend": {"api_mode": "chat_completions", "completions_url": "http://127.0.0.1:8014/v1/chat/completions"},
            "models": {"gc": {"model": "qwen/qwen3-coder", "backend": {"use_ssl": False}}},
        },
    )

    config_dict, _meta = load_config_ex(str(base))
    assert "запись gc ослабляет защиту запроса, который идёт с общим токеном" in config_dict["compat_notices"]


# --- ссылка ${ENV:…}, которая не подставилась, обязана называть себя ---


def _pair_error(entry: dict, env: dict, monkeypatch) -> str:
    """Вердикт правила пары для записи, прогнанной через резолвер, как это делает
    загрузчик: сперва подстановка, потом проверка."""
    from gc_core.config_modules.env_interpolation import resolve_env_placeholders
    from gc_core.config_modules.model_registry import validate_registry_pairs

    for name in ("PAIR_URL", "PAIR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    raw = {"models": {"corp": dict(entry)}}
    try:
        validate_registry_pairs(resolve_env_placeholders(raw), source="config.yml", raw_source=raw)
    except ValueError as exc:
        return str(exc)
    return ""


def test_an_unresolved_reference_names_the_variable_not_the_config(monkeypatch):
    """Запись с плейсхолдерами в обоих концах пары — ПРАВИЛЬНАЯ форма (docs §«Секреты»).
    Пока переменные не экспортированы, ссылки схлопываются в "" и пара разваливается,
    но винить конфиг за это нельзя: человек забыл export, а не ошибся в файле."""
    error = _pair_error({"model": "m", "url": "${ENV:PAIR_URL}", "token": "${ENV:PAIR_TOKEN}"}, {}, monkeypatch)
    assert "PAIR_URL" in error and "PAIR_TOKEN" in error, error
    assert "задан свой токен, но не задан адрес" not in error, error


def test_a_literal_violation_still_speaks_about_the_config(monkeypatch):
    """Контроль: ссылок нет — прежний текст про пару остаётся, он там верен."""
    error = _pair_error({"model": "m", "url": "https://h/v1"}, {}, monkeypatch)
    assert "задан свой адрес, но не задан токен" in error, error


def test_a_resolved_reference_is_judged_by_its_value(monkeypatch):
    """Контроль: переменные заданы — пара цела, отказа нет."""
    assert (
        _pair_error(
            {"model": "m", "url": "${ENV:PAIR_URL}", "token": "${ENV:PAIR_TOKEN}"},
            {"PAIR_URL": "https://h/v1", "PAIR_TOKEN": "sk-1"},
            monkeypatch,
        )
        == ""
    )
