"""Integration-тесты для layered config: <cwd>/.gc/config.yml поверх global config.yml."""

import os
from pathlib import Path

import pytest
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from pydantic import ValidationError


def test_project_local_config_path_returns_path_when_file_exists(
    tmp_path, monkeypatch
):
    from gc_core.config_modules.env_vars import project_local_config_path

    gc_dir = tmp_path / ".gc"
    gc_dir.mkdir()
    project_cfg = gc_dir / "config.yml"
    project_cfg.write_text("agent:\n  max_steps: 100\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    result = project_local_config_path()
    assert result is not None
    assert os.path.abspath(result) == str(project_cfg.resolve())


def test_project_local_config_path_returns_none_when_missing(
    tmp_path, monkeypatch
):
    from gc_core.config_modules.env_vars import project_local_config_path

    monkeypatch.chdir(tmp_path)
    # Никакого .gc/config.yml в tmp_path нет.
    assert project_local_config_path() is None


def test_project_local_config_path_returns_none_when_gc_dir_is_file(
    tmp_path, monkeypatch
):
    from gc_core.config_modules.env_vars import project_local_config_path

    # Краевой случай: <cwd>/.gc — это файл, а не директория. Нет config.yml внутри.
    (tmp_path / ".gc").write_text("not a dir", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert project_local_config_path() is None


# Минимальный валидный глобальный config для всех тестов layered loading.
# AppConfig имеет default_factory для всех топ-полей, но мы фиксируем явные значения,
# на которых будем проверять override-семантику.
_MINIMAL_GLOBAL_CONFIG = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"

agent:
  model: TestModel
  max_steps: 50
  stop_tools: [bash_run, fs_write]
"""


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """
    Изолировать env_vars singleton + платформу + cwd. Тот же паттерн, что
    в tests/test_config_resolve_paths.py — иначе resolve_paths() читает
    реальный GC_HOME / XDG_*-окружение пользователя, и тесты становятся
    нестабильны на разных машинах.
    """
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    return fake


@pytest.fixture
def global_config_file(tmp_path):
    """Записать минимальный валидный global config в tmp_path и вернуть путь."""
    path = tmp_path / "global_config.yml"
    path.write_text(_MINIMAL_GLOBAL_CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch, isolated_env):
    """Изолированный cwd внутри tmp_path под подпапкой 'project' + isolated_env."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    return project_dir


def _write_project_config(project_dir: Path, content: str) -> Path:
    gc_dir = project_dir / ".gc"
    gc_dir.mkdir(exist_ok=True)
    project_cfg = gc_dir / "config.yml"
    project_cfg.write_text(content, encoding="utf-8")
    return project_cfg


def test_global_only_when_no_project_config(global_config_file, isolated_cwd):
    from gc_core.config import load_settings

    settings = load_settings(str(global_config_file)).settings
    assert settings.agent.max_steps == 50
    assert settings.agent.stop_tools == ["bash_run", "fs_write"]


def test_project_overrides_scalar(global_config_file, isolated_cwd):
    from gc_core.config import load_settings

    _write_project_config(isolated_cwd, "agent:\n  max_steps: 999\n")
    settings = load_settings(str(global_config_file)).settings
    assert settings.agent.max_steps == 999
    # Не-переопределённые поля из global сохраняются.
    assert settings.agent.stop_tools == ["bash_run", "fs_write"]


def test_project_extends_list_with_dedup(global_config_file, isolated_cwd):
    from gc_core.config import load_settings

    # project добавляет новый stop_tool, плюс дублирует существующий.
    _write_project_config(
        isolated_cwd,
        "agent:\n  stop_tools: [bash_run, browse_url]\n",
    )
    settings = load_settings(str(global_config_file)).settings
    # Порядок: сначала global (bash_run, fs_write), потом новые из project (browse_url).
    # bash_run — дубль, не повторяется.
    assert settings.agent.stop_tools == ["bash_run", "fs_write", "browse_url"]


def test_project_extends_dict_mcp_servers(
    global_config_file, isolated_cwd, tmp_path
):
    """Project добавляет новый MCP-сервер, не трогая существующие из global."""
    from gc_core.config import load_settings

    # Перезаписываем global с MCP-серверами.
    extended_global = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
agent:
  model: TestModel
  client_name: test_client
  x_cl_id: "0"
mcp:
  servers:
    fileserver:
      transport: http
      url: "http://localhost:8080"
"""
    extended_path = tmp_path / "extended_global.yml"
    extended_path.write_text(extended_global, encoding="utf-8")

    _write_project_config(
        isolated_cwd,
        """
mcp:
  servers:
    db:
      transport: stdio
      command: psql
""",
    )
    settings = load_settings(str(extended_path)).settings
    assert "fileserver" in settings.mcp.servers
    assert "db" in settings.mcp.servers
    # fileserver сохранил свои поля из global.
    assert settings.mcp.servers["fileserver"].url == "http://localhost:8080"


def test_project_overrides_nested_scalar(
    global_config_file, isolated_cwd, tmp_path
):
    """Project переопределяет конкретное scalar-поле внутри dict в global."""
    from gc_core.config import load_settings

    extended_global = """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
agent:
  model: TestModel
  client_name: test_client
  x_cl_id: "0"
mcp:
  servers:
    fileserver:
      transport: http
      url: "http://localhost:8080"
"""
    extended_path = tmp_path / "extended_global.yml"
    extended_path.write_text(extended_global, encoding="utf-8")

    _write_project_config(
        isolated_cwd,
        """
mcp:
  servers:
    fileserver:
      url: "http://localhost:9090"
""",
    )
    settings = load_settings(str(extended_path)).settings
    # url переопределён в project.
    assert settings.mcp.servers["fileserver"].url == "http://localhost:9090"
    # transport остался из global.
    assert settings.mcp.servers["fileserver"].transport == "http"


def test_explicit_config_pointing_to_project_local_no_recursion(
    isolated_cwd, monkeypatch
):
    """
    Если CLI запущен как `gc-cli --config <cwd>/.gc/config.yml`, layered loading
    не применяется (защита от рекурсии: иначе deep_merge мерджил бы файл сам с собой).

    Этот тест активно ловит удаление recursion guard через подсчёт open() на этот файл —
    deep_merge идемпотентен при self-merge, так что простая проверка cfg равенства
    не отличила бы guard'нутую версию от unguard'нутой.
    """
    import builtins

    from gc_core.config import load_settings

    project_cfg = _write_project_config(
        isolated_cwd,
        """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
agent:
  model: TestModel
  client_name: test_client
  x_cl_id: "0"
  max_steps: 77
  stop_tools: [foo]
""",
    )

    project_cfg_abs = os.path.abspath(str(project_cfg))
    open_calls: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        try:
            open_calls.append(os.path.abspath(str(file)))
        except Exception:
            pass
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)

    settings = load_settings(str(project_cfg)).settings

    # Recursion guard работает: project_cfg открыт ровно один раз — как global.
    # Без guard'а файл бы открылся дважды (global + project local).
    assert open_calls.count(project_cfg_abs) == 1, (
        f"Expected project config file opened exactly once (recursion guard works); "
        f"got {open_calls.count(project_cfg_abs)} opens for {project_cfg_abs}. "
        f"All open calls: {open_calls}"
    )

    # Дополнительная проверка end-state: данные из файла применились корректно.
    assert settings.agent.stop_tools == ["foo"]
    assert settings.agent.max_steps == 77


def test_cli_extra_overrides_win_over_layered(
    global_config_file, isolated_cwd
):
    """CLI args (через apply_cli_overrides extra_overrides) побеждают layered config."""
    from gc_core.config import apply_cli_overrides, load_settings

    _write_project_config(isolated_cwd, "agent:\n  max_steps: 999\n")
    loaded = load_settings(str(global_config_file))
    # Project уже применился: max_steps == 999.
    assert loaded.settings.agent.max_steps == 999

    # CLI перебивает: max_steps == 12345.
    overridden = apply_cli_overrides(
        loaded, extra_overrides={"agent.max_steps": 12345}
    )
    assert overridden.settings.agent.max_steps == 12345
    assert overridden.provenance.source("agent.max_steps").layer == "cli"


def test_invalid_yaml_in_project_loud_error(global_config_file, isolated_cwd):
    """Невалидный YAML в project config → громкая ошибка, не silent fallback.

    Класс — `ValueError` (с путём файла и координатой из парсера), а не сырой
    `yaml.YAMLError`: чтение слоёв держит тот же контракт, что `_raw_from_text`
    для текста и `validate_profile_text` для профиля. Не косметика — pre-write
    валидация стека ловит `(OSError, ValueError)` и откатывается к одному файлу,
    а `yaml.YAMLError` уходил мимо этого отката и ронял /settings на рендере
    меню. Предмет стража прежний: молчания здесь быть не должно."""
    from gc_core.config import load_settings

    _write_project_config(
        isolated_cwd,
        "this is: : : not [valid yaml }\n",
    )
    with pytest.raises(ValueError, match="невалидный YAML"):
        load_settings(str(global_config_file))


def test_invalid_schema_in_project_loud_error(
    global_config_file, isolated_cwd
):
    """
    Невалидная схема в project config → ValidationError от Pydantic, не silent fallback.

    Лишний ключ отказа больше не даёт (отбрасывается с уведомлением, см.
    config_modules/unknown_keys.py) — ломаем негодным значением: `api_mode` вне
    `responses`/`chat_completions` отвергает валидатор поля.

    Ревизия (MR #80, класс L28): отказ схемы приходит завёрнутым — ValueError с
    текстом из координат и причин, БЕЗ pydantic-дампа (`input_value` нёс токен из
    файла в хинт пикера); сырой `ValidationError` — в `__cause__`, по нему
    `explain_broken_config` берёт точные координаты.
    """
    from gc_core.config import load_settings

    _write_project_config(
        isolated_cwd,
        "backend:\n  api_mode: bogus\n",
    )
    with pytest.raises(ValueError, match="не проходит схему") as excinfo:
        load_settings(str(global_config_file))
    assert isinstance(excinfo.value.__cause__, ValidationError)
    assert "input_value" not in str(excinfo.value)


def test_invalid_yaml_in_base_loud_error(tmp_path):
    """Невалидный YAML в БАЗОВОМ config.yml → громкая ValueError с путём файла и
    причиной — симметрично test_invalid_yaml_in_project_loud_error, но для базы.

    Раньше базу читали голыми open()/yaml.safe_load() (config.py:492-493) без
    какой-либо защиты: наружу летел сырой yaml.ScannerError, а не ValueError —
    ни один из входов (cli.py/tui/__init__.py) не смог бы поймать его одним
    except ValueError."""
    from gc_core.config import load_settings

    base = tmp_path / "base_config.yml"
    base.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")
    with pytest.raises(ValueError, match="невалидный YAML"):
        load_settings(str(base))


def test_base_config_not_dict_loud_error(tmp_path):
    """Список вместо словаря на верхнем уровне БАЗОВОГО config.yml → ValueError,
    а не AttributeError из глубины мержа/схемы (`'list' object has no attribute
    'get'`). `_layer_document` уже отвергает такой документ явно — базе того же
    приёма не хватало."""
    from gc_core.config import load_settings

    base = tmp_path / "base_config.yml"
    base.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_settings(str(base))
    assert not isinstance(exc_info.value, AttributeError)


def test_unreadable_base_config_loud_error(tmp_path):
    """Несуществующий путь БАЗОВОГО config.yml → ValueError с именем файла, а не
    сырой FileNotFoundError/OSError."""
    from gc_core.config import load_settings

    missing = tmp_path / "does_not_exist.yml"
    with pytest.raises(ValueError) as exc_info:
        load_settings(str(missing))
    assert str(missing) in str(exc_info.value)


def test_empty_project_config_falls_back_to_global(
    global_config_file, isolated_cwd
):
    """Пустой project config = пустой dict; deep_merge с global = global. Это нормальное поведение."""
    from gc_core.config import load_settings

    _write_project_config(isolated_cwd, "")  # абсолютно пустой файл
    settings = load_settings(str(global_config_file)).settings
    # max_steps из global, никаких ошибок.
    assert settings.agent.max_steps == 50


def test_apply_cli_overrides_sets_agentsmd_override():
    """apply_cli_overrides(agentsmd_override=True) выставляет agentsmd_override в True."""
    from gc_core.config import apply_cli_overrides
    from gc_testkit.settings import make_settings

    loaded = make_settings({"tui.agentsmd_override": False})
    settings = apply_cli_overrides(loaded, agentsmd_override=True).settings
    assert settings.tui.agentsmd_override is True


def test_apply_cli_overrides_leaves_agentsmd_override_false_by_default():
    """apply_cli_overrides() без флага НЕ трогает agentsmd_override."""
    from gc_core.config import apply_cli_overrides
    from gc_testkit.settings import make_settings

    loaded = make_settings({"tui.agentsmd_override": False})
    settings = apply_cli_overrides(loaded).settings
    assert settings.tui.agentsmd_override is False


def test_hiding_the_project_layer_works_from_its_home_module(
    global_config_file, isolated_cwd, monkeypatch
):
    """Проектный слой глушится подменой у ВЛАДЕЛЬЦА пути — и подмена обязана
    действовать на весь загрузчик.

    Адрес чтения один: и `_merge_layers`, и снимок свежести спрашивают
    `config_modules.env_vars`. Пока загрузчик звал импортированное к себе имя, а
    слои — владельца, подмена одного адреса другой не задевала: тесты глушили
    слой, который всё равно читался. Зелено было лишь потому, что машинный конфиг
    разработчика совместим — на другой машине сьют проверял бы не то состояние,
    которое считает своим."""
    from gc_core import config as config_mod
    from gc_core.config_modules import env_vars

    _write_project_config(isolated_cwd, "agent:\n  max_steps: 4242\n")
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)

    settings = config_mod.load_settings(str(global_config_file)).settings

    assert settings.agent.max_steps == 50  # значение базы, слой скрыт


def test_pointing_the_managed_overlay_works_from_its_home_module(
    global_config_file, isolated_cwd, monkeypatch, tmp_path
):
    """То же для machine-managed оверлея: подмена у владельца
    (`mcp_runtime.paths`) действует и на слой, и на снимок свежести."""
    from gc_core import config as config_mod

    managed = tmp_path / "managed.yml"
    managed.write_text("agent:\n  max_steps: 777\n", encoding="utf-8")
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path", lambda: managed
    )

    settings = config_mod.load_settings(str(global_config_file)).settings

    assert (
        settings.agent.max_steps == 777
    )  # патч действует: оверлей взят отсюда
