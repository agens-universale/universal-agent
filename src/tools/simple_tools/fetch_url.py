import html as html_lib
import re
from typing import Optional

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
                "Extracts main text from a web page. No duplicate URL requests. Returns empty/partial on failure; result is deterministic."
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

    def __call__(
        self,
        url: str,
        timeout_seconds: Optional[int] = None,
        **kwargs,
    ) -> str:
        if not isinstance(url, str):
            return f"Error: Argument 'url' must be a string. Got: {type(url)} ({url})"

        if timeout_seconds is not None and not isinstance(timeout_seconds, int):
            return (
                "Error: Argument 'timeout_seconds' must be an integer or None. "
                f"Got: {type(timeout_seconds)} ({timeout_seconds})"
            )

        normalized_url = url.strip()
        if not normalized_url:
            return "Error: URL cannot be empty."

        if not normalized_url.startswith(("http://", "https://")):
            return "Error: URL must start with http:// or https://."

        effective_timeout = timeout_seconds or self.timeout_seconds
        if effective_timeout <= 0:
            return "Error: timeout_seconds must be greater than 0."

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
            return f"Error: Failed to fetch URL '{normalized_url}': {exc}"

        html = response.text

        extracted_text = trafilatura.extract(
            html,
            url=response.url,
            include_links=False,
            include_images=False,
            favor_recall=True,
        )

        if extracted_text and extracted_text.strip():
            return extracted_text.strip()

        try:
            readable_doc = Document(html, url=response.url)
            summary_html = readable_doc.summary(html_partial=True) or ""
            fallback_text = trafilatura.extract(
                summary_html,
                url=response.url,
                include_links=False,
                include_images=False,
                favor_recall=True,
            )
            if fallback_text and fallback_text.strip():
                return fallback_text.strip()

            cleaned_summary = self._strip_html_tags(summary_html)
            if cleaned_summary:
                return cleaned_summary
        except Exception:
            pass

        preview = html.strip().replace("\r", "").replace("\n", " ")
        preview = preview[:1200]
        return (
            "Warning: Could not extract readable text. "
            f"Returned HTML preview:\n{preview}"
        )
