"""Native guard flows consume worker facts without repeating filesystem I/O."""

import pytest
from gc_core.security.guard_probe import (
    GuardProbeRequest,
    evaluate_guard_probe,
)
from gc_core.security.path_policy import enforce_write_guard
from gc_core.security.read_guard import enforce_read_guard
from gc_testkit.settings import fake_ctx, make_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "write"])
async def test_native_path_guard_matches_local_without_local_probe_io(
    tmp_path, monkeypatch, operation
):
    root = str(tmp_path)
    config = {
        "launch_root": root,
        "write_guard": {"enabled": True, "mode": "deny"},
        "read_guard": {"enabled": True, "mode": "deny"},
    }
    path = str(tmp_path / "file.txt")
    ctx = fake_ctx(config=config)
    trace = []

    class Tool:
        _ctx = ctx

        @staticmethod
        def registered_tool_name():
            return "test_tool"

        async def acall(self, path):
            trace.append("body")
            return "ok"

    decorator = (
        enforce_read_guard if operation == "read" else enforce_write_guard
    )
    Tool.acall = decorator(path_arg="path")(Tool.acall)
    assert await Tool().acall(path) == "ok"
    expected_trace = list(trace)
    trace.clear()
    # Evaluate on the worker before banning host access in the core.
    requests = [
        GuardProbeRequest(f"protected_{operation}", "test_tool", path, root),
        GuardProbeRequest(
            f"{operation}_path",
            "test_tool",
            path,
            root,
            "write" if operation == "write" else "",
        ),
    ]
    facts = {r.kind: evaluate_guard_probe(r, settings=ctx) for r in requests}

    async def probe(request):
        return facts[request.kind]

    ctx.guard_probe = probe

    def forbidden(*_args, **_kwargs):
        raise AssertionError("filesystem guard helper ran in core")

    monkeypatch.setattr(
        "gc_core.security.path_policy.check_write_path_allowed", forbidden
    )
    monkeypatch.setattr(
        "gc_core.security.read_guard.check_read_path_allowed", forbidden
    )
    monkeypatch.setattr(
        "gc_core.security.read_guard.protected_paths", forbidden
    )
    monkeypatch.setattr(
        "gc_core.security.protected_paths.protected_decision", forbidden
    )
    assert await Tool().acall(path) == "ok"
    assert trace == expected_trace


def test_probe_rejects_unknown_kind_and_missing_cwd():
    with pytest.raises(ValueError):
        evaluate_guard_probe(
            GuardProbeRequest("unknown", "t", "x", "/"),
            settings=make_settings(),
        )
    with pytest.raises(ValueError):
        evaluate_guard_probe(
            GuardProbeRequest("read_path", "t", "x", ""),
            settings=make_settings(),
        )
