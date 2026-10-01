"""The actual command judge receives the same host policy in native execution."""

import json

import pytest
from gc_core.security.command_moderation import load_never_touch_paths
from test_native_parity_live import (
    parity_http,  # noqa: F401
    parity_scenarios,  # noqa: F401
)
from test_native_parity_live import (
    test_actual_http_local_native_parity as run_parity_case,
)


@pytest.mark.parametrize("backend", ["file", "sqlite"])
async def test_native_command_judge_keeps_real_protected_paths(
    tmp_path,
    monkeypatch,
    parity_scenarios,  # noqa: F811
    parity_http,  # noqa: F811
    caplog,
    backend,
):
    from parity_harness import assistant_text
    from test_local_runtime_parity import call, finish

    parity_scenarios["command_judge"] = {
        "turns": [
            call(
                "exec_command",
                {"command": "printf command-parity", "cwd": str(tmp_path)},
            ),
            assistant_text(
                json.dumps({"verdict": "safe", "reasons": [], "confidence": 1})
            ),
            finish("Command verified"),
        ]
    }
    await run_parity_case(
        tmp_path,
        monkeypatch,
        parity_scenarios,
        parity_http,
        "command_judge",
        backend,
    )
    requests = parity_http[2]
    assert len(requests) == 3
    judge = requests[1]
    user = next(
        message["content"]
        for message in judge["messages"]
        if message["role"] == "user"
    )
    payload, _ = json.JSONDecoder().raw_decode(user[user.index("{") :])
    assert payload["never_touch_paths"] == load_never_touch_paths(
        str(tmp_path)
    )
    assert payload["never_touch_paths"]
    assert not any(
        "Path.home restricted" in record.getMessage()
        for record in caplog.records
    )
