"""Тесты для src/swarm/runner.py.

Покрывает start_swarm_process() с моком subprocess.Popen:
- meta.json создаётся в queued, потом обновляется на running с pid + deadline_at;
- sandbox-каталог создаётся;
- env пробрасывается через Popen со всеми SWARM_* переменными;
- start_new_session=True передан в Popen.
"""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import swarm.runner as runner  # noqa: E402
import swarm.storage as storage  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_swarm_cfg(tmp_path):
    """Минимально-валидный swarm_cfg для start_swarm_process."""
    return {
        "threads_dir": str(tmp_path / "threads"),
        "sandbox_root_dir": str(tmp_path / "sandbox"),
        "wrapper_path": "/usr/bin/true",  # любой исполняемый — Popen мокается
        "cli_command": "gc-cli",
        "sandbox_tool": "bubblewrap",
        "agentmd_path": "",
        "skills_dir": "",
        "limits": {
            "memory_mb": 1024,
            "disk_mb": 2048,
            "cpu_percent": 50,
            "timeout_seconds": 1800,
        },
    }


@pytest.fixture
def mock_popen():
    """Мок subprocess.Popen, который возвращает фейковый proc с pid=12345."""
    with patch("swarm.runner.subprocess.Popen") as mock:
        proc = MagicMock()
        proc.pid = 12345
        mock.return_value = proc
        yield mock


# ---------------------------------------------------------------------------
# _make_swarm_id
# ---------------------------------------------------------------------------


def test_make_swarm_id_concatenates_system_task():
    assert runner._make_swarm_id("ci", "build-42") == "ci_build-42"


def test_make_swarm_id_lowercases_system_strips_task():
    assert runner._make_swarm_id("CI", "  build-42  ") == "ci_build-42"


def test_make_swarm_id_empty_strings():
    """Поведение на edge — пустые значения дают вырожденный id, но не падают."""
    assert runner._make_swarm_id("", "") == "_"


# ---------------------------------------------------------------------------
# _compute_deadline_iso — шаг 1.6
# ---------------------------------------------------------------------------


def test_compute_deadline_returns_iso_format():
    result = runner._compute_deadline_iso(1800)
    # Должен парситься как ISO timestamp.
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None  # с timezone


def test_compute_deadline_in_future():
    before = datetime.now(timezone.utc)
    result = runner._compute_deadline_iso(60)
    parsed = datetime.fromisoformat(result)
    delta = (parsed - before).total_seconds()
    assert 55 < delta < 65  # ±5s tolerance


# ---------------------------------------------------------------------------
# start_swarm_process — main entry point
# ---------------------------------------------------------------------------


def test_start_swarm_process_returns_id_and_pid(base_swarm_cfg, mock_popen):
    sid, pid = runner.start_swarm_process(
        base_swarm_cfg,
        system="ci",
        task_id="build-42",
        payload={"q": "hello"},
    )
    assert sid == "ci_build-42"
    assert pid == 12345


def test_start_swarm_creates_sandbox_dir(base_swarm_cfg, mock_popen):
    runner.start_swarm_process(
        base_swarm_cfg, system="ci", task_id="t1", payload={},
    )
    sandbox_path = os.path.join(base_swarm_cfg["sandbox_root_dir"], "ci_t1")
    assert os.path.isdir(sandbox_path)


def test_start_swarm_writes_meta_with_running_status(base_swarm_cfg, mock_popen):
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    meta = storage.load_swarm_meta(base_swarm_cfg["threads_dir"], "g_t")
    assert meta["system"] == "g"
    assert meta["task_id"] == "t"
    sm = meta["swarm_meta"]
    assert sm["status"] == "running"
    assert sm["pid"] == 12345
    assert sm["sandbox_status"] == "active"
    assert "started_at" in sm
    assert "deadline_at" in sm
    assert "created_at" in sm
    assert sm["limits"]["timeout_seconds"] == 1800
    assert sm["sandbox_path"].endswith("/sandbox/g_t")


