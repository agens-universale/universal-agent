"""Integration-тесты для layered config: <cwd>/.gc/config.yml поверх global config.yml."""

import os
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config_modules import env_vars
from config_modules.env_vars import EnvVars


def test_project_local_config_path_returns_path_when_file_exists(tmp_path, monkeypatch):
    from config import _project_local_config_path

    gc_dir = tmp_path / ".gc"
    gc_dir.mkdir()
    project_cfg = gc_dir / "config.yml"
    project_cfg.write_text("agent:\n  max_steps: 100\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    result = _project_local_config_path()
    assert result is not None
    assert os.path.abspath(result) == str(project_cfg.resolve())


def test_project_local_config_path_returns_none_when_missing(tmp_path, monkeypatch):
    from config import _project_local_config_path

    monkeypatch.chdir(tmp_path)
    # Никакого .gc/config.yml в tmp_path нет.
    assert _project_local_config_path() is None


def test_project_local_config_path_returns_none_when_gc_dir_is_file(tmp_path, monkeypatch):
    from config import _project_local_config_path

    # Краевой случай: <cwd>/.gc — это файл, а не директория. Нет config.yml внутри.
    (tmp_path / ".gc").write_text("not a dir", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _project_local_config_path() is None


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
    from config import load_config

    cfg = load_config(str(global_config_file))
    assert cfg["max_steps"] == 50
    assert cfg["stop_tools"] == ["bash_run", "fs_write"]


def test_project_overrides_scalar(global_config_file, isolated_cwd):
    from config import load_config

    _write_project_config(isolated_cwd, "agent:\n  max_steps: 999\n")
    cfg = load_config(str(global_config_file))
    assert cfg["max_steps"] == 999
    # Не-переопределённые поля из global сохраняются.
    assert cfg["stop_tools"] == ["bash_run", "fs_write"]


def test_project_extends_list_with_dedup(global_config_file, isolated_cwd):
    from config import load_config

    # project добавляет новый stop_tool, плюс дублирует существующий.
    _write_project_config(
        isolated_cwd,
        "agent:\n  stop_tools: [bash_run, browse_url]\n",
    )
    cfg = load_config(str(global_config_file))
    # Порядок: сначала global (bash_run, fs_write), потом новые из project (browse_url).
    # bash_run — дубль, не повторяется.
    assert cfg["stop_tools"] == ["bash_run", "fs_write", "browse_url"]


def test_project_extends_dict_mcp_servers(global_config_file, isolated_cwd, tmp_path):
    """Project добавляет новый MCP-сервер, не трогая существующие из global."""
    from config import load_config

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
    cfg = load_config(str(extended_path))
    assert "fileserver" in cfg["mcp-servers"]
    assert "db" in cfg["mcp-servers"]
    # fileserver сохранил свои поля из global.
    assert cfg["mcp-servers"]["fileserver"]["url"] == "http://localhost:8080"


def test_project_overrides_nested_scalar(global_config_file, isolated_cwd, tmp_path):
    """Project переопределяет конкретное scalar-поле внутри dict в global."""
    from config import load_config

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
    cfg = load_config(str(extended_path))
    # url переопределён в project.
    assert cfg["mcp-servers"]["fileserver"]["url"] == "http://localhost:9090"
    # transport остался из global.
    assert cfg["mcp-servers"]["fileserver"]["transport"] == "http"


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

    from config import load_config

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

    cfg = load_config(str(project_cfg))

    # Recursion guard работает: project_cfg открыт ровно один раз — как global.
    # Без guard'а файл бы открылся дважды (global + project local).
    assert open_calls.count(project_cfg_abs) == 1, (
        f"Expected project config file opened exactly once (recursion guard works); "
        f"got {open_calls.count(project_cfg_abs)} opens for {project_cfg_abs}. "
        f"All open calls: {open_calls}"
    )

    # Дополнительная проверка end-state: данные из файла применились корректно.
    assert cfg["stop_tools"] == ["foo"]
    assert cfg["max_steps"] == 77


def test_cli_extra_overrides_win_over_layered(global_config_file, isolated_cwd):
    """CLI args (через apply_cli_overrides extra_overrides) побеждают layered config."""
    from config import apply_cli_overrides, load_config

    _write_project_config(isolated_cwd, "agent:\n  max_steps: 999\n")
    cfg = load_config(str(global_config_file))
    # Project уже применился: max_steps == 999.
    assert cfg["max_steps"] == 999

    # CLI перебивает: max_steps == 12345.
    apply_cli_overrides(cfg, extra_overrides={"max_steps": 12345})
    assert cfg["max_steps"] == 12345


def test_invalid_yaml_in_project_loud_error(global_config_file, isolated_cwd):
    """Невалидный YAML в project config → yaml.YAMLError, не silent fallback."""
    from config import load_config

    _write_project_config(
        isolated_cwd,
        "this is: : : not [valid yaml }\n",
    )
    with pytest.raises(yaml.YAMLError):
        load_config(str(global_config_file))


def test_invalid_schema_in_project_loud_error(global_config_file, isolated_cwd):
    """
    Невалидная схема в project config → ValidationError от Pydantic, не silent fallback.

    AppConfig (src/config_modules/root.py) имеет model_config = ConfigDict(extra='forbid').
    Добавление несуществующего top-level поля гарантированно ломает model_validate.

    (Полагаться на отказ coerce'ить scalar в int нельзя: Pydantic v2 по умолчанию
    coerce'ит "123" → 123 без ошибки, и тест становится хрупким. Extra-forbid —
    чёткий тест.)
    """
    from config import load_config

    _write_project_config(
        isolated_cwd,
        "this_top_level_field_does_not_exist_in_appconfig_schema: 123\n",
    )
    with pytest.raises(ValidationError):
        load_config(str(global_config_file))


def test_empty_project_config_falls_back_to_global(global_config_file, isolated_cwd):
    """Пустой project config = пустой dict; deep_merge с global = global. Это нормальное поведение."""
    from config import load_config

    _write_project_config(isolated_cwd, "")  # абсолютно пустой файл
    cfg = load_config(str(global_config_file))
    # max_steps из global, никаких ошибок.
    assert cfg["max_steps"] == 50


def test_apply_cli_overrides_sets_agentsmd_override():
    """apply_cli_overrides(agentsmd_override=True) sets config_dict key to True."""
    from config import apply_cli_overrides

    cfg: dict = {"agentsmd_override": False}
    apply_cli_overrides(cfg, agentsmd_override=True)
    assert cfg["agentsmd_override"] is True


def test_apply_cli_overrides_leaves_agentsmd_override_false_by_default():
    """apply_cli_overrides() without flag does NOT override agentsmd_override."""
    from config import apply_cli_overrides

    cfg: dict = {"agentsmd_override": False}
    apply_cli_overrides(cfg)
    assert cfg["agentsmd_override"] is False
