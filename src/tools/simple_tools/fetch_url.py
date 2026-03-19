import html as html_lib
import functools
import json
import re
import time
from typing import Optional, Dict, Any

import requests
import trafilatura
from readability import Document

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class FetchUrl(AbstractSimpleTool):
    timeout_seconds = 20
    max_content_chars = 20_000
    max_retries = 2
    retry_backoff_seconds = 1.0
    default_user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "fetch_url",
            "description": (
                "Fetches a URL via HTTP GET and returns extracted readable text when possible. "
                "If the response is binary (e.g., images, PDFs, archives), does NOT return raw bytes; "
                "instead returns content='binary data' with is_binary=true. "
                "Always returns a JSON string object with fields: ok, url, final_url, status_code, "
                "content_type, is_binary, result_type, extracted_source, content, preview, truncated, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute URL to fetch (http or https).",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "Optional timeout for HTTP request in seconds. "
                            f"Default is {cls.timeout_seconds}."
                        ),
                    },
                    "max_preview_chars": {
                        "type": "integer",
                        "description": "Maximum number of characters for HTML/text preview in the response. Default 1200.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls):
        return f"""Tool name: fetch_url
Tool description: Downloads a web page via HTTP GET and extracts the main text content using trafilatura.
Input schema: {{"type": "object", "properties": {{"url": {{"type": "string", "description": "Absolute URL to fetch."}}, "timeout_seconds": {{"type": "integer", "description": "HTTP timeout in seconds."}}}}, "required": ["url"], "additionalProperties": false}}

Usage:
- Use this tool when you need readable text from a web page.
- The tool sends a GET request with a realistic browser-like User-Agent.
- Supports only http/https URLs.
- On success returns extracted page text.
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
        if ct.startswith("application/"):
            return True
        return False

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
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": "fetch_url",
            "ok": ok,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "is_binary": is_binary,
            "result_type": result_type,
            "extracted_source": extracted_source,
            "content": content,
            "preview": preview,
            "truncated": truncated,
            "error": error,
        }
        if max_content_chars is not None:
            payload["max_content_chars"] = max_content_chars
        if max_preview_chars is not None:
            payload["max_preview_chars"] = max_preview_chars
        return json.dumps(payload, ensure_ascii=False)

    def __call__(
        self,
        url: str,
        timeout_seconds: Optional[int] = None,
        max_preview_chars: int = 1200,
        **kwargs,
    ) -> str:
        if not isinstance(url, str):
            return self._result(
                ok=False,
                url=str(url),
                error={"type": "invalid_argument", "field": "url"},
            )

        _result = functools.partial(self._result, url=url)

        if timeout_seconds is not None and not isinstance(timeout_seconds, int):
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "timeout_seconds"},
            )

        if not isinstance(max_preview_chars, int) or max_preview_chars <= 0:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "max_preview_chars"},
            )

        normalized_url = url.strip()
        if not normalized_url:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "url", "message": "URL cannot be empty."},
            )

        if not normalized_url.startswith(("http://", "https://")):
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "url", "message": "URL must start with http:// or https://."},
            )

        effective_timeout = timeout_seconds or self.timeout_seconds
        if effective_timeout <= 0:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "timeout_seconds", "message": "timeout_seconds must be greater than 0."},
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
        last_exc = None
        attempts = 1 + self.max_retries
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(
                    normalized_url,
                    headers=headers,
                    timeout=effective_timeout,
                    allow_redirects=True,
                )
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                last_exc = exc
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
            return _result(
                ok=False,
                result_type="error",
                error={
                    "type": "request_failed",
                    "message": str(last_exc) if last_exc else "Unknown request failure.",
                    "attempts_total": attempts,
                },
            )

        content_type = response.headers.get("Content-Type", "")
        final_url = response.url
        status_code = response.status_code
        is_binary = self._is_binary_content_type(content_type)

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
                error=None,
            )

        # Prefer explicit decoding via requests (respects apparent encoding)
        try:
            text = response.text or ""
        except Exception as e:
            # If decoding fails, treat as binary to avoid dumping bytes into chat
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=True,
                result_type="binary",
                content="binary data",
                preview="",
                error={"type": "decode_failed", "message": str(e)},
            )

        extracted_text = trafilatura.extract(
            text,
            url=response.url,
            include_links=False,
            include_images=False,
            favor_recall=True,
        )

        if extracted_text and extracted_text.strip():
            c = extracted_text.strip()
            if not self._looks_like_garbled_text(c):
                truncated = len(c) > self.max_content_chars
                if truncated:
                    c = c[: self.max_content_chars]
                return _result(
                    ok=True,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    is_binary=False,
                    result_type="content",
                    extracted_source="trafilatura",
                    content=c,
                    preview="",
                    truncated=truncated,
                    max_content_chars=self.max_content_chars if truncated else None,
                    error=None,
                )

        try:
            sanitized_text = self._sanitize_for_xml(text)
            readable_doc = Document(sanitized_text, url=response.url)
            summary_html = readable_doc.summary(html_partial=True) or ""
            fallback_text = trafilatura.extract(
                summary_html,
                url=response.url,
                include_links=False,
                include_images=False,
                favor_recall=True,
            )
            if fallback_text and fallback_text.strip():
                c = fallback_text.strip()
                if not self._looks_like_garbled_text(c):
                    truncated = len(c) > self.max_content_chars
                    if truncated:
                        c = c[: self.max_content_chars]
                    return _result(
                        ok=True,
                        final_url=final_url,
                        status_code=status_code,
                        content_type=content_type,
                        is_binary=False,
                        result_type="content",
                        extracted_source="readability_trafilatura",
                        content=c,
                        preview="",
                        truncated=truncated,
                        max_content_chars=self.max_content_chars if truncated else None,
                        error=None,
                    )

            cleaned_summary = self._strip_html_tags(summary_html)
            if cleaned_summary and not self._looks_like_garbled_text(cleaned_summary):
                c = cleaned_summary
                truncated = len(c) > self.max_content_chars
                if truncated:
                    c = c[: self.max_content_chars]
                return _result(
                    ok=True,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    is_binary=False,
                    result_type="content",
                    extracted_source="readability_stripped",
                    content=c,
                    preview="",
                    truncated=truncated,
                    max_content_chars=self.max_content_chars if truncated else None,
                    error=None,
                )
        except Exception:
            pass

        stripped = self._strip_html_tags(text)
        if self._looks_like_garbled_text(stripped):
            return _result(
                ok=False,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=False,
                result_type="error",
                error={
                    "type": "extraction_failed",
                    "message": "Could not extract readable text. Response appears garbled or encoded.",
                },
            )
        truncated = len(stripped) > max_preview_chars
        if truncated:
            stripped = stripped[:max_preview_chars]
        header = (
            f"Content with removed HTML tags (truncated to {max_preview_chars} characters)"
            if truncated
            else "Content with removed HTML tags"
        )
        preview = f"{header}:\n\n{stripped}"
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
            error={"type": "extraction_failed", "message": "Could not extract readable text. Returned preview."},
        )