def test_start_swarm_deadline_at_matches_timeout(base_swarm_cfg, mock_popen):
    base_swarm_cfg["limits"]["timeout_seconds"] = 600
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    meta = storage.load_swarm_meta(base_swarm_cfg["threads_dir"], "g_t")
    deadline = datetime.fromisoformat(meta["swarm_meta"]["deadline_at"])
    started = datetime.fromisoformat(meta["swarm_meta"]["started_at"])
    delta = (deadline - started).total_seconds()
    assert 595 < delta < 605


def test_start_swarm_popen_called_with_start_new_session(base_swarm_cfg, mock_popen):
    """Критично для AR-6 / шага 2.1: PID == PGID для os.killpg позже."""
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    assert kwargs.get("start_new_session") is True


def test_start_swarm_popen_called_with_wrapper_path(base_swarm_cfg, mock_popen):
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    # Первый позиционный — argv-list.
    assert args[0] == [base_swarm_cfg["wrapper_path"]]


def test_start_swarm_env_contains_required_swarm_vars(base_swarm_cfg, mock_popen):
    runner.start_swarm_process(
        base_swarm_cfg, system="ci", task_id="t1", payload={"q": "go"},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs.get("env")
    assert env is not None
    # Базовые обязательные
    assert env["SWARM_ID"] == "ci_t1"
    assert env["SWARM_SYSTEM"] == "ci"
    assert env["SWARM_TASK_ID"] == "t1"
    assert env["SWARM_THREADS_DIR"] == base_swarm_cfg["threads_dir"]
    assert env["SWARM_SANDBOX_DIR"] == base_swarm_cfg["sandbox_root_dir"]
    assert env["SWARM_SANDBOX_PATH"].endswith("/sandbox/ci_t1")
    assert env["SWARM_CLI_COMMAND"] == "gc-cli"
    assert env["SWARM_SANDBOX_TOOL"] == "bubblewrap"
    # Лимиты как строки
    assert env["SWARM_LIMIT_TIMEOUT_SECONDS"] == "1800"
    assert env["SWARM_LIMIT_MEMORY_MB"] == "1024"
    assert env["SWARM_LIMIT_DISK_MB"] == "2048"
    assert env["SWARM_LIMIT_CPU_PERCENT"] == "50"
    # Payload как JSON
    assert json.loads(env["SWARM_PAYLOAD_JSON"]) == {"q": "go"}


def test_start_swarm_env_inherits_host_environ(base_swarm_cfg, mock_popen, monkeypatch):
    """env Popen должен наследовать host environ, а не быть пустым."""
    monkeypatch.setenv("MY_HOST_VAR", "host_value")
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs.get("env")
    assert env["MY_HOST_VAR"] == "host_value"


def test_start_swarm_meta_queued_persisted_before_popen(base_swarm_cfg, mock_popen, tmp_path):
    """До Popen в meta должен быть status=queued. После — running."""
    state = {"queued_seen": False}

    def capture_popen_side(*args, **kwargs):
        meta = storage.load_swarm_meta(base_swarm_cfg["threads_dir"], "g_t")
        sm = meta.get("swarm_meta", {})
        if sm.get("status") == "queued":
            state["queued_seen"] = True
        proc = MagicMock()
        proc.pid = 999
        return proc

    mock_popen.side_effect = capture_popen_side

    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    assert state["queued_seen"], "meta status=queued должен существовать до Popen"

    meta = storage.load_swarm_meta(base_swarm_cfg["threads_dir"], "g_t")
    assert meta["swarm_meta"]["status"] == "running"
    assert meta["swarm_meta"]["pid"] == 999


def test_start_swarm_optional_agentmd_skills_in_env(base_swarm_cfg, mock_popen):
    base_swarm_cfg["agentmd_path"] = "/host/agent.md"
    base_swarm_cfg["skills_dir"] = "/host/skills"
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs.get("env")
    assert env["SWARM_AGENTMD_PATH"] == "/host/agent.md"
    assert env["SWARM_SKILLS_DIR"] == "/host/skills"


# ---------------------------------------------------------------------------
# Default timeout fallback
# ---------------------------------------------------------------------------


def test_start_swarm_default_timeout_when_limits_missing(tmp_path, mock_popen):
    cfg = {
        "threads_dir": str(tmp_path / "th"),
        "sandbox_root_dir": str(tmp_path / "sb"),
        "wrapper_path": "/usr/bin/true",
        "cli_command": "gc-cli",
        "sandbox_tool": "bubblewrap",
        "limits": {},  # без timeout_seconds
    }
    runner.start_swarm_process(cfg, system="g", task_id="t", payload={})
    meta = storage.load_swarm_meta(cfg["threads_dir"], "g_t")
    deadline = datetime.fromisoformat(meta["swarm_meta"]["deadline_at"])
    started = datetime.fromisoformat(meta["swarm_meta"]["started_at"])
    delta = (deadline - started).total_seconds()
    # Дефолт 1800s.
    assert 1795 < delta < 1805


# ---------------------------------------------------------------------------
# M4 / шаг 1.10 — materialize runtime config
# ---------------------------------------------------------------------------


def test_materialize_runtime_config_writes_yaml(tmp_path):
    """Базовый smoke: вызов пишет /sandbox/.gc/config.yml."""
    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("backend:\n  api_mode: chat_completions\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    out = runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    assert out == str(sandbox / ".gc" / "config.yml")
    assert (sandbox / ".gc" / "config.yml").exists()


def test_materialize_overrides_paths_threads_data(tmp_path):
    """paths.threads_data_path должен переопределиться на /work/.gc/threads."""
    import yaml as _yaml
    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("paths:\n  threads_data_path: /host/threads\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    out = runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())
    assert written["paths"]["threads_data_path"] == "/work/.gc/threads"
    assert written["paths"]["logs_dump_dir"] == "/work/.gc/logs"


def test_materialize_overrides_use_server_subagents_false(tmp_path):
    import yaml as _yaml
    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("agent:\n  use_server_subagents: true\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())
    assert written["agent"]["use_server_subagents"] is False


def test_materialize_preserves_host_backend(tmp_path):
    """Хостовые поля backend.* должны сохраниться — sandbox юзает тот же LLM."""
    import yaml as _yaml
    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "  completions_url: http://localhost:8011/v1/chat/completions\n"
    )
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())
    assert written["backend"]["api_mode"] == "chat_completions"
    assert "localhost:8011" in written["backend"]["completions_url"]


def test_materialize_copies_swarm_agent_md(tmp_path):
    """Если задан agentmd_path — копирует в sandbox и обновляет path."""
    import yaml as _yaml
    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("backend: {}\n")
    src_agent = tmp_path / "src_agent.md"
    src_agent.write_text("# swarm agent prompt\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config(
        str(host_cfg), str(sandbox),
        {"agentmd_path": str(src_agent)},
    )
    dst_agent = sandbox / ".gc" / "agent.md"
    assert dst_agent.exists()
    assert dst_agent.read_text() == "# swarm agent prompt\n"
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())
    assert written["paths"]["agent_md_path"] == "/work/.gc/agent.md"


def test_materialize_missing_host_config_uses_empty_base(tmp_path):
    """Если host config не найден — продолжаем с пустым base + overrides."""
    import yaml as _yaml
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config("/no/such/file", str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())
    # Только overrides
    assert written["paths"]["threads_data_path"] == "/work/.gc/threads"


def test_start_swarm_creates_runtime_config_in_sandbox(base_swarm_cfg, mock_popen, tmp_path):
    """start_swarm_process должен вызвать materialize → файл существует."""
    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("backend: {api_mode: chat_completions}\n")
    base_swarm_cfg["host_config_path"] = str(host_cfg)

    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    sandbox_dir = os.path.join(base_swarm_cfg["sandbox_root_dir"], "g_t")
    runtime_cfg = os.path.join(sandbox_dir, ".gc", "config.yml")
    assert os.path.exists(runtime_cfg)


def test_start_swarm_materialize_failure_does_not_block(
    base_swarm_cfg, mock_popen, monkeypatch
):
    """Если материализация падает — sworm всё равно стартует (graceful)."""
    monkeypatch.setattr(
        runner, "_materialize_runtime_config",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    base_swarm_cfg["host_config_path"] = "/tmp/whatever.yml"

    # Не должно бросить — runner ловит исключение и логирует.
    sid, pid = runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    assert sid == "g_t"
    assert pid == 12345


# ---------------------------------------------------------------------------
# M4 / шаг 1.11 — extended mounts env vars
# ---------------------------------------------------------------------------


def test_start_swarm_passes_cli_venv_root_env(base_swarm_cfg, mock_popen):
    base_swarm_cfg["cli_venv_root"] = "/opt/gc-cli/.venv"
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs["env"]
    assert env["SWARM_CLI_VENV_ROOT"] == "/opt/gc-cli/.venv"


def test_start_swarm_passes_agent_dep_dir_env(base_swarm_cfg, mock_popen):
    base_swarm_cfg["agent_dep_dir"] = "/opt/gc-cli/mcp-deps"
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs["env"]
    assert env["SWARM_AGENT_DEP_DIR"] == "/opt/gc-cli/mcp-deps"


def test_start_swarm_passes_extra_mounts_newline_joined(base_swarm_cfg, mock_popen):
    base_swarm_cfg["extra_mounts"] = [
        "/root/.cache/ms-playwright",
        "/root/.npm",
        "",  # пустые отфильтровываются
    ]
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs["env"]
    lines = env["SWARM_EXTRA_MOUNTS"].split("\n")
    assert lines == ["/root/.cache/ms-playwright", "/root/.npm"]


def test_start_swarm_extended_mount_envs_empty_when_missing(base_swarm_cfg, mock_popen):
    runner.start_swarm_process(
        base_swarm_cfg, system="g", task_id="t", payload={},
    )
    args, kwargs = mock_popen.call_args
    env = kwargs["env"]
    assert env["SWARM_CLI_VENV_ROOT"] == ""
    assert env["SWARM_AGENT_DEP_DIR"] == ""
    assert env["SWARM_EXTRA_MOUNTS"] == ""


# ---------------------------------------------------------------------------
# Layered config merge (унификация со server-side load_config)
# ---------------------------------------------------------------------------


def test_materialize_applies_project_local_overlay(tmp_path, monkeypatch):
    """<cwd>/.gc/config.yml должен мерджиться поверх base, как в обычном CLI."""
    import yaml as _yaml

    # base — host config где-то отдельно
    host_cfg = tmp_path / "host_cfg" / "host.yml"
    host_cfg.parent.mkdir(parents=True)
    host_cfg.write_text(
        "agent:\n  max_steps: 100\n  model: base_model\n"
    )

    # project local overlay в новой "cwd"
    cwd = tmp_path / "project"
    (cwd / ".gc").mkdir(parents=True)
    (cwd / ".gc" / "config.yml").write_text(
        "agent:\n  max_steps: 500\nmcp:\n  servers:\n    project_db: {transport: stdio}\n"
    )
    monkeypatch.chdir(cwd)

    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())

    # max_steps перезаписан project overlay'ем
    assert written["agent"]["max_steps"] == 500
    # model сохранён из base (overlay его не трогает)
    assert written["agent"]["model"] == "base_model"
    # MCP сервер из project overlay'я долетел
    assert "project_db" in written["mcp"]["servers"]
    # И swarm-specific override use_server_subagents=False всё равно применился
    assert written["agent"]["use_server_subagents"] is False


def test_materialize_project_overlay_skipped_when_same_file(tmp_path, monkeypatch):
    """Если host_config_path == <cwd>/.gc/config.yml — самомержа не должно быть."""
    import yaml as _yaml

    cwd = tmp_path / "proj"
    (cwd / ".gc").mkdir(parents=True)
    host_cfg = cwd / ".gc" / "config.yml"
    host_cfg.write_text("agent:\n  max_steps: 42\n")
    monkeypatch.chdir(cwd)

    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())
    # max_steps должно остаться единственным значением, не дублироваться через merge.
    assert written["agent"]["max_steps"] == 42


