"""Промпты агента не запрещают запись сами: разрешения выдаёт рантайм.

Живая проверка показала, что модель отказывала в записи вне проекта ещё до
вызова инструмента — и защита не получала действие вовсе: ни спросить человека,
ни отказать с причиной. Правило о периметре в промпте одно, и оно отдаёт
решение рантайму.
"""

import pytest
from gc_testkit.paths import ASSETS

_PROMPTS = (
    "chat_completions_system_prompt.md",
    "chat_completions_subagent_system_prompt.md",
    "chat_completions_custom_subagent_system_prompt.md",
)


@pytest.mark.parametrize("name", _PROMPTS)
def test_prompt_leaves_write_permissions_to_the_runtime(name):
    text = (ASSETS / "prompts" / name).read_text(encoding="utf-8")
    assert "Permissions are enforced by the runtime, not by you" in text
    assert "A block is a policy decision, not a tool failure" in text
    assert "Write only under allowed paths" not in text
    assert "Obey `projectDir`" not in text
