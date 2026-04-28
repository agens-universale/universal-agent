from gc_computer_mcp.errors import ComputerMCPError, ErrorCategory


def test_category_values_are_snake_case():
    for cat in ErrorCategory:
        assert cat.value == cat.value.lower()
        assert cat.value.replace("_", "").isalpha()


def test_exception_message_prefixed_with_category():
    err = ComputerMCPError(ErrorCategory.ELEMENT_NOT_FOUND, "element_id 3 stale")
    assert str(err) == "[element_not_found] element_id 3 stale"


def test_exception_keeps_category_as_attribute():
    err = ComputerMCPError(ErrorCategory.TIMEOUT, "took too long")
    assert err.category == ErrorCategory.TIMEOUT


def test_all_required_categories_exist():
    required = {
        "ELEMENT_NOT_FOUND", "STALE_STATE", "READONLY_VIOLATION",
        "BLACKLISTED_KEYS", "OUT_OF_BOUNDS", "TIMEOUT",
        "UIA_UNAVAILABLE", "APP_NOT_FOUND", "INVALID_INPUT",
        "DPI_ERROR", "UNEXPECTED",
    }
    assert required.issubset({c.name for c in ErrorCategory})
