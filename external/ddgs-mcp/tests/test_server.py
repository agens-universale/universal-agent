import sys
import types


def _install_fake_ddgs(monkeypatch, text_result, news_result):
    fake = types.ModuleType("ddgs")

    class DDGS:
        def text(self, query, max_results=10):
            return text_result

        def news(self, query, max_results=10, timelimit=None):
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
