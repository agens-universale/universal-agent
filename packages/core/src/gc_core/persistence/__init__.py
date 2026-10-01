"""Persistence exports, loaded on demand so pure projections do not import I/O."""

from importlib import import_module

_EXPORTS = {
    "STATE_SCHEMA_VERSION": "schema",
    "StateSchemaError": "schema",
    "StateRepository": "state_repository",
    "StateSerializer": "state_serializer",
    "load_state_from_snapshot": "state_serializer",
    "ThreadMetaMapper": "thread_meta_mapper",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(
        import_module(f"gc_core.persistence.{_EXPORTS[name]}"), name
    )
    globals()[name] = value
    return value
