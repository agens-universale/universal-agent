from gc_core.utils.directory_write_access import directory_allows_probe_write


def test_probe_true_for_writable_dir(tmp_path):
    assert directory_allows_probe_write(str(tmp_path)) is True


def test_probe_false_for_missing_path(tmp_path):
    missing = tmp_path / "nope_nope_nope"
    assert directory_allows_probe_write(str(missing)) is False


def test_probe_false_for_file_not_dir(tmp_path):
    fp = tmp_path / "only_file"
    fp.write_text("x", encoding="utf-8")
    assert directory_allows_probe_write(str(fp)) is False
