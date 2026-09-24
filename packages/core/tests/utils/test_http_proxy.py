"""
RED phase — tests for utils/http_proxy.resolve_proxies.
Covers: config / env / empty / override / strip / NO_PROXY bypass on url.
"""


class TestResolveProxies:
    """Resolver: (config_proxy, environ, url) → proxies dict | None."""

    # ------------------------------------------------------------------
    # Config proxy wins over env
    # ------------------------------------------------------------------

    def test_config_proxy_non_empty_wins_over_env(self):
        from gc_core.utils.http_proxy import resolve_proxies

        config_proxy = "socks5h://proxy.example:1080"
        environ = {"HTTPS_PROXY": "http://env-proxy:8080"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies(config_proxy, environ, url)

        assert result is not None
        assert result["http"] == config_proxy
        assert result["https"] == config_proxy

    def test_config_proxy_strips_whitespace(self):
        from gc_core.utils.http_proxy import resolve_proxies

        config_proxy = "  socks5h://proxy.example:1080  \n"
        environ = {}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies(config_proxy, environ, url)

        assert result is not None
        assert result["http"] == "socks5h://proxy.example:1080"
        assert result["https"] == "socks5h://proxy.example:1080"

    # ------------------------------------------------------------------
    # Env fallback when config_proxy is empty / None
    # ------------------------------------------------------------------

    def test_empty_config_falls_back_to_https_proxy(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {"HTTPS_PROXY": "http://env-proxy:8080"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is not None
        assert result["http"] == "http://env-proxy:8080"
        assert result["https"] == "http://env-proxy:8080"

    def test_empty_config_falls_back_to_http_proxy(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {"HTTP_PROXY": "http://env-proxy:8080"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is not None
        assert result["http"] == "http://env-proxy:8080"
        assert result["https"] == "http://env-proxy:8080"

    def test_empty_config_falls_back_to_all_proxy(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {"ALL_PROXY": "socks5://env-proxy:1080"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is not None
        assert result["http"] == "socks5://env-proxy:1080"
        assert result["https"] == "socks5://env-proxy:1080"

    def test_env_priority_https_over_http_over_all(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {
            "HTTPS_PROXY": "http://https-env:8080",
            "HTTP_PROXY": "http://http-env:8080",
            "ALL_PROXY": "http://all-env:1080",
        }
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is not None
        assert result["http"] == "http://https-env:8080"
        assert result["https"] == "http://https-env:8080"

    def test_lowercase_env_falls_back_when_uppercase_absent(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {"https_proxy": "http://lowercase:8080"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is not None
        assert result["http"] == "http://lowercase:8080"
        assert result["https"] == "http://lowercase:8080"

    # ------------------------------------------------------------------
    # Direct — no proxy
    # ------------------------------------------------------------------

    def test_empty_config_and_empty_env_returns_none(self):
        from gc_core.utils.http_proxy import resolve_proxies

        result = resolve_proxies(
            "", {}, "https://api.openai.com/v1/chat/completions"
        )

        assert result is None

    def test_none_config_and_empty_env_returns_none(self):
        from gc_core.utils.http_proxy import resolve_proxies

        result = resolve_proxies(
            None, {}, "https://api.openai.com/v1/chat/completions"
        )

        assert result is None

    # ------------------------------------------------------------------
    # NO_PROXY bypass — и при proxy из config, и при env proxy
    # ------------------------------------------------------------------

    def test_no_proxy_bypasses_config_proxy(self):
        from gc_core.utils.http_proxy import resolve_proxies

        config_proxy = "http://proxy.example:8080"
        environ = {"NO_PROXY": "api.openai.com, localhost"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies(config_proxy, environ, url)

        # should_bypass_proxies с NO_PROXY=... → proxy None / {}
        assert result is None or result == {}

    def test_no_proxy_bypasses_env_proxy(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {
            "HTTPS_PROXY": "http://env-proxy:8080",
            "NO_PROXY": "api.openai.com",
        }
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is None or result == {}

    def test_no_proxy_lowercase(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {
            "HTTPS_PROXY": "http://env-proxy:8080",
            "no_proxy": "api.openai.com",
        }
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is None or result == {}

    def test_no_proxy_not_matching_returns_proxy(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {
            "HTTPS_PROXY": "http://env-proxy:8080",
            "NO_PROXY": "other.example.com",
        }
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is not None
        assert result["https"] == "http://env-proxy:8080"

    def test_no_proxy_wildcard_subdomain(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {
            "HTTPS_PROXY": "http://env-proxy:8080",
            "NO_PROXY": ".openai.com",
        }
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert result is None or result == {}

    # ------------------------------------------------------------------
    # Returns dict structure (not used on Session, but per-request)
    # ------------------------------------------------------------------

    def test_result_is_dict_with_http_and_https_keys(self):
        from gc_core.utils.http_proxy import resolve_proxies

        environ = {"HTTPS_PROXY": "http://env-proxy:8080"}
        url = "https://api.openai.com/v1/chat/completions"

        result = resolve_proxies("", environ, url)

        assert isinstance(result, dict)
        assert "http" in result
        assert "https" in result