def test_materialize_applies_swarm_env_paths(tmp_path, monkeypatch):
    """SWARM_AGENTMD_PATH / SWARM_THREADS_DIR / SWARM_SKILLS_DIR оверрайдят paths.*"""
    import yaml as _yaml
    from config_modules import env_vars as _ev

    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text(
        "paths:\n  agent_md_path: /base/agent.md\n  skills_data_path: /base/skills\n"
    )

    # Перевзять env_vars singleton с новыми значениями.
    monkeypatch.setenv("SWARM_AGENTMD_PATH", "/swarm/agent.md")
    monkeypatch.setenv("SWARM_SKILLS_DIR", "/swarm/skills")
    monkeypatch.setattr(_ev, "env", _ev.EnvVars())

    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    runner._materialize_runtime_config(str(host_cfg), str(sandbox), {})
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())

    assert written["paths"]["agent_md_path"] == "/swarm/agent.md"
    assert written["paths"]["skills_data_path"] == "/swarm/skills"


def test_materialize_applies_agent_overrides_flat(tmp_path):
    """Flat agent_overrides (legacy webhook-формат) → авто-обёртка в agent: ."""
    import yaml as _yaml

    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text(
        "agent:\n  max_steps: 100\n  stop_tools: []\n"
    )
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    swarm_cfg = {
        "agent_overrides": {
            "stop_tools": ["write_to_file", "execute_command"],
            "max_steps": 50,
        },
    }
    runner._materialize_runtime_config(str(host_cfg), str(sandbox), swarm_cfg)
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())

    # Flat overrides осели под agent: благодаря auto-wrap.
    # stop_tools — список → deep_merge конкатенирует + dedup; "write_to_file"
    # и "execute_command" должны присутствовать.
    assert "write_to_file" in written["agent"]["stop_tools"]
    assert "execute_command" in written["agent"]["stop_tools"]
    # max_steps скаляр → перезаписан overrides'ом.
    assert written["agent"]["max_steps"] == 50


