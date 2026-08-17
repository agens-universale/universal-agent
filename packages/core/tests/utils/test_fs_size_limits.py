from gc_core.utils.fs_size_limits import DEFAULT_FS_MUTATE_MAX_BYTES, DEFAULT_FS_READ_MAX_BYTES, check_file_size, file_too_large_error, fs_mutate_max_bytes, fs_read_max_bytes


def test_defaults_and_config_keys():
    assert DEFAULT_FS_READ_MAX_BYTES == 8 * 1024 * 1024
    assert DEFAULT_FS_MUTATE_MAX_BYTES == 16 * 1024 * 1024
    assert fs_read_max_bytes(None) == DEFAULT_FS_READ_MAX_BYTES
    assert fs_mutate_max_bytes(None) == DEFAULT_FS_MUTATE_MAX_BYTES
    assert fs_read_max_bytes({"fs_tools": {"max_read_bytes": 100}}) == 100
    assert fs_mutate_max_bytes({"fs_tools": {"max_mutate_bytes": 200}}) == 200
    assert fs_read_max_bytes({"fs_tools": {"max_read_bytes": 0}}) == DEFAULT_FS_READ_MAX_BYTES
    assert fs_read_max_bytes({"fs_tools": {"max_read_bytes": "x"}}) == DEFAULT_FS_READ_MAX_BYTES


def test_check_file_size():
    assert check_file_size(10, 100) is None
    err = check_file_size(101, 100, operation="search_files")
    assert err == file_too_large_error(
        actual_bytes=101, max_bytes=100, operation="search_files"
    )
    assert err["type"] == "file_too_large"
    assert "search_files" in err["message"]
