import model_picker as mp


def test_parse_config_models():
    payload = {"chat": {"models": [
        {"model": "vllm/Qwen3-Coder-Next-262k", "description": "Qwen3-Coder",
         "plugin_side": {"supports_parallel_tool_calls": True, "context_window_size": 262144}},
        {"model": "CodeChat", "description": "CodeChat(Qwen3-32B)",
         "plugin_side": {"supports_parallel_tool_calls": False, "context_window_size": 131072}},
    ]}}
    out = mp.parse_config_models(payload)
    assert [m.id for m in out] == ["vllm/Qwen3-Coder-Next-262k", "CodeChat"]
    assert out[0].tool_capable is True and out[0].context == 262144
    assert out[1].tool_capable is False


def test_parse_config_models_empty():
    assert mp.parse_config_models({}) == []
    assert mp.parse_config_models({"chat": {}}) == []


def test_parse_models_list_filters_chat():
    payload = {"object": "list", "data": [
        {"id": "CodeChat", "endpoints": ["/chat/completions", "/responses"]},
        {"id": "Gigacode-Inline-13B", "endpoints": ["/completions"]},
        {"id": "no-endpoints-field"},
    ]}
    out = mp.parse_models_list(payload)
    ids = [m.id for m in out]
    assert "CodeChat" in ids
    assert "Gigacode-Inline-13B" not in ids       # только /completions
    assert "no-endpoints-field" in ids            # нет поля endpoints → не фильтруем
    assert all(m.tool_capable is None for m in out)


def test_api_base():
    assert mp.api_base("https://h/api/v2/chat/completions") == "https://h/api/v2"
    assert mp.api_base("https://h/v1/chat/completions") == "https://h/v1"


def test_fetch_prefers_config_then_models_then_none():
    calls = []
    def get_config_ok(url, headers, verify):
        calls.append(url)
        if url.endswith("/config"):
            return {"chat": {"models": [{"model": "M1", "plugin_side": {"supports_parallel_tool_calls": True}}]}}
        raise RuntimeError("no")
    got = mp.fetch_models("https://h/api/v2/chat/completions", "Bearer x", http_get=get_config_ok)
    assert [m.id for m in got] == ["M1"]

    def get_only_models(url, headers, verify):
        if url.endswith("/config"):
            raise RuntimeError("404")
        return {"data": [{"id": "M2", "endpoints": ["/chat/completions"]}]}
    got = mp.fetch_models("https://h/api/v2/chat/completions", None, http_get=get_only_models)
    assert [m.id for m in got] == ["M2"]

    def get_all_fail(url, headers, verify):
        raise RuntimeError("down")
    assert mp.fetch_models("https://h/api/v2/chat/completions", None, http_get=get_all_fail) is None


def test_probe_fetch_dedups_identical_network_errors(monkeypatch):
    err = "<urlopen error [WinError 10061] отказано>"
    monkeypatch.setattr(mp, "_requests_get_ex",
                        lambda url, headers, verify, *, config_proxy=None: mp.HttpReply(
                            status=None, payload=None, network_error=err))
    out = mp.probe_fetch("http://x/v1", None, verify=False)
    assert out.status is None
    assert out.network_error.count(err) == 1        # дедуп: не дважды
