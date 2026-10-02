import pytest
from gc_core.utils.fs_size_limits import (
    DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES,
    DEFAULT_FS_MUTATE_MAX_BYTES,
    DEFAULT_FS_READ_MAX_BYTES,
    check_file_size,
    file_too_large_error,
    fs_find_in_files_max_bytes,
    fs_mutate_max_bytes,
    fs_read_max_bytes,
)
from pydantic import ValidationError


def test_defaults_and_config_keys():
    assert DEFAULT_FS_READ_MAX_BYTES == 8 * 1024 * 1024
    assert DEFAULT_FS_MUTATE_MAX_BYTES == 16 * 1024 * 1024
    assert fs_read_max_bytes(None) == DEFAULT_FS_READ_MAX_BYTES
    assert fs_mutate_max_bytes(None) == DEFAULT_FS_MUTATE_MAX_BYTES
    assert fs_read_max_bytes({"fs_tools": {"max_read_bytes": 100}}) == 100
    assert fs_mutate_max_bytes({"fs_tools": {"max_mutate_bytes": 200}}) == 200
    assert (
        fs_read_max_bytes({"fs_tools": {"max_read_bytes": 0}})
        == DEFAULT_FS_READ_MAX_BYTES
    )
    # Нечисло отсекает схема настроек (строгая конвертация), а не фолбэк.
    with pytest.raises(ValidationError):
        fs_read_max_bytes({"fs_tools": {"max_read_bytes": "x"}})


def test_find_in_files_max_bytes_defaults():
    assert DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES == 16 * 1024 * 1024
    assert (
        fs_find_in_files_max_bytes(None)
        == DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES
    )
    assert (
        fs_find_in_files_max_bytes({})
        == DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES
    )
    assert (
        fs_find_in_files_max_bytes({"fs_tools": {}})
        == DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES
    )


def test_find_in_files_max_bytes_reads_nested_key():
    cfg = {"fs_tools": {"find_in_files": {"max_file_bytes": 100}}}
    assert fs_find_in_files_max_bytes(cfg) == 100


def test_find_in_files_max_bytes_ignores_read_mutate_keys():
    cfg = {
        "fs_tools": {
            "max_read_bytes": 1,
            "max_mutate_bytes": 2,
            "find_in_files": {"max_file_bytes": 100},
        }
    }
    assert fs_find_in_files_max_bytes(cfg) == 100
    assert fs_read_max_bytes(cfg) == 1
    assert fs_mutate_max_bytes(cfg) == 2


def test_find_in_files_max_bytes_invalid_fallbacks():
    assert (
        fs_find_in_files_max_bytes(
            {"fs_tools": {"find_in_files": {"max_file_bytes": 0}}}
        )
        == DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES
    )
    # Нечисло и не-словарь секции отсекает схема настроек.
    with pytest.raises(ValidationError):
        fs_find_in_files_max_bytes(
            {"fs_tools": {"find_in_files": {"max_file_bytes": "x"}}}
        )
    assert (
        fs_find_in_files_max_bytes(
            {"fs_tools": {"find_in_files": {"max_file_bytes": -5}}}
        )
        == DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES
    )
    with pytest.raises(ValidationError):
        fs_find_in_files_max_bytes({"fs_tools": {"find_in_files": 42}})
    # find_in_files present but without max_file_bytes → default
    assert (
        fs_find_in_files_max_bytes({"fs_tools": {"find_in_files": {}}})
        == DEFAULT_FS_FIND_IN_FILES_MAX_FILE_BYTES
    )


def test_check_file_size():
    assert check_file_size(10, 100) is None
    err = check_file_size(101, 100, operation="search_files")
    assert err == file_too_large_error(
        actual_bytes=101, max_bytes=100, operation="search_files"
    )
    assert err["type"] == "file_too_large"
    assert "search_files" in err["message"]
