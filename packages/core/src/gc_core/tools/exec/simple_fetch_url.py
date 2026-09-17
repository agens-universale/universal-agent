import functools
import html as html_lib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests

from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.security.url_policy import enforce_url_policy, validate_url_policy, UrlPolicyDecision


class _RedirectBlocked(Exception):
    """Внутреннее исключение для срыва redirect-цикла при policy-блокировке."""
    def __init__(self, decision: UrlPolicyDecision) -> None:
        super().__init__(decision.reason or "redirect blocked")
        self.decision = decision

@loop_warn_on_repeat(**READ_PROFILE)
class SimpleFetchUrl(AbstractSimpleTool):
    timeout_seconds = 20
    max_content_chars = 20_000
    # LRU-лимит кэша извлечённого текста страниц (на тред/сессию).
    # Кэш хранится в SharedStateData (per-thread, персистится в meta),
    # поэтому лимит ограничивает и RAM, и размер JSON-meta треда.
    # Должен совпадать с FetchUrl.fetch_url_cache_max_size — оба делят
    # shared.fetch_url_cache.
    fetch_url_cache_max_size = 10
    max_retries = 2
    retry_backoff_seconds = 1.0
    default_user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, ctx: Any = None, state: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return {
            "type": "function",
            "name": "exec_fetch_url",
            "description": (
                "Fetches a URL via HTTP GET and returns readable text extracted from HTML without heavy dependencies. "
                "If the response is binary, returns content='binary data' with is_binary=true. "
                "Stores full extracted text in session in-memory cache by URL and supports paginated reads via offset/limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to fetch (http or https)."},
                    "timeout": {"type": "integer", "description": f"HTTP timeout in seconds. Default {cls.timeout_seconds}."},
                    "max_preview_chars": {"type": "integer", "description": "Maximum number of characters for preview. Default 1200."},
                    "offset": {"type": "integer", "description": "Character offset for paginated reads from cached full content. Default 0."},
                    "limit": {"type": "integer", "description": f"Maximum number of characters to return in content. Default {cls.max_content_chars}."},
                    "proxy": {
                        "type": "string",
                        "description": (
                            "Optional proxy URL for this request "
                            "(http://, https://, socks5://, socks5h://, socks4://). "
                            "Empty string or omit = no proxy."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return (
            "Tool name: exec_fetch_url\n"
            "Tool description: Downloads a URL and extracts readable text using a lightweight HTML cleaner. "
            "Optional proxy (http/https/socks5/socks5h/socks4); empty string or omit = no proxy."
        )

    @staticmethod
    def _result(
        *,
        url: str,
        ok: bool,
        final_url: Optional[str] = None,
        status_code: Optional[int] = None,
        content_type: Optional[str] = None,
        is_binary: bool = False,
        result_type: Optional[str] = None,
        extracted_source: Optional[str] = None,
        content: str = "",
        preview: str = "",
        truncated: bool = False,
        max_content_chars: Optional[int] = None,
        max_preview_chars: Optional[int] = None,
        error: Optional[Dict[str, Any]] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        returned_chars: Optional[int] = None,
        total_chars: Optional[int] = None,
        has_more: Optional[bool] = None,
        cache_hit: Optional[bool] = None,
        meta: Optional[Dict[str, Optional[str]]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": SimpleFetchUrl.registered_tool_name(),
            "ok": ok,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "is_binary": is_binary,
            "result_type": result_type,
            "extracted_source": extracted_source,
            "truncated": truncated,
            "meta": meta if meta is not None else {"title": None, "description": None, "lang": None},
            "error": error,
        }
        if max_content_chars is not None:
            payload["max_content_chars"] = max_content_chars
        if max_preview_chars is not None:
            payload["max_preview_chars"] = max_preview_chars
        if offset is not None:
            payload["offset"] = offset
        if limit is not None:
            payload["limit"] = limit
        if returned_chars is not None:
            payload["returned_chars"] = returned_chars
        if total_chars is not None:
            payload["total_chars"] = total_chars
        if has_more is not None:
            payload["has_more"] = has_more
        if has_more is not None and offset is not None and limit is not None:
            payload["next_offset"] = offset + limit if has_more else None
        if cache_hit is not None:
            payload["cache_hit"] = cache_hit
        payload["content"] = content
        payload["preview"] = preview
        return json.dumps(payload, ensure_ascii=False)

    def _get_cache(self) -> Optional["OrderedDict[str, Any]"]:
        shared = getattr(self._state, "shared", None)
        if shared is None:
            return None
        cache = getattr(shared, "fetch_url_cache", None)
        # Нормализация: из meta кэш восстанавливается как обычный dict,
        # а для LRU нужен OrderedDict. Если тип не совпадает — пересобираем.
        if not isinstance(cache, OrderedDict):
            cache = OrderedDict(cache if isinstance(cache, dict) else {})
            setattr(shared, "fetch_url_cache", cache)
        return cache

    @staticmethod
    def _cache_key(url: str, proxy: Optional[str] = None) -> str:
        """Ключ кэша: URL, при наличии proxy — URL+proxy (разные маршруты не смешиваются)."""
        if proxy:
            return f"{url}||proxy={proxy}"
        return url

    _PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")

    @classmethod
    def _normalize_proxy(cls, proxy: Optional[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Возвращает (normalized_proxy | None, error | None)."""
        if proxy is None:
            return None, None
        if not isinstance(proxy, str):
            return None, {"type": "invalid_argument", "field": "proxy"}
        normalized = proxy.strip()
        if not normalized:
            # Пустая / whitespace-only строка = прокси не задан.
            return None, None
        lower = normalized.lower()
        if not lower.startswith(cls._PROXY_SCHEMES):
            return None, {
                "type": "invalid_argument",
                "field": "proxy",
                "message": (
                    "proxy must start with http://, https://, socks5://, "
                    "socks5h://, socks4://, or socks4a://."
                ),
            }
        return normalized, None

    @staticmethod
    def _proxies_dict(proxy: Optional[str]) -> Optional[Dict[str, str]]:
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def _get_from_cache(
        self, cache_key: str
    ) -> Optional[Tuple[str, Dict[str, Optional[str]]]]:
        """Возвращает (content, meta) для закэшированного ключа или None.

        При cache-hit обновляет LRU-порядок: только что читанный ключ становится
        самым свежим и не вытесняется следующими записями.
        """
        cache = self._get_cache()
        if cache is None or cache_key not in cache:
            return None
        cached = cache[cache_key]
        if isinstance(cached, dict):
            content = cached.get("content", "")
            meta = self._normalize_meta(cached.get("meta"))
        else:
            content = cached if isinstance(cached, str) else ""
            meta = self._normalize_meta(None)
        cache.move_to_end(cache_key)
        return content, meta

    def _put_to_cache(
        self, cache_key: str, content: str, meta: Dict[str, Optional[str]]
    ) -> None:
        """Записывает страницу в кэш и эвиктирует самые старые при превышении лимита."""
        cache = self._get_cache()
        if cache is None:
            return
        cache[cache_key] = {"content": content, "meta": meta}
        cache.move_to_end(cache_key, last=True)
        while len(cache) > self.fetch_url_cache_max_size:
            cache.popitem(last=False)

    @staticmethod
    def _normalize_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
        normalized: Dict[str, Optional[str]] = {"title": None, "description": None, "lang": None}
        if not isinstance(meta, dict):
            return normalized
        for key in ("title", "description", "lang"):
            value = meta.get(key)
            if isinstance(value, str):
                value = value.strip()
                normalized[key] = value or None
        return normalized

    @classmethod
    def _extract_meta(cls, html: str) -> Dict[str, Optional[str]]:
        meta: Dict[str, Optional[str]] = {"title": None, "description": None, "lang": None}
        if not html:
            return meta

        title_match = re.search(r"<title\b[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = cls._strip_tags(title_match.group(1)).strip()
            meta["title"] = title or None

        html_match = re.search(r"<html\b([^>]*)>", html, flags=re.IGNORECASE)
        if html_match:
            attrs = html_match.group(1)
            lang_match = re.search(r"""\blang\s*=\s*(['"])(.*?)\1""", attrs, flags=re.IGNORECASE | re.DOTALL)
            if lang_match:
                lang = html_lib.unescape(re.sub(r"\s+", " ", lang_match.group(2))).strip()
                meta["lang"] = lang or None

        desc_match = re.search(
            r"""<meta\b(?=[^>]*\bname\s*=\s*(['"])description\1)(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>""",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if desc_match:
            description = html_lib.unescape(re.sub(r"\s+", " ", desc_match.group(3))).strip()
            meta["description"] = description or None

        return cls._normalize_meta(meta)

    @staticmethod
    def _is_binary_content_type(content_type: str) -> bool:
        ct = (content_type or "").split(";")[0].strip().lower()
        if not ct:
            return False
        if ct.startswith("text/"):
            return False
        if ct in (
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/javascript",
            "application/x-javascript",
        ):
            return False
        if ct.startswith("image/"):
            return True
        if ct in (
            "application/pdf",
            "application/zip",
            "application/gzip",
            "application/x-gzip",
            "application/x-tar",
            "application/octet-stream",
            "application/x-bzip2",
            "application/x-7z-compressed",
        ):
            return True
        if ct.startswith("application/"):
            return True
        return False

    @staticmethod
    def _is_text_like_content_type(content_type: str) -> bool:
        ct = (content_type or "").split(";")[0].strip().lower()
        if not ct:
            return False
        if ct.startswith("text/"):
            return True
        return ct in (
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/javascript",
            "application/x-javascript",
            "application/x-www-form-urlencoded",
            "application/rss+xml",
            "application/atom+xml",
        )

    @staticmethod
    def _looks_binary_bytes(data: bytes) -> bool:
        if not data:
            return False
        sample = data[:4096]
        if b"\x00" in sample:
            return True
        printable = 0
        for b in sample:
            if b in (9, 10, 13) or 32 <= b <= 126:
                printable += 1
        return (printable / max(len(sample), 1)) < 0.75

    @staticmethod
    def _strip_tags(fragment: str) -> str:
        without_tags = re.sub(r"<[^>]+>", " ", fragment)
        text = html_lib.unescape(without_tags)
        text = re.sub(r"[ \t]{2,}", "  ", text)
        return text.strip()

    @classmethod
    def _html_block_to_text(cls, match: re.Match[str]) -> str:
        inner = cls._strip_tags(match.group(2))
        if not inner:
            return "\n\n"
        return f"\n\n{inner}\n\n"

    @classmethod
    def _html_list_item_to_text(cls, match: re.Match[str]) -> str:
        inner = cls._strip_tags(match.group(1))
        if not inner:
            return "\n"
        return f"\n- {inner}\n"

    @classmethod
    def _html_heading_to_markdown(cls, match: re.Match[str]) -> str:
        tag_name = (match.group(1) or "").lower()
        inner = cls._strip_tags(match.group(2))
        if not inner:
            return "\n\n"
        level = 1
        if len(tag_name) == 2 and tag_name.startswith("h") and tag_name[1].isdigit():
            level = max(1, min(6, int(tag_name[1])))
        return f"\n\n{'#' * level} {inner}\n\n"

    @staticmethod
    def _html_pre_to_text(match: re.Match[str]) -> str:
        inner = match.group(1) or ""
        inner = re.sub(r"(?i)<br\s*/?>", "\n", inner)
        inner = re.sub(r"<[^>]+>", "", inner)
        inner = html_lib.unescape(inner)
        inner = inner.strip("\n")
        if not inner:
            return "\n\n"
        return f"\n\n{inner}\n\n"

    @classmethod
    def _html_table_cell_to_text(cls, match: re.Match[str]) -> str:
        return f" {cls._strip_tags(match.group(2))} |"

    @classmethod
    def _extract_simple_html_text(cls, html: str) -> str:
        content = html or ""
        content = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?>.*?</\1>", " ", content)
        content = re.sub(r"(?i)<br\s*/?>", "\n", content)
        content = re.sub(r"(?i)</tr\s*>", "\n", content)
        content = re.sub(r"(?is)<(p|div)\b[^>]*>(.*?)</\1>", cls._html_block_to_text, content)
        content = re.sub(r"(?is)<(h[1-6])\b[^>]*>(.*?)</\1>", cls._html_heading_to_markdown, content)
        content = re.sub(r"(?is)<li\b[^>]*>(.*?)</li>", cls._html_list_item_to_text, content)
        content = re.sub(r"(?i)</?(ul|ol)\b[^>]*>", "\n", content)
        content = re.sub(r"(?is)<pre\b[^>]*>(.*?)</pre>", cls._html_pre_to_text, content)
        content = re.sub(r"(?is)<(td|th)\b[^>]*>(.*?)</\1>", cls._html_table_cell_to_text, content)
        content = re.sub(r"(?i)</?(table|thead|tbody|tfoot)\b[^>]*>", "\n", content)
        content = cls._strip_tags(content)
        content = re.sub(r"[ \t]*\|[ \t]*(\n|$)", r"\1", content)
        # Normalize whitespace-only lines to real empty lines before collapsing.
        content = re.sub(r"\n[ \t\n]+\n", "\n\n", content)
        content = re.sub(r"(?:\n[ \t]+\n)+", "\n\n", content)
        content = re.sub(r"\r\n?", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = re.sub(r"[ \t]+([,.;:!?…])", r"\1", content)
        content = re.sub(r"[ \t]{2,}", "  ", content)
        return content.strip()

    def _slice_content(self, content: str, offset: int, limit: int) -> Dict[str, Any]:
        total_chars = len(content)
        if offset >= total_chars:
            return {"content": "", "returned_chars": 0, "total_chars": total_chars, "has_more": False}
        chunk = content[offset : offset + limit]
        returned_chars = len(chunk)
        has_more = offset + returned_chars < total_chars
        return {"content": chunk, "returned_chars": returned_chars, "total_chars": total_chars, "has_more": has_more}

    def _url_policy_blocked_result(
        self,
        error: Dict[str, Any],
        url: str,
        **kwargs: Any,
    ) -> str:
        return self._result(
            url=url,
            ok=False,
            result_type="error",
            error=error,
        )

    @enforce_url_policy(url_arg="url")
    def __call__(
        self,
        url: str,
        timeout: Optional[int] = None,
        max_preview_chars: int = 1200,
        offset: int = 0,
        limit: Optional[int] = None,
        proxy: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        if not isinstance(url, str):
            return self._result(ok=False, url=str(url), error={"type": "invalid_argument", "field": "url"})

        _result = functools.partial(self._result, url=url)

        if timeout is not None and not isinstance(timeout, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "timeout"})
        if not isinstance(max_preview_chars, int) or max_preview_chars <= 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_preview_chars"})
        if not isinstance(offset, int) or offset < 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "offset"})
        if limit is None:
            limit = self.max_content_chars
        if not isinstance(limit, int) or limit <= 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "limit"})

        normalized_proxy, proxy_error = self._normalize_proxy(proxy)
        if proxy_error is not None:
            return _result(ok=False, error=proxy_error)

        normalized_url = url.strip()
        if not normalized_url:
            return _result(ok=False, error={"type": "invalid_argument", "field": "url", "message": "URL cannot be empty."})
        if not normalized_url.startswith(("http://", "https://")):
            return _result(ok=False, error={"type": "invalid_argument", "field": "url", "message": "URL must start with http:// or https://."})

        effective_timeout = timeout or self.timeout_seconds
        if effective_timeout <= 0:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "timeout", "message": "timeout must be greater than 0."},
            )

        cache_key = self._cache_key(normalized_url, normalized_proxy)
        proxies = self._proxies_dict(normalized_proxy)

        cached = self._get_from_cache(cache_key)
        if cached is not None:
            cached_content, cached_meta = cached
            sliced = self._slice_content(cached_content, offset, limit)
            return _result(
                ok=True,
                final_url=normalized_url,
                status_code=200,
                content_type="text/plain; charset=utf-8",
                is_binary=False,
                result_type="content",
                extracted_source="cache",
                content=sliced["content"],
                truncated=sliced["has_more"],
                offset=offset,
                limit=limit,
                returned_chars=sliced["returned_chars"],
                total_chars=sliced["total_chars"],
                has_more=sliced["has_more"],
                cache_hit=True,
                meta=cached_meta,
                error=None,
            )

        headers = {
            "User-Agent": self.default_user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        }

        response = None
        attempts = 1 + self.max_retries
        # Извлекаем url_policy для per-hop redirect-валидации.
        # Берём из ToolSecurityContext (тот же путь что и enforce_url_policy).
        _cfg = getattr(self, "_ctx", None)
        _cfg = getattr(_cfg, "config_dict", {}) or {}
        _cm = _cfg.get("command_moderation") or {}
        _up = _cm.get("url_policy") or {}
        _allowed_schemes = list(_up.get("allowed_schemes") or ["http", "https"])
        _url_blacklist = list(_up.get("url_blacklist") or [])
        _max_redirects = 10

        def _fetch_with_redirect_validation(session, start_url, headers, timeout, proxies=None):
            """Manual redirect loop с per-hop validate_url_policy."""
            current = start_url
            for _ in range(_max_redirects + 1):
                get_kwargs: Dict[str, Any] = {
                    "headers": headers,
                    "timeout": timeout,
                    "allow_redirects": False,
                }
                if proxies is not None:
                    get_kwargs["proxies"] = proxies
                resp = session.get(current, **get_kwargs)
                status = getattr(resp, "status_code", 0) or 0
                # 3xx redirect codes. Используем status_code чтобы не зависеть
                # от Response.is_redirect (отсутствует в тест-stubах).
                if not (300 <= status < 400):
                    return resp
                next_url = getattr(resp, "headers", {}).get("Location") if hasattr(resp, "headers") else None
                if not next_url:
                    return resp
                # Абсолютизируем относительный Location.
                next_abs = urljoin(current, next_url)
                # ВАЛИДИРУЕМ КАЖДЫЙ HOP. AS-IS spec говорил «redirect не перепроверяется» —
                # это и было дырой. Теперь перепроверяется.
                decision = validate_url_policy(next_abs, _allowed_schemes, _url_blacklist)
                if not decision.allowed:
                    raise _RedirectBlocked(decision)
                current = next_abs
            raise _RedirectBlocked(
                UrlPolicyDecision(
                    allowed=False, url=current,
                    error_type="too_many_redirects",
                    reason=f"Exceeded {_max_redirects} redirects.",
                )
            )

        with requests.Session() as session:
            for attempt in range(1, attempts + 1):
                try:
                    response = _fetch_with_redirect_validation(
                        session, normalized_url, headers, effective_timeout, proxies=proxies
                    )
                    response.raise_for_status()
                    break
                except _RedirectBlocked as redirect_exc:
                    _decision = redirect_exc.decision
                    return _result(
                        ok=False,
                        final_url=_decision.url,
                        status_code=None,
                        content_type=None,
                        result_type="error",
                        error={
                            "type": _decision.error_type or "url_blocked_by_policy",
                            "reason": _decision.reason,
                            "matched_rule": _decision.matched_rule,
                            "blocked_on_redirect": True,
                        },
                    )
                except requests.RequestException as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    retryable = status is None or status in (429,) or status >= 500
                    if (not retryable) or attempt == attempts:
                        return _result(
                            ok=False,
                            final_url=getattr(getattr(exc, "response", None), "url", None),
                            status_code=status,
                            content_type=getattr(getattr(exc, "response", None), "headers", {}).get("Content-Type")
                            if getattr(exc, "response", None) is not None
                            else None,
                            result_type="error",
                            error={
                                "type": "request_failed",
                                "message": str(exc),
                                "attempt": attempt,
                                "attempts_total": attempts,
                                "retryable": retryable,
                            },
                        )
                    time.sleep(self.retry_backoff_seconds * attempt)

        if response is None:
            return _result(ok=False, result_type="error", error={"type": "request_failed", "message": "Unknown request failure."})

        content_type = response.headers.get("Content-Type", "")
        final_url = response.url
        status_code = response.status_code
        raw_content = getattr(response, "content", b"")
        content_type_binary = self._is_binary_content_type(content_type)
        content_type_text = self._is_text_like_content_type(content_type)
        is_binary = content_type_binary or (not content_type_text and self._looks_binary_bytes(raw_content))
        if is_binary:
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=True,
                result_type="binary",
                content="binary data",
                offset=offset,
                limit=limit,
                returned_chars=0,
                total_chars=0,
                has_more=False,
                cache_hit=False,
                error=None,
            )

        text = response.text or ""
        page_meta = self._extract_meta(text)
        extracted = self._extract_simple_html_text(text)
        if extracted:
            self._put_to_cache(cache_key, extracted, page_meta)
            sliced = self._slice_content(extracted, offset, limit)
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=False,
                result_type="content",
                extracted_source="simple_regex",
                content=sliced["content"],
                truncated=sliced["has_more"],
                offset=offset,
                limit=limit,
                returned_chars=sliced["returned_chars"],
                total_chars=sliced["total_chars"],
                has_more=sliced["has_more"],
                cache_hit=False,
                meta=page_meta,
                error=None,
            )

        preview_content = re.sub(r"\s+", " ", self._strip_tags(text)).strip()
        truncated = len(preview_content) > max_preview_chars
        if truncated:
            preview_content = preview_content[:max_preview_chars]
        header = (
            f"Content with removed HTML tags (truncated to {max_preview_chars} characters)"
            if truncated
            else "Content with removed HTML tags"
        )
        preview = f"{header}:\n\n{preview_content}"
        return _result(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=False,
            result_type="preview",
            extracted_source="stripped_html",
            content="",
            preview=preview,
            truncated=truncated,
            max_preview_chars=max_preview_chars if truncated else None,
            meta=page_meta,
            error={"type": "extraction_failed", "message": "Could not extract readable text. Returned preview."},
        )


FetchUrl = SimpleFetchUrl
