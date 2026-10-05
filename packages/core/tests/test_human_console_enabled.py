from gc_core.utils.logging_module import human_console_enabled
from gc_testkit.settings import make_settings


def test_text_format_with_console_trace_prints():
    assert (
        human_console_enabled(
            make_settings(
                {"logging.console_trace": True, "logging.format": "text"}
            )
        )
        is True
    )


def test_jsonl_format_suppresses_human_console():
    assert (
        human_console_enabled(
            make_settings(
                {"logging.console_trace": True, "logging.format": "jsonl"}
            )
        )
        is False
    )


def test_console_trace_off_never_prints():
    assert (
        human_console_enabled(
            make_settings(
                {"logging.console_trace": False, "logging.format": "text"}
            )
        )
        is False
    )
    assert (
        human_console_enabled(
            make_settings(
                {"logging.console_trace": False, "logging.format": "jsonl"}
            )
        )
        is False
    )


def test_defaults_print_like_before():
    # console_trace defaults True, format defaults text → human console on
    assert human_console_enabled(make_settings({})) is True


def test_jsonl_case_insensitive():
    assert (
        human_console_enabled(
            make_settings(
                {"logging.console_trace": True, "logging.format": "JSONL"}
            )
        )
        is False
    )
