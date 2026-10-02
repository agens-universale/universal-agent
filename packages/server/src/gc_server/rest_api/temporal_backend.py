"""Load the optional execution backend only when selected in server config."""

from pathlib import Path


def configure_temporal(app, config):
    server = config.get("server") or {}
    backend = server.get("execution_backend", "local")
    if backend == "local":
        return None
    if backend != "temporal-native":
        raise ValueError("Unknown server.execution_backend")
    from gc_ua_temporalio.http_api import router
    from gc_ua_temporalio.native_provider import NativeWorkerConfig
    from gc_ua_temporalio.native_registry import NativeRunRegistry

    path = server.get("temporal_config")
    if not path:
        raise ValueError("server.temporal_config is required")
    path = Path(path)
    if not path.is_absolute():
        source = config.get("config_path")
        base = Path(source).resolve().parent if source else Path.cwd()
        path = base / path
    registry = NativeRunRegistry(
        NativeWorkerConfig.load(path),
        profile=server.get("temporal_profile", "demo"),
        store_id=server.get("temporal_store"),
    )
    app.include_router(router(registry))
    return registry