def test_materialize_applies_agent_overrides_raw(tmp_path):
    """Raw agent_overrides (с секциями agent: / paths: ) применяется как есть."""
    import yaml as _yaml

    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("agent:\n  max_steps: 100\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    swarm_cfg = {
        "agent_overrides": {
            "agent": {"max_steps": 999, "verbose": True},
            "backend": {"api_mode": "responses"},
        },
    }
    runner._materialize_runtime_config(str(host_cfg), str(sandbox), swarm_cfg)
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())

    assert written["agent"]["max_steps"] == 999
    assert written["agent"]["verbose"] is True
    assert written["backend"]["api_mode"] == "responses"


def test_materialize_swarm_specific_overrides_win_over_agent_overrides(tmp_path):
    """Swarm-specific override (paths.threads_data_path=/work/...) должен побеждать
    даже если пользователь пытается переопределить его в agent_overrides."""
    import yaml as _yaml

    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("paths:\n  threads_data_path: /host/threads\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    swarm_cfg = {
        # Пользователь злонамеренно/случайно пытается сбить sandbox path
        "agent_overrides": {
            "paths": {"threads_data_path": "/etc/secrets"},
        },
    }
    runner._materialize_runtime_config(str(host_cfg), str(sandbox), swarm_cfg)
    written = _yaml.safe_load((sandbox / ".gc" / "config.yml").read_text())

    # Sandbox path всё равно /work/.gc/threads — swarm overrides последний слой.
    assert written["paths"]["threads_data_path"] == "/work/.gc/threads"


def test_materialize_agent_overrides_empty_noop(tmp_path):
    """Пустой / отсутствующий agent_overrides не должен ничего ломать."""
    import yaml as _yaml

    host_cfg = tmp_path / "host.yml"
    host_cfg.write_text("agent:\n  max_steps: 77\n")
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    for swarm_cfg in [{}, {"agent_overrides": {}}, {"agent_overrides": None}]:
        # Каждый раз новый sandbox, чтобы избежать конфликта с предыдущим
        sb = tmp_path / f"sb_{id(swarm_cfg)}"
        sb.mkdir(exist_ok=True)
        runner._materialize_runtime_config(str(host_cfg), str(sb), swarm_cfg)
        written = _yaml.safe_load((sb / ".gc" / "config.yml").read_text())
        assert written["agent"]["max_steps"] == 77
