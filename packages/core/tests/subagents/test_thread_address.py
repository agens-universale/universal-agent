"""Адрес треда прогона — для тех, кто кладёт файлы в тред (картинки)."""

from types import SimpleNamespace

from gc_core.thread_paths import thread_address
from gc_core.tools.subagents.sidecar import (
    bind_thread_identity,
    clear_thread_identity,
)


def test_bound_run_has_a_thread_address():
    ctx = SimpleNamespace()
    bind_thread_identity(ctx, threads_dir="", system_id="cli", thread_id="t1")
    assert thread_address(ctx) == ("cli", "t1")


def test_run_without_thread_has_no_address():
    ctx = SimpleNamespace()
    clear_thread_identity(ctx)
    assert thread_address(ctx) is None
    assert thread_address(SimpleNamespace()) is None
