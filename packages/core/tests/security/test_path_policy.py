import json
from types import SimpleNamespace

import pytest
from gc_core.security.path_policy import (
    WriteGuardViolation,
    audit_policy_decision,
    check_write_path_allowed,
    enforce_write_guard,
    is_within_root,
    normalize_path_for_policy,
)


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)


def test_normalize_path_resolves_relative_segments(tmp_path):
    base = tmp_path / "project"
    base.mkdir()

    resolved = normalize_path_for_policy("src/../README.md", str(base))

    assert resolved == base / "README.md"


def test_check_write_path_allows_launch_root_and_extra_roots(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    extra_root = tmp_path / "cache"
    extra_root.mkdir()

    inside = check_write_path_allowed(
        str(launch_root / "src" / "new.py"),
        str(launch_root),
    )
    extra = check_write_path_allowed(
        str(extra_root / "artifact.txt"),
        str(launch_root),
        [str(extra_root)],
    )

    assert inside == launch_root / "src" / "new.py"
    assert extra == extra_root / "artifact.txt"


def test_check_write_path_blocks_outside_root(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    with pytest.raises(WriteGuardViolation) as exc:
        check_write_path_allowed(
            str(tmp_path / "outside.txt"), str(launch_root)
        )

    assert exc.value.error["type"] == "write_guard_violation"
    assert exc.value.error["launch_root"] == str(launch_root)


def test_check_write_path_blocks_symlink_escape(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = launch_root / "linked"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(WriteGuardViolation):
        check_write_path_allowed(str(link / "data.txt"), str(launch_root))


def test_is_within_root_supports_case_insensitive_comparison():
    assert is_within_root(
        "/PROJECT/src/file.py", "/project", case_insensitive=True
    )
    assert not is_within_root(
        "/PROJECT/src/file.py", "/project", case_insensitive=False
    )


def test_enforce_write_guard_returns_structured_error(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    config_dict = {
        "launch_root": str(launch_root),
        "write_guard": {"enabled": True},
    }

    @enforce_write_guard(path_arg="path", operation="write")
    def write(path: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "path": path})

    result = json.loads(
        write(str(tmp_path / "outside.txt"), config_dict=config_dict)
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "write_guard_violation"


def test_enforce_write_guard_audits_blocked_decision(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    logger = _Logger()
    ctx = SimpleNamespace(
        config_dict={
            "launch_root": str(launch_root),
            "write_guard": {"enabled": True},
        },
        logger=logger,
    )

    @enforce_write_guard(path_arg="path", operation="write")
    def write(path: str, **kwargs) -> str:
        return json.dumps({"ok": True, "path": path})

    result = json.loads(write(str(tmp_path / "outside.txt"), ctx=ctx))

    assert result["ok"] is False
    assert '"decision": "blocked"' in logger.messages[-1]
    assert '"reason_code": "policy_deny"' in logger.messages[-1]
    assert '"error_type": "write_guard_violation"' in logger.messages[-1]


def test_enforce_write_guard_uses_explicit_logger_when_provided(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    logger = _Logger()

    @enforce_write_guard(path_arg="path", operation="write")
    def write(path: str, **kwargs) -> str:
        return json.dumps({"ok": True, "path": path})

    result = json.loads(
        write(
            str(tmp_path / "outside.txt"),
            logger=logger,
            config_dict={
                "launch_root": str(launch_root),
                "write_guard": {"enabled": True},
            },
        )
    )

    assert result["ok"] is False
    assert '"decision": "blocked"' in logger.messages[-1]
    assert '"reason_code": "policy_deny"' in logger.messages[-1]


def test_enforce_write_guard_prefers_explicit_logger_over_context_logger(
    tmp_path,
):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    explicit_logger = _Logger()
    context_logger = _Logger()
    ctx = SimpleNamespace(
        config_dict={
            "launch_root": str(launch_root),
            "write_guard": {"enabled": True},
        },
        logger=context_logger,
    )

    @enforce_write_guard(path_arg="path", operation="write")
    def write(path: str, **kwargs) -> str:
        return json.dumps({"ok": True, "path": path})

    result = json.loads(
        write(
            str(tmp_path / "outside.txt"),
            ctx=ctx,
            logger=explicit_logger,
        )
    )

    assert result["ok"] is False
    assert '"decision": "blocked"' in explicit_logger.messages[-1]
    assert context_logger.messages == []


def test_audit_policy_decision_absent_trace_adds_no_key():
    logger = _Logger()
    context = SimpleNamespace(
        logger=logger, ctx=SimpleNamespace(config_dict={})
    )

    audit_policy_decision(
        context,
        tool_name="exec_command",
        decision="allow",
        reason_code="ok",
    )

    assert '"trace_id"' not in logger.messages[-1]


def test_audit_policy_decision_trace_is_inside_the_payload():
    logger = _Logger()
    context = SimpleNamespace(
        logger=logger,
        ctx=SimpleNamespace(config_dict={"trace_id": "wf-42"}),
    )

    audit_policy_decision(
        context,
        tool_name="exec_command",
        decision="deny",
        reason_code="blocked",
    )

    assert '"trace_id": "wf-42"' in logger.messages[-1]
    assert '"decision": "deny"' in logger.messages[-1]
