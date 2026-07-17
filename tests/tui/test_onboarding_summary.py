from tui.services.onboarding_summary import summary_line


def test_skeleton_all_empty():
    line = summary_line(url="", auth=None, model="", mcp_count=0)
    assert line == "сервер —   ·   токен —   ·   модель —   ·   MCP —"


def test_url_shortened_to_host_port():
    line = summary_line(url="https://46.243.227.147:15000/v1/chat/completions",
                        auth=None, model="", mcp_count=0)
    assert "46.243.227.147:15000" in line
    assert "https://" not in line and "/v1/" not in line


def test_token_masked_never_literal():
    line = summary_line(url="http://h", auth="Bearer sk-SECRET", model="", mcp_count=0)
    assert "sk-SECRET" not in line
    assert "токен Bearer" in line


def test_no_auth_and_mcp_count():
    line = summary_line(url="http://h", auth="", model="m", mcp_count=2)
    assert "токен нет" in line
    assert "MCP 2" in line


def test_long_values_truncated_single_line():
    line = summary_line(url="http://" + "a" * 80 + ":15000/v1", auth=None,
                        model="x" * 80, mcp_count=0)
    assert "\n" not in line
    assert "…" in line
