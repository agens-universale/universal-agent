"""Распознавание пробы живости в подставных окружениях.

Прод-код пробу шлёт и сверяет сумму сам. Фейкам нужен ответ «это echo суммы»
до своей логики, чтобы лишний echo не попадал в журнал вызовов.
"""

from __future__ import annotations

import re

# Слагаемые двузначные и без ведущего нуля: `08` в `$((…))` — восьмеричное.
_PROBE_COMMAND_RE = re.compile(r"echo \$\(\(([1-9][0-9])\+([1-9][0-9])\)\)")


def probe_total_in_command(command: str) -> int | None:
    """Сумма из текста пробы, если команда её содержит."""
    match = _PROBE_COMMAND_RE.search(command or "")
    if match is None:
        return None
    return int(match.group(1)) + int(match.group(2))


def probe_stdout(command: str) -> str | None:
    """Ответ живой пробы: строка с суммой. ``None`` — это не проба."""
    total = probe_total_in_command(command)
    if total is None:
        return None
    return f"{total}\n"
