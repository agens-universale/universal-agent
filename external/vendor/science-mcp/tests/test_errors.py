from science_mcp.errors import ErrorCategory, ScienceMCPError, format_error


def test_rate_limited_message():
    msg = format_error(ErrorCategory.RATE_LIMITED, source="arxiv")
    assert "rate limit" in msg.lower()
    assert "arxiv" in msg


def test_not_found_message():
    msg = format_error(ErrorCategory.NOT_FOUND, identifier="10.1/x")
    assert "not found" in msg.lower()
    assert "10.1/x" in msg


def test_invalid_argument_message():
    msg = format_error(ErrorCategory.INVALID_ARGUMENT, reason="limit > 25")
    assert "invalid argument" in msg.lower()
    assert "limit > 25" in msg


def test_unauthorized_message():
    msg = format_error(ErrorCategory.UNAUTHORIZED, source="semantic_scholar")
    assert "unauthorized" in msg.lower() or "key" in msg.lower()
    assert "semantic_scholar" in msg


def test_response_too_large_message():
    msg = format_error(ErrorCategory.RESPONSE_TOO_LARGE, size_bytes=120_000, max_bytes=50_000)
    assert "120000" in msg or "120,000" in msg
    assert "50000" in msg or "50,000" in msg


def test_science_mcp_error_carries_category():
    err = ScienceMCPError(ErrorCategory.RATE_LIMITED, source="arxiv", retry_after_sec=90)
    assert err.category == ErrorCategory.RATE_LIMITED
    assert err.context["source"] == "arxiv"
    assert err.context["retry_after_sec"] == 90
    assert "arxiv" in str(err)
