"""Лоадер профилей-файлов (спека `docs/specs/2026-07-23-profiles-design.md` §3):
два корня, поиск по имени, двухфазная валидация, оверлей-слой в `load_config_ex`.
"""
from pathlib import Path

import pytest
import yaml

from config_modules import env_vars
from config_modules.env_vars import EnvVars
from config_modules.profiles import (
    discover_profiles,
    load_profile_document,
    profile_roots,
    resolve_profile,
)


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
def isolated_env(monkeypatch, tmp_path):
    """Изолировать env_vars singleton + платформу (тот же паттерн, что в
    tests/test_config_layered.py) — иначе managed_config_path()/gc_paths() читают
    реальное окружение машины и тесты становятся нестабильны."""
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path / "gc_home")
    monkeypatch.setattr(env_vars, "env", fake)
    return fake


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch, isolated_env):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    return project_dir


# --- Юнит-уровень: profile_roots / discover_profiles / resolve_profile / load_profile_document ---


def test_project_root_wins_name_collision(tmp_path):
    project_dir = tmp_path / "proj"
    base_config = tmp_path / "base" / "config.yml"
    base_config.parent.mkdir(parents=True)
    base_config.write_text("agent:\n  model: x\n", encoding="utf-8")

    roots = profile_roots(project_dir, base_config)
    _write_yaml(roots[0] / "work.yml", {"agent": {"model": "project-work"}})
    _write_yaml(roots[1] / "work.yml", {"agent": {"model": "base-work"}})

    discovered = discover_profiles(roots)
    assert discovered["work"].root_kind == "project"
    assert discovered["work"].path == roots[0] / "work.yml"

    ref = resolve_profile("work", roots)
    assert ref.root_kind == "project"
    assert ref.path == roots[0] / "work.yml"


def test_explicit_config_excludes_home_profiles(tmp_path, monkeypatch):
    """`--config /opt/x/config.yml` явный → второй корень = `/opt/x/profiles`,
    `~/.gc/profiles` (здесь — изолированный аналог домашней папки) не участвует."""
    from config import load_config_ex

    monkeypatch.setattr("sys.platform", "linux")
    fake_env = EnvVars(gc_home=tmp_path / "gc_home")
    monkeypatch.setattr(env_vars, "env", fake_env)

    # Профиль лежит "рядом с базой" домашнего конфига — но запуск идёт с explicit --config.
    _write_yaml(tmp_path / "gc_home" / "profiles" / "work.yml", {"agent": {"model": "home-work"}})

    explicit_config = tmp_path / "explicit" / "config.yml"
    _write_yaml(explicit_config, _MINIMAL_BASE)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    # Будь домашний корень в игре — "work" резолвился бы. Явный --config его исключает.
    with pytest.raises(ValueError, match="не найден"):
        load_config_ex(str(explicit_config), profile_override="work")


def test_profile_key_inside_profile_forbidden(tmp_path):
    profile_path = _write_yaml(tmp_path / "work.yml", {"profile": "other", "agent": {"model": "x"}})
    with pytest.raises(ValueError, match="запрещ"):
        load_profile_document(profile_path)


def test_removed_key_in_profile_is_dropped_with_notice_not_fatal(tmp_path):
    """Устаревший ключ (`compat.REMOVED`) в профиле — тот же тихий выброс с
    нотисом, что и в config.yml: compat идёт ДО схемы. Иначе профиль, собранный
    копипастой секции из старого config.yml, обрушивает всю загрузку простынёй
    pydantic — при том что в самом config.yml этот ключ проглатывается."""
    profile_path = _write_yaml(
        tmp_path / "work.yml",
        {"agent": {"model": "x"}, "update": {"check_interval_days": 3}},
    )
    notices: list[str] = []

    raw = load_profile_document(profile_path, notices=notices)

    assert "check_interval_days" not in (raw.get("update") or {})
    assert any("check_interval_days" in n and str(profile_path) in n for n in notices)


def test_not_found_error_lists_paths_and_available(tmp_path):
    project_dir = tmp_path / "proj"
    base_config = tmp_path / "base" / "config.yml"
    base_config.parent.mkdir(parents=True)
    base_config.write_text("agent:\n  model: x\n", encoding="utf-8")
    roots = profile_roots(project_dir, base_config)
    _write_yaml(roots[0] / "corp.yml", {"agent": {"model": "corp"}})
    _write_yaml(roots[1] / "work.yml", {"agent": {"model": "w"}})
    _write_yaml(roots[1] / "home.yml", {"agent": {"model": "h"}})

    with pytest.raises(ValueError) as exc_info:
        resolve_profile("wrok", roots)
    msg = str(exc_info.value)
    assert str(roots[0] / "wrok.yml") in msg
    assert str(roots[1] / "wrok.yml") in msg
    assert "work" in msg
    assert "home" in msg
    assert "corp" in msg
    assert "глобальные" in msg
    assert "проектный" in msg


