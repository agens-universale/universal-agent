"""Tests for litellm/run.py: env loading, callback import sanity, command build."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "litellm"))

import run as run_mod  # type: ignore[import-not-found]


def test_load_env_file_parses_export_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "export GIGACHAT_CREDENTIALS=abc123\n"
        "\n"
        "export YANDEX_API_KEY=key-456\n"
    )
    loaded = run_mod.load_env_file(env)
    assert loaded == {
        "GIGACHAT_CREDENTIALS": "abc123",
        "YANDEX_API_KEY": "key-456",
    }


def test_load_env_file_returns_empty_when_missing(tmp_path: Path) -> None:
    assert run_mod.load_env_file(tmp_path / "missing.env") == {}


def test_load_env_file_handles_quoted_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('export X="hello world"\n')
    assert run_mod.load_env_file(env) == {"X": "hello world"}


def test_build_command_uses_litellm_cli() -> None:
    cmd = run_mod.build_command(port=4000, cli="/fake/litellm")
    assert cmd[0] == "/fake/litellm"
    assert "--config" in cmd
    assert "proxy_config.yaml" in cmd
    assert "--port" in cmd
    assert "4000" in cmd


def test_build_command_uses_given_port() -> None:
    cmd = run_mod.build_command(port=9999, cli="/fake/litellm")
    assert "9999" in cmd


def test_check_callback_importable_true_for_real_layout() -> None:
    real_litellm = Path(__file__).resolve().parents[2] / "litellm"
    assert run_mod.check_callback_importable(real_litellm) is True


def test_check_callback_importable_false_for_empty_dir(tmp_path: Path) -> None:
    assert run_mod.check_callback_importable(tmp_path) is False
