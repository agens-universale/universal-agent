from __future__ import annotations

import asyncio
import functools
import html as html_lib
import os
import re
from typing import Any, Dict, Optional

from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.security.path_policy import resolve_tool_context
from gc_core.security.url_policy import enforce_url_policy
from gc_core.tools.exec.fetch_url_base import FetchUrlBase, http_error_failure
from gc_core.tools.exec.redirect_policy import (
    RedirectBlocked,
    TooManyRedirects,
    fetch_validating_redirects,
)
from gc_core.utils.async_http import get_async_client
from gc_core.utils.http_proxy import resolve_proxies


@loop_warn_on_repeat(**READ_PROFILE)
class FetchUrl(FetchUrlBase):
    #: application/*, которые не считаются бинарными (см. FetchUrlBase).
    _NON_BINARY_APPLICATION_TYPES = (
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/x-javascript",
        "application/x-www-form-urlencoded",
    )

    @classmethod
    def tool_description(cls, settings: Any = None):
        return cls._tool_spec(
            description=(
                "Fetches a URL via HTTP GET and returns extracted readable text when possible. "
                "Uses external network; does not execute local code or shell commands. "
                "If the response is binary (e.g., images, PDFs, archives), does NOT return raw bytes; "
                "instead returns content='binary data' with is_binary=true. "
                "Stores full extracted text in session in-memory cache by URL and supports paginated reads via offset/limit. "
                "Always returns a JSON string object: metadata fields first (ok, url, final_url, status_code, "
                "content_type, is_binary, result_type, extracted_source, truncated, error, pagination fields), "
                "then bulk text fields last (content, preview)."
            ),
            timeout_description=(
                f"HTTP timeout in seconds. Default is {cls.timeout_seconds}."
            ),
            preview_description="Maximum number of characters for HTML/text preview in the response. Default 1200.",
        )

    @classmethod
    def prompt_description(cls, settings: Any = None):
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

    @classmethod
    def _meta_title_text(cls, fragment: str) -> str:
        return cls._strip_html_tags(fragment)

    @staticmethod
    def _find_meta_description(html: str) -> Optional[str]:
        """Сырое ``content`` первого ``<meta name="description">``, иначе ``og:description``."""
        description_patterns = [
            r"""<meta\b(?=[^>]*\bname\s*=\s*(['"])description\1)(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>""",
            r"""<meta\b(?=[^>]*\bproperty\s*=\s*(['"])og:description\1)(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>""",
        ]
        for pattern in description_patterns:
            description_match = re.search(
                pattern, html, flags=re.IGNORECASE | re.DOTALL
            )
            if description_match:
                return description_match.group(3)
        return None

    def _parse_primary(self, text: str, url: str) -> tuple:
        """Метаданные + основной текст. Синхронно и дорого — звать через to_thread."""
        trafilatura = _extractor("trafilatura")

        return self._extract_meta(text), trafilatura.extract(
            text,
            url=url,
            include_links=True,
            include_images=False,
            favor_recall=True,
        )

    def _parse_readability(self, text: str, url: str) -> tuple:
        """Запасной разбор через readability. Синхронно и дорого — через to_thread."""
        Document = _extractor("Document")
        trafilatura = _extractor("trafilatura")

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

    async def _arequest_with_retries(
        self,
        normalized_url: str,
        headers: Dict[str, str],
        effective_timeout,
        normalized_proxy,
    ):
        """(response, None) либо (None, kwargs ошибки для _result)."""
        # Контекст политики нужен на КАЖДОМ переходе редиректа, а не только
        # на стартовом адресе (его проверил гвард `enforce_url_policy`).
        import httpx

        policy_context = resolve_tool_context(
            self, ctx=self._ctx, state=self._state
        )
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
                return None, (
                    "__blocked__",
                    {
                        "type": decision.error_type or "url_blocked_by_policy",
                        "reason": decision.reason,
                        "matched_rule": decision.matched_rule,
                        "blocked_on_redirect": True,
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                failure = http_error_failure(exc, attempt, attempts)
                if failure is not None:
                    return None, failure
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        return None, dict(
            ok=False,
            result_type="error",
            error={
                "type": "request_failed",
                "message": str(last_exc)
                if last_exc
                else "Unknown request failure.",
                "attempts_total": attempts,
            },
        )

    async def _aextract_readable(self, text: str, url: str):
        """(page_meta, (source, text)) — читаемый текст первым сработавшим экстрактором, иначе (page_meta, None)."""
        # Разбор — в поток: он синхронный, тяжёлый (trafilatura/lxml) и на
        # больших страницах занимает секунды.
        page_meta, extracted_text = await asyncio.to_thread(
            self._parse_primary, text, url
        )
        if extracted_text and extracted_text.strip():
            c = extracted_text.strip()
            if not self._looks_like_garbled_text(c):
                return page_meta, ("trafilatura", c)
        try:
            summary_html, fallback_text = await asyncio.to_thread(
                self._parse_readability, text, url
            )
            if fallback_text and fallback_text.strip():
                c = fallback_text.strip()
                if not self._looks_like_garbled_text(c):
                    return page_meta, ("readability_trafilatura", c)
            cleaned_summary = self._strip_html_tags(summary_html)
            if cleaned_summary and not self._looks_like_garbled_text(
                cleaned_summary
            ):
                return page_meta, ("readability_stripped", cleaned_summary)
        except Exception:
            pass
        return page_meta, None

    def _decode_failed_payload(
        self, final_url, status_code, content_type, e: Exception
    ) -> Dict[str, Any]:
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
        # Проверка аргументов и попадание в кэш — общие с simple_fetch_url.
        early, request = self._begin_fetch(
            url, timeout, max_preview_chars, offset, limit, proxy
        )
        if request is None:
            return early
        _result = functools.partial(self._result, url=url)
        headers = self._browser_headers()

        # Контекст политики нужен на КАЖДОМ переходе редиректа, а не только
        # на стартовом адресе (его проверил гвард `enforce_url_policy`).
        response, request_error = await self._arequest_with_retries(
            request.url, headers, request.timeout, request.proxy
        )
        if isinstance(request_error, tuple):
            return self._url_policy_blocked_result(
                request_error[1], request.url
            )
        if request_error is not None:
            return _result(**request_error)

        content_type = response.headers.get("Content-Type", "")
        final_url = str(response.url)
        status_code = response.status_code
        raw_content = getattr(response, "content", b"")
        is_binary = self._is_binary_response(content_type, raw_content)

        if is_binary:
            return _result(
                **self._binary_payload(
                    final_url,
                    status_code,
                    content_type,
                    request.offset,
                    request.limit,
                )
            )

        # Prefer explicit decoding via requests (respects apparent encoding)
        try:
            text = response.text or ""
        except Exception as e:
            # If decoding fails, treat as binary to avoid dumping bytes into chat
            return _result(
                **self._decode_failed_payload(
                    final_url, status_code, content_type, e
                )
            )

        # Разбор — в поток: он синхронный, тяжёлый (trafilatura/lxml) и на
        # больших страницах занимает секунды. Пока __call__ был синхронным,
        # диспетчер уводил в поток весь тул; теперь тул корутинный, и без
        # to_thread разбор встал бы на event loop вызывающего.
        page_meta, extracted = await self._aextract_readable(
            text, str(response.url)
        )
        if extracted is not None:
            source, c = extracted
            self._put_to_cache(request.cache_key, c, page_meta)
            return _result(
                **self._content_payload(
                    c,
                    request.offset,
                    request.limit,
                    source=source,
                    cache_hit=False,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    meta=page_meta,
                )
            )

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
        return _result(
            **self._preview_payload(
                stripped,
                max_preview_chars,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                meta=page_meta,
            )
        )


def _extractor(name):
    # Preserve module-local overrides used by embedders and test transports.
    return globals()[name] if name in globals() else __getattr__(name)


def __getattr__(name):
    if name == "trafilatura":
        import trafilatura

        return trafilatura
    if name == "Document":
        from readability import Document

        return Document
    raise AttributeError(name)