def test_reserved_profile_name_ignored_and_pointer_errors(tmp_path):
    project_dir = tmp_path / "proj"
    base_config = tmp_path / "base" / "config.yml"
    base_config.parent.mkdir(parents=True)
    base_config.write_text("agent:\n  model: x\n", encoding="utf-8")
    roots = profile_roots(project_dir, base_config)
    _write_yaml(roots[1] / "light.yml", {"agent": {"model": "reserved-file"}})
    _write_yaml(roots[1] / "corp.yml", {"agent": {"model": "corp"}})

    notices: list[str] = []
    discovered = discover_profiles(roots, notices=notices)
    assert "light" not in discovered
    assert "corp" in discovered
    assert any("зарезервировано" in n for n in notices)

    with pytest.raises(ValueError, match="зарезервировано"):
        resolve_profile("light", roots)


# --- Интеграционный уровень: load_config_ex (base→managed→project→profile→compat→validate) ---


def test_profile_content_overlays_project_config(isolated_cwd, tmp_path):
    from config import load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(
        isolated_cwd / ".gc" / "config.yml",
        {"profile": "work", "summary": {"chat_completions_summary_trigger_chars": 1}},
    )
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml",
        {"summary": {"chat_completions_summary_trigger_chars": 2}},
    )

    cfg, meta = load_config_ex(str(base_config))
    assert cfg["summary_params"]["chat_completions_summary_trigger_chars"] == 2
    assert meta.active_name == "work"
    assert meta.ref is not None and meta.ref.root_kind == "project"


def test_level_from_profile_beats_project_but_loses_to_cli(isolated_cwd, tmp_path):
    from config import apply_cli_overrides, load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "work", "level": "max"})
    _write_yaml(isolated_cwd / ".gc" / "profiles" / "work.yml", {"level": "light"})

    cfg, meta = load_config_ex(str(base_config))
    assert cfg["level"] == "light"

    apply_cli_overrides(cfg, extra_overrides={"level": "default"})
    assert cfg["level"] == "default"


def test_env_interpolation_per_file_before_validation(isolated_cwd, tmp_path, monkeypatch):
    from config import load_config_ex

    monkeypatch.setenv("GC_PROFILE_TEST_TIMEOUT", "42")

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "work"})
    # backend.request_timeout_sec — числовое поле (int(value)); плейсхолдер должен
    # разрешиться ДО фазы-1-валидации профиля, иначе int() упал бы на "${ENV:...}".
    profile_path = isolated_cwd / ".gc" / "profiles" / "work.yml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        'backend:\n  request_timeout_sec: "${ENV:GC_PROFILE_TEST_TIMEOUT}"\n',
        encoding="utf-8",
    )

    cfg, meta = load_config_ex(str(base_config))
    assert cfg["backend_request_timeout_sec"] == 42


def test_mtime_set_includes_both_candidate_paths(isolated_cwd, tmp_path):
    from config import load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "work"})
    project_profile = isolated_cwd / ".gc" / "profiles" / "work.yml"
    _write_yaml(project_profile, {"agent": {"model": "y"}})

    cfg, meta = load_config_ex(str(base_config))
    base_root_candidate = base_config.parent / "profiles" / "work.yml"

    assert str(project_profile) in meta.mtime_set
    assert meta.mtime_set[str(project_profile)] is not None
    assert str(base_root_candidate) in meta.mtime_set
    assert meta.mtime_set[str(base_root_candidate)] is None  # второй корень — файла нет


def test_reserved_profile_file_warns_via_load_config_ex(isolated_cwd, tmp_path):
    """light.yml лежит рядом с активным профилем corp — резолв corp дискаверит весь
    корень попутно и обязан долетающим warning'ом отметить проигнорированный light.yml
    (спека §3.2). Раньше `resolve_profile` звал `discover_profiles(roots)` без `notices`,
    и предупреждение терялось — здесь бьём через реальный пайплайн `load_config_ex`."""
    from config import load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "corp"})
    _write_yaml(isolated_cwd / ".gc" / "profiles" / "corp.yml", {"agent": {"model": "corp-model"}})
    _write_yaml(isolated_cwd / ".gc" / "profiles" / "light.yml", {"agent": {"model": "reserved-file"}})

    cfg, meta = load_config_ex(str(base_config))
    assert any("зарезервировано" in n for n in meta.notices)
    assert any("зарезервировано" in n for n in cfg["compat_notices"])


def test_project_profile_literal_token_warns(isolated_cwd, tmp_path):
    from config import load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "work"})
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "work.yml",
        {
            "models": {
                "flash": {
                    "url": "http://x/v1/chat/completions",
                    "token": "sk-literal-secret",
                }
            }
        },
    )

    cfg, meta = load_config_ex(str(base_config))
    assert any("token" in n and "sk-literal-secret" not in n for n in meta.notices)
    assert any("token" in n for n in cfg["compat_notices"])


