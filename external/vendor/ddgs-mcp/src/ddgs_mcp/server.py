"""Тонкая MCP-обёртка над библиотекой ddgs (web/news search).
requires-python >=3.10 без верхнего потолка — работает на Python 3.14
(в отличие от стороннего ddgs-mcp==0.1.0, пиннутого <3.14)."""
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("ddgs")

# Дефолтный регион поиска. ru-ru выбран вместо апстрим-ового cn-zh, т.к.
# cn-zh у news-бэкендов DDGS возвращает «No results found» для большинства
# запросов; ru-ru стабильно отдаёт русско- и англоязычные новости.
DEFAULT_REGION = "ru-ru"


@mcp.tool()
def web_search(
    query: str,
    region: Annotated[
        str, Field(default=DEFAULT_REGION, description="Region code (e.g., ru-ru, us-en, cn-zh)")
    ] = DEFAULT_REGION,
    max_results: int = 10,
) -> list[dict]:
    """Web search across multiple engines via DuckDuckGo (DDGS)."""
    from ddgs import DDGS

    return DDGS().text(query=query, region=region, max_results=max_results)


@mcp.tool()
def news_search(
    query: str,
    region: Annotated[
        str, Field(default=DEFAULT_REGION, description="Region code (e.g., ru-ru, us-en, cn-zh)")
    ] = DEFAULT_REGION,
    max_results: int = 10,
    timelimit: str | None = None,
) -> list[dict]:
    """News search with optional time filter (d/w/m) via DuckDuckGo (DDGS)."""
    from ddgs import DDGS

    return DDGS().news(query=query, region=region, max_results=max_results, timelimit=timelimit)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
