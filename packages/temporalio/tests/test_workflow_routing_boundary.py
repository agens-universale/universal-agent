"""Workflow entry points must use the sequential routing factory.

New workflow modules are checked automatically. Actual runtime and replay
checks complement this rule in test_sandbox_restrictions_live and child_live.
"""

import ast
from pathlib import Path


def _calls(node):
    return [item for item in ast.walk(node) if isinstance(item, ast.Call)]


def _name(node):
    return ast.unparse(node)


def test_workflow_modules_do_not_construct_threaded_routers_or_breakers():
    source = Path(__file__).resolve().parents[1] / "src" / "gc_core_temporalio"
    failures = []
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text())
        workflow = any(
            _name(call.func) == "workflow.defn" for call in _calls(tree)
        )
        if not workflow and path.name != "context.py":
            continue  # Worker activities deliberately keep real thread locks.
        for call in _calls(tree):
            if _name(call.func) in {
                "ModelRouter",
                "CircuitBreaker",
                "CircuitBreaker.restore",
            }:
                failures.append(
                    f"{path.name}:{call.lineno}: {_name(call.func)}"
                )
    assert not failures, (
        "Use ModelRouter.sequential in workflow code: " + ", ".join(failures)
    )
