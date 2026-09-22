import asyncio
import functools
import html as html_lib
import json
import os
import re
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import httpx
import trafilatura
from readability import Document

from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.security.path_policy import resolve_tool_context
from gc_core.security.url_policy import enforce_url_policy
from gc_core.tools.exec.redirect_policy import (
    RedirectBlocked,
    TooManyRedirects,
    fetch_validating_redirects,
)
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.async_http import get_async_client
from gc_core.utils.http_proxy import resolve_proxies


def _response_url(exc: Exception) -> "str | None":
    """URL из ответа-ошибки строкой: httpx.URL в JSON не сериализуется."""
    response = getattr(exc, "response", None)
    url = getattr(response, "url", None)
    return str(url) if url is not None else None


def _preview_from_stripped(stripped: str, max_preview_chars: int) -> "tuple[str, bool]":
    truncated = len(stripped) > max_preview_chars
    if truncated:
        stripped = stripped[:max_preview_chars]
    header = (
        f"Content with removed HTML tags (truncated to {max_preview_chars} characters)"
        if truncated
        else "Content with removed HTML tags"
    )
    return f"{header}:\n\n{stripped}", truncated


@loop_warn_on_repeat(**READ_PROFILE)
class FetchUrl(AbstractSimpleTool):
    timeout_seconds = 20
    max_content_chars = 20_000
    # LRU-лимит кэша извлечённого текста страниц (на тред/сессию).
    # Кэш хранится в SharedStateData (per-thread, персистится в meta),
    # поэтому лимит ограничивает и RAM, и размер JSON-meta треда.
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
                "Fetches a URL via HTTP GET and returns extracted readable text when possible. "
                "Uses external network; does not execute local code or shell commands. "
                "If the response is binary (e.g., images, PDFs, archives), does NOT return raw bytes; "
                "instead returns content='binary data' with is_binary=true. "
                "Stores full extracted text in session in-memory cache by URL and supports paginated reads via offset/limit. "
                "Always returns a JSON string object: metadata fields first (ok, url, final_url, status_code, "
                "content_type, is_binary, result_type, extracted_source, truncated, error, pagination fields), "
                "then bulk text fields last (content, preview)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute URL to fetch (http or https).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "HTTP timeout in seconds. "
                            f"Default is {cls.timeout_seconds}."
                        ),
                    },
                    "max_preview_chars": {
                        "type": "integer",
                        "description": "Maximum number of characters for HTML/text preview in the response. Default 1200.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset for paginated reads from cached full content. Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum number of characters to return in content. Default {cls.max_content_chars}.",
                    },
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
        return """Tool name: exec_fetch_url
Tool description: Downloads a web page via HTTP GET and extracts the main text content using trafilatura.
Input schema: {"type": "object", "properties": {"url": {"type": "string", "description": "Absolute URL to fetch."}, "timeout": {"type": "integer", "description": "HTTP timeout in seconds."}, "offset": {"type": "integer", "description": "Character offset for paginated reads."}, "limit": {"type": "integer", "description": "Max characters to return in this call."}, "proxy": {"type": "string", "description": "Optional proxy URL (http/https/socks5/socks5h/socks4). Empty string or omit = no proxy."}}, "required": ["url"], "additionalProperties": false}

Usage:
- Use this tool when you need readable text from a web page.
- The tool sends a GET request with a realistic browser-like User-Agent.
- Supports only http/https URLs.
- Optional proxy routes this request; empty string or omit means direct (no proxy).
- On success stores full extracted page text in session cache keyed by URL.
- Use offset/limit to read long pages in chunks from cache.
- If extraction fails, returns preview with HTML tags removed (truncated to max_preview_chars).
- If request fails, returns an error message with details.
"""

    @staticmethod
    def _strip_html_tags(content: str) -> str:
        no_tags = re.sub(r"<[^>]+>", " ", content)
        normalized = re.sub(r"\s+", " ", no_tags).strip()
        return html_lib.unescape(normalized)

    _XML_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

    @classmethod
    def _sanitize_for_xml(cls, text: str) -> str:
        """Remove NULL bytes and control chars invalid in XML before lxml/readability parsing."""
        return cls._XML_INVALID_CHARS.sub("", text)

    @classmethod
    def _looks_like_garbled_text(cls, text: str) -> bool:
        if not text:
            return False
        sample = text[:2000]
        if "\x00" in sample:
            return True
        bad_controls = len(cls._XML_INVALID_CHARS.findall(sample))
        if bad_controls > 3:
            return True
        replacement_chars = sample.count("\ufffd")
        if replacement_chars > 5:
            return True
        total = len(sample)
        printable = 0
        for ch in sample:
            if ch in ("\n", "\r", "\t") or ch.isprintable():
                printable += 1
        printable_ratio = printable / max(total, 1)
        return printable_ratio < 0.85

    @staticmethod
    def _is_binary_content_type(content_type: str) -> bool:
        ct = (content_type or "").split(";")[0].strip().lower()
        if not ct:
            return False
        if ct.startswith("text/"):
            return False
        # Treat these as text-like
        if ct in (
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/javascript",
            "application/x-javascript",
            "application/x-www-form-urlencoded",
        ):
            return False
        # Common binary types
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
        # Default: unknown application/* considered binary to be safe
        return bool(ct.startswith("application/"))

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
            "tool": FetchUrl.registered_tool_name(),
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
            shared.fetch_url_cache = cache
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
            title = cls._strip_html_tags(title_match.group(1)).strip()
            meta["title"] = title or None

        html_match = re.search(r"<html\b([^>]*)>", html, flags=re.IGNORECASE)
        if html_match:
            attrs = html_match.group(1)
            lang_match = re.search(r"""\blang\s*=\s*(['"])(.*?)\1""", attrs, flags=re.IGNORECASE | re.DOTALL)
            if lang_match:
                lang = html_lib.unescape(re.sub(r"\s+", " ", lang_match.group(2))).strip()
                meta["lang"] = lang or None

        description_patterns = [
            r"""<meta\b(?=[^>]*\bname\s*=\s*(['"])description\1)(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>""",
            r"""<meta\b(?=[^>]*\bproperty\s*=\s*(['"])og:description\1)(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>""",
        ]
        for pattern in description_patterns:
            description_match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
            if description_match:
                description = html_lib.unescape(re.sub(r"\s+", " ", description_match.group(3))).strip()
                meta["description"] = description or None
                break
        return cls._normalize_meta(meta)

    def _slice_content(self, content: str, offset: int, limit: int) -> Dict[str, Any]:
        total_chars = len(content)
        if offset >= total_chars:
            return {
                "content": "",
                "returned_chars": 0,
                "total_chars": total_chars,
                "has_more": False,
            }
        chunk = content[offset : offset + limit]
        returned_chars = len(chunk)
        has_more = offset + returned_chars < total_chars
        return {
            "content": chunk,
            "returned_chars": returned_chars,
            "total_chars": total_chars,
            "has_more": has_more,
        }

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

    def _parse_primary(self, text: str, url: str) -> tuple:
        """Метаданные + основной текст. Синхронно и дорого — звать через to_thread."""
        return self._extract_meta(text), trafilatura.extract(
            text,
            url=url,
            include_links=True,
            include_images=False,
            favor_recall=True,
        )

    def _parse_readability(self, text: str, url: str) -> tuple:
        """Запасной разбор через readability. Синхронно и дорого — через to_thread."""
        sanitized_text = self._sanitize_for_xml(text)
        readable_doc = Document(sanitized_text, url=url)
        summary_html = readable_doc.summary(html_partial=True) or ""
        fallback_text = trafilatura.extract(
            summary_html,
            url=url,
            include_links=False,
            include_images=False,
            favor_recall=True,
        )
        return summary_html, fallback_text


    def _fetch_arg_error(self, timeout, max_preview_chars, offset, limit) -> "Optional[Dict[str, Any]]":
        if timeout is not None and not isinstance(timeout, int):
            return {"type": "invalid_argument", "field": "timeout"}
        if not isinstance(max_preview_chars, int) or max_preview_chars <= 0:
            return {"type": "invalid_argument", "field": "max_preview_chars"}
        if not isinstance(offset, int) or offset < 0:
            return {"type": "invalid_argument", "field": "offset"}
        if not isinstance(limit, int) or limit <= 0:
            return {"type": "invalid_argument", "field": "limit"}
        return None

    def _url_or_timeout_error(self, normalized_url: str, timeout) -> "Optional[Dict[str, Any]]":
        if not normalized_url:
            return {"type": "invalid_argument", "field": "url", "message": "URL cannot be empty."}
        if not normalized_url.startswith(("http://", "https://")):
            return {"type": "invalid_argument", "field": "url", "message": "URL must start with http:// or https://."}
        if (timeout or self.timeout_seconds) <= 0:
            return {"type": "invalid_argument", "field": "timeout", "message": "timeout must be greater than 0."}
        return None

    def _content_payload(self, content: str, offset: int, limit: int, *, source: str, cache_hit: bool, final_url, status_code, content_type, meta) -> Dict[str, Any]:
        sliced = self._slice_content(content, offset, limit)
        return dict(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=False,
            result_type="content",
            extracted_source=source,
            content=sliced["content"],
            preview="",
            truncated=sliced["has_more"],
            offset=offset,
            limit=limit,
            returned_chars=sliced["returned_chars"],
            total_chars=sliced["total_chars"],
            has_more=sliced["has_more"],
            cache_hit=cache_hit,
            meta=meta,
            error=None,
        )

    async def _arequest_with_retries(self, normalized_url: str, headers: Dict[str, str], effective_timeout, normalized_proxy):
        """(response, None) либо (None, kwargs ошибки для _result)."""
        # Контекст политики нужен на КАЖДОМ переходе редиректа, а не только
        # на стартовом адресе (его проверил гвард `enforce_url_policy`).
        policy_context = resolve_tool_context(self, ctx=self._ctx, state=self._state)
        last_exc = None
        attempts = 1 + self.max_retries
        for attempt in range(1, attempts + 1):
            try:
                # Таймаут — на запрос: клиент общий и переиспользуется. Явный
                # `proxy` тула идёт ПЕРВЫМ аргументом: он и должен побеждать окружение.
                client = get_async_client(
                    proxies=resolve_proxies(
                        normalized_proxy, os.environ, normalized_url
                    )
                )
                # Каждый переход проверяется политикой (см. redirect_policy).
                response = await fetch_validating_redirects(
                    client,
                    normalized_url,
                    headers,
                    effective_timeout,
                    allowed_schemes=policy_context.url_allowed_schemes,
                    url_blacklist=policy_context.url_blacklist,
                )
                response.raise_for_status()
                return response, None
            except TooManyRedirects as loop_exc:
                # НЕ политика: это петля редиректов.
                return None, dict(
                    ok=False,
                    final_url=loop_exc.url,
                    status_code=None,
                    content_type=None,
                    result_type="error",
                    error={
                        "type": "too_many_redirects",
                        "message": str(loop_exc),
                        "limit": loop_exc.limit,
                    },
                )
            except RedirectBlocked as redirect_exc:
                decision = redirect_exc.decision
                return None, ("__blocked__", {
                    "type": decision.error_type or "url_blocked_by_policy",
                    "reason": decision.reason,
                    "matched_rule": decision.matched_rule,
                    "blocked_on_redirect": True,
                })
            except httpx.HTTPError as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status in (429,) or status >= 500
                if (not retryable) or attempt == attempts:
                    return None, dict(
                        ok=False,
                        final_url=_response_url(exc),
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
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        return None, dict(
            ok=False,
            result_type="error",
            error={
                "type": "request_failed",
                "message": str(last_exc) if last_exc else "Unknown request failure.",
                "attempts_total": attempts,
            },
        )

    async def _aextract_readable(self, text: str, url: str):
        """(page_meta, (source, text)) — читаемый текст первым сработавшим экстрактором, иначе (page_meta, None)."""
        # Разбор — в поток: он синхронный, тяжёлый (trafilatura/lxml) и на
        # больших страницах занимает секунды.
        page_meta, extracted_text = await asyncio.to_thread(self._parse_primary, text, url)
        if extracted_text and extracted_text.strip():
            c = extracted_text.strip()
            if not self._looks_like_garbled_text(c):
                return page_meta, ("trafilatura", c)
        try:
            summary_html, fallback_text = await asyncio.to_thread(self._parse_readability, text, url)
            if fallback_text and fallback_text.strip():
                c = fallback_text.strip()
                if not self._looks_like_garbled_text(c):
                    return page_meta, ("readability_trafilatura", c)
            cleaned_summary = self._strip_html_tags(summary_html)
            if cleaned_summary and not self._looks_like_garbled_text(cleaned_summary):
                return page_meta, ("readability_stripped", cleaned_summary)
        except Exception:
            pass
        return page_meta, None

    def _is_binary_response(self, content_type: str, raw_content: bytes) -> bool:
        content_type_binary = self._is_binary_content_type(content_type)
        content_type_text = self._is_text_like_content_type(content_type)
        return content_type_binary or (not content_type_text and self._looks_binary_bytes(raw_content))

    def _decode_failed_payload(self, final_url, status_code, content_type, e: Exception) -> Dict[str, Any]:
        return dict(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=True,
            result_type="binary",
            content="binary data",
            preview="",
            meta=self._normalize_meta(None),
            error={"type": "decode_failed", "message": str(e)},
        )

    @enforce_url_policy(url_arg="url")
    async def acall(
        self,
        url: str,
        timeout: Optional[int] = None,
        max_preview_chars: int = 1200,
        offset: int = 0,
        limit: Optional[int] = None,
        proxy: Optional[str] = None,
        **kwargs,
    ) -> str:
        if not isinstance(url, str):
            return self._result(
                ok=False,
                url=str(url),
                error={"type": "invalid_argument", "field": "url"},
            )

        _result = functools.partial(self._result, url=url)

        if limit is None:
            limit = self.max_content_chars
        arg_error = self._fetch_arg_error(timeout, max_preview_chars, offset, limit)
        if arg_error is not None:
            return _result(ok=False, error=arg_error)
        normalized_proxy, proxy_error = self._normalize_proxy(proxy)
        if proxy_error is not None:
            return _result(ok=False, error=proxy_error)
        normalized_url = url.strip()
        url_error = self._url_or_timeout_error(normalized_url, timeout)
        if url_error is not None:
            return _result(ok=False, error=url_error)
        effective_timeout = timeout or self.timeout_seconds

        cache_key = self._cache_key(normalized_url, normalized_proxy)
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            cached_content, cached_meta = cached
            return _result(**self._content_payload(
                cached_content, offset, limit, source="cache", cache_hit=True,
                final_url=normalized_url, status_code=200,
                content_type="text/plain; charset=utf-8", meta=cached_meta,
            ))

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

        # Контекст политики нужен на КАЖДОМ переходе редиректа, а не только
        # на стартовом адресе (его проверил гвард `enforce_url_policy`).
        response, request_error = await self._arequest_with_retries(normalized_url, headers, effective_timeout, normalized_proxy)
        if isinstance(request_error, tuple):
            return self._url_policy_blocked_result(request_error[1], normalized_url)
        if request_error is not None:
            return _result(**request_error)

        content_type = response.headers.get("Content-Type", "")
        final_url = str(response.url)
        status_code = response.status_code
        raw_content = getattr(response, "content", b"")
        is_binary = self._is_binary_response(content_type, raw_content)

        if is_binary:
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=True,
                result_type="binary",
                content="binary data",
                preview="",
                offset=offset,
                limit=limit,
                returned_chars=0,
                total_chars=0,
                has_more=False,
                cache_hit=False,
                meta=self._normalize_meta(None),
                error=None,
            )

        # Prefer explicit decoding via requests (respects apparent encoding)
        try:
            text = response.text or ""
        except Exception as e:
            # If decoding fails, treat as binary to avoid dumping bytes into chat
            return _result(**self._decode_failed_payload(final_url, status_code, content_type, e))

        # Разбор — в поток: он синхронный, тяжёлый (trafilatura/lxml) и на
        # больших страницах занимает секунды. Пока __call__ был синхронным,
        # диспетчер уводил в поток весь тул; теперь тул корутинный, и без
        # to_thread разбор встал бы на event loop вызывающего.
        page_meta, extracted = await self._aextract_readable(text, str(response.url))
        if extracted is not None:
            source, c = extracted
            self._put_to_cache(cache_key, c, page_meta)
            return _result(**self._content_payload(
                c, offset, limit, source=source, cache_hit=False,
                final_url=final_url, status_code=status_code, content_type=content_type, meta=page_meta,
            ))

        stripped = self._strip_html_tags(text)
        if self._looks_like_garbled_text(stripped):
            return _result(
                ok=False,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=False,
                result_type="error",
                meta=page_meta,
                error={
                    "type": "extraction_failed",
                    "message": "Could not extract readable text. Response appears garbled or encoded.",
                },
            )
        preview, truncated = _preview_from_stripped(stripped, max_preview_chars)
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
