import pytest
from gc_core.thread_store import resolve_threads_dir
from gc_testkit.settings import make_settings


def test_resolve_threads_dir_returns_explicit_absolute(tmp_path):
    explicit = str(tmp_path / "threads")
    assert (
        resolve_threads_dir(
            make_settings({"paths.threads_data_path": explicit})
        )
        == explicit
    )


def test_resolve_threads_dir_relative_raises(tmp_path):
    with pytest.raises(ValueError, match="абсолютным"):
        resolve_threads_dir(
            make_settings({"paths.threads_data_path": "rel/threads"})
        )


def test_resolve_threads_dir_empty_raises(tmp_path):
    with pytest.raises(ValueError, match="threads_data_path"):
        resolve_threads_dir(make_settings())


def test_resolve_threads_dir_none_raises(tmp_path):
    with pytest.raises(ValueError, match="threads_data_path"):
        resolve_threads_dir(make_settings({"paths.threads_data_path": None}))
