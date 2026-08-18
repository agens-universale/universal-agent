from office_mcp.errors import ErrorCategory, format_error


def test_file_not_found_message():
    msg = format_error(ErrorCategory.FILE_NOT_FOUND, path="/x/y.doc")
    assert msg == "Error: file not found: /x/y.doc. Check that the path is correct."


def test_path_not_allowed_message():
    msg = format_error(
        ErrorCategory.PATH_NOT_ALLOWED,
        path="/etc/passwd",
        allowed_dirs=["/workspace", "/data"],
    )
    assert "path not allowed: /etc/passwd" in msg
    assert "/workspace" in msg and "/data" in msg


def test_unsupported_format_message():
    msg = format_error(ErrorCategory.UNSUPPORTED_FORMAT, ext=".rtf")
    assert "unsupported format '.rtf'" in msg
    assert ".pdf" in msg and ".docx" in msg
    assert ".doc," not in msg  # legacy .doc is no longer listed as supported


def test_file_too_large_message():
    msg = format_error(ErrorCategory.FILE_TOO_LARGE, size_mb=120.5, max_mb=50)
    assert "file too large: 120.5MB" in msg
    assert "max 50MB" in msg


def test_parse_failed_message():
    msg = format_error(ErrorCategory.PARSE_FAILED, path="/x.doc", fmt=".doc")
    assert "failed to parse /x.doc as .doc" in msg
    assert "corrupted" in msg


def test_timeout_message():
    msg = format_error(ErrorCategory.TIMEOUT, timeout_s=60)
    assert "timed out after 60s" in msg


def test_invalid_base64_message():
    msg = format_error(ErrorCategory.INVALID_BASE64)
    assert "not valid base64" in msg


def test_permission_denied_message():
    msg = format_error(ErrorCategory.PERMISSION_DENIED, path="/x/y.doc")
    assert "permission denied" in msg
    assert "/x/y.doc" in msg


def test_unsupported_format_legacy_doc_includes_conversion_hint():
    msg = format_error(ErrorCategory.UNSUPPORTED_FORMAT, ext=".doc")
    assert "unsupported format '.doc'" in msg
    assert ".docx" in msg
    assert "Convert" in msg


def test_unsupported_format_legacy_rtf_includes_conversion_hint():
    msg = format_error(ErrorCategory.UNSUPPORTED_FORMAT, ext=".rtf")
    assert "Convert" in msg


def test_unsupported_format_modern_unknown_no_hint():
    msg = format_error(ErrorCategory.UNSUPPORTED_FORMAT, ext=".xyz")
    assert "Convert" not in msg


def test_column_not_found_message():
    msg = format_error(ErrorCategory.COLUMN_NOT_FOUND, column="status", available=["id", "name"])
    assert "column not found: 'status'" in msg
    assert "id" in msg and "name" in msg


def test_unsupported_aggregation_message():
    msg = format_error(
        ErrorCategory.UNSUPPORTED_AGGREGATION,
        func="rank",
        supported=["sum", "mean", "count"],
    )
    assert "unsupported aggregation: 'rank'" in msg
    assert "sum" in msg


def test_incompatible_dtype_message():
    msg = format_error(
        ErrorCategory.INCOMPATIBLE_DTYPE,
        column="name",
        dtype="text",
        operation="sum",
    )
    assert "name" in msg
    assert "text" in msg
    assert "sum" in msg
