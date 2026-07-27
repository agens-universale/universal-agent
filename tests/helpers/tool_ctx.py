"""Attach a local ExecutionWorkspace to tools in unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional, Type, TypeVar

from config_modules.agent import exec_output_capture_max_chars_from_config_dict
from execution_workspace import build_execution_workspace

T = TypeVar("T")


def local_ctx(config_dict: Optional[dict] = None) -> SimpleNamespace:
    """Minimal RunContext-like object with LocalFsBackend + workspace.

    LocalFsBackend импортируется здесь, а не на уровне модуля: некоторые тесты
    вычищают ``tools.*`` из ``sys.modules`` и переимпортируют пакет — тогда
    module-level ссылка становится чужим классом относительно monkeypatch.
    """
    from backends.local import LocalFsBackend

    cfg = dict(config_dict or {})
    backend = LocalFsBackend(
        exec_output_capture_max_chars=exec_output_capture_max_chars_from_config_dict(cfg)
    )
    return SimpleNamespace(
        config_dict=cfg,
        os_backend=backend,
        workspace=build_execution_workspace(backend, cfg),
    )


def with_local_ctx(tool_cls: Type[T], *, config_dict: Optional[dict] = None, **kwargs: Any) -> T:
    """Instantiate ``tool_cls`` with a local ctx (and optional config_dict)."""
    cfg = config_dict if config_dict is not None else kwargs.get("config_dict")
    ctx_cfg = cfg if isinstance(cfg, dict) else {}
    kw = dict(kwargs)
    if cfg is not None:
        kw["config_dict"] = cfg
    kw.setdefault("ctx", local_ctx(ctx_cfg))
    return tool_cls(**kw)
