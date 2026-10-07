import os
import tempfile

from gc_core.config import load_config
from gc_core.summarize.config import (
    parse_summary_trigger_chars,
    parse_summary_trigger_tokens,
    parse_summary_turn_end_trigger_chars,
    parse_summary_turn_end_trigger_tokens,
    summary_mid_turn_enabled,
)


def test_parse_turn_end_reads_normalized_value_only():
    config_dict = {
        "summary_params": {
            "chat_completions_summary_turn_end_trigger_chars": 120000,
        }
    }
    assert parse_summary_turn_end_trigger_chars(config_dict) == 120000


def test_parse_turn_end_without_key_returns_zero():
    assert parse_summary_turn_end_trigger_chars({"summary_params": {}}) == 0
    assert parse_summary_trigger_chars({"summary_params": {}}) == 0


def test_parse_turn_end_explicit_zero_disables():
    config_dict = {
        "summary_params": {
            "chat_completions_summary_turn_end_trigger_chars": 0,
        }
    }
    assert parse_summary_turn_end_trigger_chars(config_dict) == 0


def test_turn_end_default_sixty_percent_via_load_config():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write(
                "backend:\n  api_mode: chat_completions\nsummary:\n  chat_completions_summary_trigger_chars: 200000\n"
            )
        cfg = load_config(cfg_path)
        assert (
            cfg["summary_params"][
                "chat_completions_summary_turn_end_trigger_chars"
            ]
            == 120000
        )
        assert parse_summary_turn_end_trigger_chars(cfg) == 120000


def test_parse_trigger_tokens_broken_value_is_zero():
    assert parse_summary_trigger_tokens({"summary_params": {}}) == 0
    assert (
        parse_summary_trigger_tokens(
            {
                "summary_params": {
                    "chat_completions_summary_trigger_tokens": "nope"
                }
            }
        )
        == 0
    )
    assert (
        parse_summary_turn_end_trigger_tokens(
            {
                "summary_params": {
                    "chat_completions_summary_turn_end_trigger_tokens": None
                }
            }
        )
        == 0
    )


def test_parse_turn_end_tokens_explicit_zero_disables():
    config_dict = {
        "summary_params": {
            "chat_completions_summary_turn_end_trigger_tokens": 0,
        }
    }
    assert parse_summary_turn_end_trigger_tokens(config_dict) == 0


def test_turn_end_tokens_default_sixty_percent_via_load_config():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write(
                "backend:\n  api_mode: chat_completions\n"
                "summary:\n"
                "  chat_completions_summary_trigger_tokens: 100000\n"
            )
        cfg = load_config(cfg_path)
        assert (
            cfg["summary_params"]["chat_completions_summary_trigger_tokens"]
            == 100000
        )
        assert (
            cfg["summary_params"][
                "chat_completions_summary_turn_end_trigger_tokens"
            ]
            == 60000
        )
        assert parse_summary_turn_end_trigger_tokens(cfg) == 60000
        assert (
            cfg["summary_params"]["chat_completions_summary_trigger_chars"]
            == 0
        )


def test_mid_turn_enabled_when_either_threshold_positive():
    assert summary_mid_turn_enabled({"summary_params": {}}) is False
    assert (
        summary_mid_turn_enabled(
            {"summary_params": {"chat_completions_summary_trigger_chars": 10}}
        )
        is True
    )
    assert (
        summary_mid_turn_enabled(
            {"summary_params": {"chat_completions_summary_trigger_tokens": 10}}
        )
        is True
    )


def _load_summary(summary_yaml: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write(
                "backend:\n  api_mode: chat_completions\nsummary:\n"
                + summary_yaml
            )
        return load_config(cfg_path)["summary_params"]


def test_batch_from_chars_threshold_when_set():
    params = _load_summary(
        "  chat_completions_summary_trigger_chars: 200000\n"
        "  chat_completions_summary_trigger_tokens: 50000\n"
    )
    assert params["incremental_batch_chars"] == 240000


def test_batch_from_tokens_threshold_when_chars_off():
    # Два символа на токен — тот же курс, что и в оценке хвоста; ×1.2 — как у символов.
    params = _load_summary(
        "  chat_completions_summary_trigger_chars: 0\n"
        "  chat_completions_summary_trigger_tokens: 100000\n"
    )
    assert params["incremental_batch_chars"] == 240000


def test_explicit_batch_wins_over_tokens_threshold():
    params = _load_summary(
        "  chat_completions_summary_trigger_chars: 0\n"
        "  chat_completions_summary_trigger_tokens: 100000\n"
        "  incremental_batch_chars: 50000\n"
    )
    assert params["incremental_batch_chars"] == 50000


def test_batch_zero_when_both_thresholds_off():
    assert (
        _load_summary("  chat_completions_summary_trigger_chars: 0\n")[
            "incremental_batch_chars"
        ]
        == 0
    )
