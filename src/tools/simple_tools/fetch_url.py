import html as html_lib
import functools
import json
import re
from typing import Optional, Dict, Any

import requests
import trafilatura
from readability import Document

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class FetchUrl(AbstractSimpleTool):
    timeout_seconds = 20
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
                "content_type, is_binary, content, extracted, preview, error."
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
- If extraction fails, returns a short preview of raw HTML.
- If request fails, returns an error message with details.
"""

    @staticmethod
    def _strip_html_tags(content: str) -> str:
        no_tags = re.sub(r"<[^>]+>", " ", content)
        normalized = re.sub(r"\s+", " ", no_tags).strip()
        return html_lib.unescape(normalized)

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
        content: str = "",
        extracted: bool = False,
        preview: str = "",
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "fetch_url",
                "ok": ok,
                "url": url,
                "final_url": final_url,
                "status_code": status_code,
                "content_type": content_type,
                "is_binary": is_binary,
                "content": content,
                "extracted": extracted,
                "preview": preview,
                "error": error,
            },
            ensure_ascii=False,
        )

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
            "Accept-Encoding": "gzip, deflate, br",
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

        try:
            response = requests.get(
                normalized_url,
                headers=headers,
                timeout=effective_timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return _result(
                ok=False,
                final_url=getattr(getattr(exc, "response", None), "url", None),
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                content_type=getattr(getattr(exc, "response", None), "headers", {}).get("Content-Type")
                if getattr(exc, "response", None) is not None
                else None,
                error={"type": "request_failed", "message": str(exc)},
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
                content="binary data",
                extracted=False,
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
                content="binary data",
                extracted=False,
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
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=False,
                content=extracted_text.strip(),
                extracted=True,
                preview="",
                error=None,
            )

        try:
            readable_doc = Document(text, url=response.url)
            summary_html = readable_doc.summary(html_partial=True) or ""
            fallback_text = trafilatura.extract(
                summary_html,
                url=response.url,
                include_links=False,
                include_images=False,
                favor_recall=True,
            )
            if fallback_text and fallback_text.strip():
                return _result(
                    ok=True,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    is_binary=False,
                    content=fallback_text.strip(),
                    extracted=True,
                    preview="",
                    error=None,
                )

            cleaned_summary = self._strip_html_tags(summary_html)
            if cleaned_summary:
                return _result(
                    ok=True,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    is_binary=False,
                    content=cleaned_summary,
                    extracted=True,
                    preview="",
                    error=None,
                )
        except Exception:
            pass

        preview = text.strip().replace("\r", "").replace("\n", " ")
        preview = preview[:max_preview_chars]
        return _result(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=False,
            content="",
            extracted=False,
            preview=preview,
            error={"type": "extraction_failed", "message": "Could not extract readable text. Returned preview."},
        )