def test_base_root_profile_literal_token_does_not_warn(isolated_env, isolated_cwd, tmp_path):
    """Обратная половина предыдущего: предупреждение адресное, а не всеобщее.

    Смысл warning'а — «этот файл обычно лежит в репозитории» (§3.5), и он
    привязан к корню (`config.py`: `warn_literal_tokens=(root_kind == "project")`).
    Без негативной половины страж молчал бы о снятии условия: варнить на КАЖДЫЙ
    литеральный токен значит варнить на приватный `~/.gc/profiles/*.yml`, где
    секрет лежит законно, — и приучить пользователя не читать notice."""
    from config import load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"profile": "work"})
    _write_yaml(
        base_config.parent / "profiles" / "work.yml",
        {"models": {"flash": {"url": "http://x/v1/chat/completions",
                              "token": "sk-literal-secret"}}},
    )

    cfg, meta = load_config_ex(str(base_config))
    assert meta.ref is not None and meta.ref.root_kind == "base"
    assert not [n for n in meta.notices if "token" in n]
    assert not [n for n in cfg["compat_notices"] if "token" in n]


def test_registry_merges_across_layers_overlay_cannot_remove_a_model(isolated_cwd, tmp_path):
    """§14 «мерж реестров по слоям»: добавление алиаса, перекрытие алиаса и
    «оверлей не может убрать модель» — три семантики одним снимком.

    Родовой `test_config_merge.py` проверяет их на абстрактном dict, но реестр
    моделей проходит через `flatten_app_config`, и то, что `models` не попал в
    список секций-исключений мержа, не утверждает никто.

    Громкую половину последствий фаза 2 ловит сама: профиль, заменивший реестр
    базы, оставил бы `slots.default` базового слоя висеть на исчезнувшем алиасе,
    и загрузка упала бы с `slots.default: алиас "keep" не найден в
    merged-реестре models`. Именно так этот тест и валится под мутацией мержа —
    по ГРОМКОМУ пути.

    Молчит фаза 2 в другом случае: оверлей подменяет реестр ВМЕСТЕ со слотами —
    ссылки сходятся, ошибки нет, модели базового слоя просто исчезают (проверено
    отдельно: `load_config_ex` отдаёт реестр из одного профиля). Такого сценария
    тест НЕ строит. Первый ассерт ниже, состав реестра, поймал бы и его, но
    доказано это здесь только на громком пути."""
    from config import load_config_ex

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {
        "profile": "work",
        "models": {"keep": {"url": "http://keep/v1", "token": "sk-keep"},
                   "shared": {"url": "http://base/v1", "token": "sk-base"}},
        "slots": {"default": "keep"},
    })
    # Перекрытие алиаса — парой целиком (§2): слой, задавший только адрес, увёз бы
    # токен базы на свой хост и отвергается правилом ещё до мержа.
    _write_yaml(isolated_cwd / ".gc" / "profiles" / "work.yml", {
        "models": {"shared": {"url": "http://profile/v1", "token": "sk-profile"},
                   "added": {"url": "http://added/v1", "token": "sk-added"}},
    })

    cfg, _meta = load_config_ex(str(base_config))
    models = cfg["models"]

    assert set(models) == {"keep", "shared", "added"}
    assert models["keep"]["url"] == "http://keep/v1"          # оверлей не убрал
    assert models["shared"]["url"] == "http://profile/v1"     # оверлей перекрыл
    assert models["added"]["url"] == "http://added/v1"        # оверлей добавил


def test_model_inherits_from_backend_overridden_by_profile(isolated_cwd, tmp_path):
    """§14 Router: наследование от ЭФФЕКТИВНОГО backend, «включая перекрытый
    профилем». Запись реестра без своих url/token берёт их из backend ПОСЛЕ мержа
    всей стопки — то есть из профиля, если тот перекрыл backend; иначе профиль
    «сменить провайдера одной строкой» уводил бы адрес только у явных записей."""
    from config import load_config_ex
    from model_router import ModelRouter

    base_config = tmp_path / "base_config.yml"
    _write_yaml(base_config, _MINIMAL_BASE)

    _write_yaml(isolated_cwd / ".gc" / "config.yml", {
        "profile": "work",
        "models": {"bare": {}},
        "slots": {"default": "bare"},
    })
    _write_yaml(isolated_cwd / ".gc" / "profiles" / "work.yml", {
        "backend": {"completions_url": "http://from-profile/v1/chat/completions"},
    })

    cfg, _meta = load_config_ex(str(base_config))
    resolved = ModelRouter(cfg, level_provider=lambda: "default").resolve("main")

    assert resolved.alias == "bare"
    assert resolved.url == "http://from-profile/v1/chat/completions"
