"""Тонкая MCP-обёртка над библиотекой ddgs (web/news search).
requires-python >=3.10 без верхнего потолка — работает на Python 3.14
(в отличие от стороннего ddgs-mcp==0.1.0, пиннутого <3.14)."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ddgs")


@mcp.tool()
def web_search(query: str, max_results: int = 10) -> list[dict]:
    """Web search across multiple engines via DuckDuckGo (DDGS)."""
    from ddgs import DDGS
    return DDGS().text(query, max_results=max_results)


@mcp.tool()
def news_search(query: str, max_results: int = 10, timelimit: str | None = None) -> list[dict]:
    """News search with optional time filter (d/w/m) via DuckDuckGo (DDGS)."""
    from ddgs import DDGS
    return DDGS().news(query, max_results=max_results, timelimit=timelimit)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
