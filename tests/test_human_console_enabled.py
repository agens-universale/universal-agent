from gc_core.utils.logging_module import human_console_enabled


def test_text_format_with_console_trace_prints():
    assert human_console_enabled({"console_trace": True, "logging_format": "text"}) is True


def test_jsonl_format_suppresses_human_console():
    assert human_console_enabled({"console_trace": True, "logging_format": "jsonl"}) is False


def test_console_trace_off_never_prints():
    assert human_console_enabled({"console_trace": False, "logging_format": "text"}) is False
    assert human_console_enabled({"console_trace": False, "logging_format": "jsonl"}) is False


def test_defaults_print_like_before():
    # console_trace defaults True, format defaults text → human console on
    assert human_console_enabled({}) is True


def test_jsonl_case_insensitive():
    assert human_console_enabled({"console_trace": True, "logging_format": "JSONL"}) is False
