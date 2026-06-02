import os
import tempfile

from config import load_config
from summarize.config import (
    parse_summary_turn_end_trigger_chars,
    parse_summary_trigger_chars,
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
                "backend:\n  api_mode: chat_completions\n"
                "summary:\n"
                "  chat_completions_summary_trigger_chars: 200000\n"
            )
        cfg = load_config(cfg_path)
        assert (
            cfg["summary_params"]["chat_completions_summary_turn_end_trigger_chars"]
            == 120000
        )
        assert parse_summary_turn_end_trigger_chars(cfg) == 120000
