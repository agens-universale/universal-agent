"""backend.send_tool_strict controls whether `strict` reaches the provider.

Some OpenAI-compatible builds (vllm/sglang) answer 500 on `function.strict`
instead of ignoring the unknown key, so the field has to be switchable — for
both API dialects, since `responses` sends tool specs verbatim.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest
from gc_core.config import load_settings
from gc_core.config_modules.backend import BackendConfig
from gc_core.tools import abuild_tools_data
from gc_core.utils.connect_backend import (
    create_call_data_responses,
)
from gc_testkit.settings import make_settings

_CONFIG = {"agent.stop_tools": [], "backend.openai_strict_tools_schema": False}


def _specs(send_tool_strict: bool) -> list:
    # Сборка набора тулов корутинная: синхронный `build_tools_data` снят вместе
    # с синхронными входами в дискавери — он уводил обход серверов в
    # `run_coro_blocking`. Фикстура синхронная и своего loop не имеет, поэтому
    # поднимаем его здесь одним `asyncio.run`.
    async def _build():
        return await abuild_tools_data(
            make_settings(
                {**_CONFIG, "backend.send_tool_strict": send_tool_strict}
            )
        )

    _, specs, *_ = asyncio.run(_build())
    return specs


@pytest.fixture(scope="module")
def specs_on() -> list:
    return _specs(True)


@pytest.fixture(scope="module")
def specs_off() -> list:
    return _specs(False)


def test_defaults_to_sending_strict():
    assert BackendConfig().send_tool_strict is True


def test_load_settings_reads_backend_key():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write("backend:\n  send_tool_strict: false\n")
        loaded = load_settings(cfg_path)
        assert loaded.settings.backend.send_tool_strict is False


def test_enabled_keeps_strict_in_specs(specs_on):
    assert any("strict" in spec for spec in specs_on)


def test_disabled_keeps_strict_in_canonical_specs(specs_off):
    """Флаг не запекается в канон: strict исходной спеки остаётся в state.tools."""
    assert specs_off, "registry must not be empty"
    assert any("strict" in spec for spec in specs_off)


def test_disabled_keeps_strict_out_of_chat_completions_payload(specs_off):
    from gc_core.utils.connect_backend import create_call_data_completions

    state = SimpleNamespace(
        messages=[],
        default_model="test-model",
        tools=specs_off,
    )
    data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        state,
        settings=make_settings(
            {
                "backend.send_tool_strict": False,
                "backend.openai_strict_tools_schema": False,
            }
        ),
    )

    assert data["tools"]
    assert [
        t["function"]["name"]
        for t in data["tools"]
        if "strict" in t["function"]
    ] == []


def test_disabled_keeps_strict_out_of_responses_payload(specs_off):
    state = SimpleNamespace(
        default_model="test-model",
        tools=specs_off,
        call_metadata={},
        previous_response_id=None,
    )

    call_data = create_call_data_responses(
        [{"role": "user", "content": "hi"}],
        state,
        settings=make_settings({"backend.send_tool_strict": False}),
    )

    assert [t["name"] for t in call_data["tools"] if "strict" in t] == []
