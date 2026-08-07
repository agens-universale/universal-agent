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


def test_api_base_cuts_audio_methods_too():
    """STT-endpoint голосового ввода — тот же OpenAI-совместимый корень: без стрижки
    суффикса список моделей уходил бы в `…/audio/transcriptions/models`."""
    assert mp.api_base("https://h/v1/audio/transcriptions") == "https://h/v1"
    assert mp.api_base("https://h/v1/audio/translations") == "https://h/v1"
    assert mp.api_base("http://127.0.0.1:8735/v1/audio/transcriptions/") == "http://127.0.0.1:8735/v1"
    # чужой корень не трогаем (адрес без известного суффикса — уже база)
    assert mp.api_base("https://h/v1") == "https://h/v1"
    assert mp.api_base("https://h/v1/audio") == "https://h/v1/audio"


def test_fetch_models_from_an_stt_endpoint_hits_the_v1_root():
    """Список моделей своего STT-endpoint: запрос уходит в корень `/v1`, а не под
    `/audio/transcriptions` (сервер speaches отдаёт whisper-модели именно там)."""
    seen = []

    def _get(url, headers, verify):
        seen.append(url)
        if url.endswith("/config"):
            raise RuntimeError("404")
        return {"data": [{"id": "Systran/faster-whisper-small"}]}

    got = mp.fetch_models("http://127.0.0.1:8735/v1/audio/transcriptions", None, http_get=_get)
    assert [m.id for m in got] == ["Systran/faster-whisper-small"]
    assert seen == ["http://127.0.0.1:8735/v1/config", "http://127.0.0.1:8735/v1/models"]


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


class _FakeResp:
    status = 200

    def read(self):
        return b'{"data": []}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_user_agents(monkeypatch) -> list:
    seen = []

    def _urlopen(req, timeout=None, context=None):
        seen.append(req.get_header("User-agent"))
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return seen


def test_requests_introduce_themselves_by_product_name(monkeypatch):
    """Каждый исходящий запрос несёт визитку продукта. Без неё Python подставляет
    `Python-urllib/X.Y`, и щит провайдера (Cloudflare у Groq — проверено живьём)
    отвечает 403 ещё ДО проверки токена: список моделей молчал бы на рабочем ключе."""
    import app_version
    seen = _capture_user_agents(monkeypatch)

    mp._urllib_get("https://h/v1/models", {"Content-Type": "application/json"}, True)
    mp._urllib_get_ex("https://h/v1/models", {}, True)
    mp._urllib_post_ex("https://h/v1/chat/completions", {}, {"model": "m"}, True)

    expected = f"{app_version.DIST_NAME}/{app_version.agent_version()}"
    assert seen == [expected] * 3
    assert seen[0].startswith("gc-ua/") and "urllib" not in seen[0]


def test_the_user_agent_carries_the_live_version(monkeypatch):
    """Имя и версия — из метаданных пакета, не из строки в коде: подняли версию в
    pyproject — визитка поехала сама."""
    import app_version
    monkeypatch.setattr(app_version, "agent_version", lambda: "9.9.9")
    mp._user_agent.cache_clear()
    try:
        seen = _capture_user_agents(monkeypatch)
        mp._urllib_get("https://h/v1/models", {}, True)
        assert seen == ["gc-ua/9.9.9"]
    finally:
        mp._user_agent.cache_clear()
