import sys
import types


def _install_fake_ddgs(monkeypatch, text_result, news_result):
    fake = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, region=None, max_results=10):
            return text_result

        def news(self, query, region=None, max_results=10, timelimit=None):
            return news_result

    fake.DDGS = DDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)


def test_web_and_news_search_glue(monkeypatch):
    _install_fake_ddgs(monkeypatch,
                       text_result=[{"title": "t", "href": "u"}],
                       news_result=[{"title": "n"}])
    sys.modules.pop("ddgs_mcp.server", None)
    from ddgs_mcp import server
    assert server.web_search("python", max_results=3) == [{"title": "t", "href": "u"}]
    assert server.news_search("python", max_results=3) == [{"title": "n"}]
    assert callable(server.main)


def test_default_region_is_ru_ru(monkeypatch):
    """Дефолтный регион — ru-ru (не апстрим-овый cn-zh)."""
    seen = {}

    fake = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, region=None, max_results=10):
            seen["text_region"] = region
            return [{"title": "t"}]

        def news(self, query, region=None, max_results=10, timelimit=None):
            seen["news_region"] = region
            return [{"title": "n"}]

    fake.DDGS = DDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    sys.modules.pop("ddgs_mcp.server", None)
    from ddgs_mcp import server

    server.web_search("python")
    server.news_search("python")
    assert seen["text_region"] == "ru-ru"
    assert seen["news_region"] == "ru-ru"


def test_explicit_region_override(monkeypatch):
    """Явно переданный region перебивает дефолт."""
    seen = {}

    fake = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, region=None, max_results=10):
            seen["text_region"] = region
            return [{"title": "t"}]

        def news(self, query, region=None, max_results=10, timelimit=None):
            seen["news_region"] = region
            return [{"title": "n"}]

    fake.DDGS = DDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)
    sys.modules.pop("ddgs_mcp.server", None)
    from ddgs_mcp import server

    server.web_search("python", region="us-en")
    server.news_search("python", region="us-en")
    assert seen["text_region"] == "us-en"
    assert seen["news_region"] == "us-en"
