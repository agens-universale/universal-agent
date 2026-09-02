"""agent.walk_output_max_chars — потолок приватного канала обхода (Harbor) из конфига."""

import asyncio

import yaml

from backends.factory import resolve_os_backend
from backends.protocol import WALK_OUTPUT_MAX_CHARS
from config import load_config
from config_modules.agent import walk_output_max_chars_from_config_dict


def test_walk_ceiling_accessor_defaults_and_parses():
    assert walk_output_max_chars_from_config_dict({}) == WALK_OUTPUT_MAX_CHARS
    assert walk_output_max_chars_from_config_dict({"walk_output_max_chars": "abc"}) == WALK_OUTPUT_MAX_CHARS
    assert walk_output_max_chars_from_config_dict({"walk_output_max_chars": -5}) == WALK_OUTPUT_MAX_CHARS
    assert walk_output_max_chars_from_config_dict({"walk_output_max_chars": 1000}) == 1000


def test_factory_passes_walk_ceiling_to_harbor():
    loop = asyncio.new_event_loop()
    try:
        backend = resolve_os_backend(
            {"os_backend": "harbor", "walk_output_max_chars": 1234},
            environment=object(),
            loop=loop,
        )
        assert backend.walk_output_max_chars == 1234
    finally:
        loop.close()


def test_flat_config_carries_walk_ceiling(tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.dump({
            "backend": {"completions_url": "http://localhost/v1/chat/completions"},
            "agent": {"walk_output_max_chars": 777},
        }),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_path))
    assert cfg["walk_output_max_chars"] == 777


def test_demo_config_documents_the_walk_ceiling():
    """demo_config.yml — полный справочник значений: ключ должен быть в нём, а не только в docs."""
    import pathlib

    doc = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "demo_config.yml").read_text(encoding="utf-8"))
    assert doc["agent"]["walk_output_max_chars"] == WALK_OUTPUT_MAX_CHARS
