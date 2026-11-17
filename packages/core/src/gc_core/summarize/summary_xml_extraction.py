"""Isolate one final summary document from auxiliary model output."""

import re

from gc_core.summarize.summary_validation import SummaryRejectedError

_OPEN = re.compile(r"<summary>")
_CLOSE = re.compile(r"</summary>")


def extract_final_summary_xml(text: str) -> str | None:
    """Return the sole closed summary after the last reasoning boundary.

    An absent complete document remains available to the existing partial
    summary repair path. Multiple documents in the final answer are ambiguous.
    """
    answer = (text or "").rsplit("</think>", 1)[-1]
    opens = list(_OPEN.finditer(answer))
    closes = list(_CLOSE.finditer(answer))
    if len(opens) > 1 or len(closes) > 1:
        raise SummaryRejectedError(
            "summary produced no usable summary (ambiguous final XML)"
        )
    if len(opens) != 1 or len(closes) != 1:
        return None
    if opens[0].start() >= closes[0].start():
        return None
    return answer[opens[0].start() : closes[0].end()]
