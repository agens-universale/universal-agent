import json
import re
from typing import Any


def extract_json_candidate(text: str) -> str:
    """
    Return best-effort JSON candidate from arbitrary text.

    Handles common LLM pattern when JSON is wrapped in markdown fences:
    ```json
    { ... }
    ```
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced.group(1).strip()
    return raw


def parse_json_from_text(text: str, default: Any = None) -> Any:
    """
    Parse JSON object/array from plain or fenced text.

    Returns `default` if parsing fails.
    """
    candidate = extract_json_candidate(text)
    if not candidate:
        return default
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return default
