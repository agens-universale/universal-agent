"""Документация про субагентов не расходится с дефолтами схемы.

`docs/subagents.md` утверждал, что `turn_ask_human` у субагентов «уже отключён
в дефолтном конфиге». По умолчанию `stop_tools` пуст — субагенты тул получают,
и правка про порт ответа в дочернем контексте понадобилась именно поэтому.
Читатель по документу делал ровно неверный вывод.
"""
import re

from gc_testkit.paths import REPO

DOC = REPO / "docs/subagents.md"


def _default_stop_tools() -> list:
    from gc_core.config_modules.root import AppConfig

    cfg = AppConfig.model_validate({})
    return list(getattr(cfg.agent, "stop_tools", []) or [])


def test_stop_tools_is_empty_by_default():
    """Опора документа: если дефолт изменится, тест ниже станет неверным."""
    assert _default_stop_tools() == [], (
        "дефолт `stop_tools` перестал быть пустым — текст в docs/subagents.md "
        "надо перечитать заново"
    )


def test_doc_does_not_claim_the_tool_is_disabled_by_default():
    text = DOC.read_text(encoding="utf-8")
    claim = re.search(r"уже отключ[её]н", text)
    assert claim is None, (
        "docs/subagents.md снова говорит, что turn_ask_human отключён по "
        "умолчанию, хотя stop_tools пуст и субагенты его получают"
    )


def test_doc_says_the_default_is_empty():
    text = DOC.read_text(encoding="utf-8")
    assert re.search(r"по умолчанию `stop_tools`.*пуст", text, re.I), (
        "в документе не сказано, что по умолчанию список пуст — а это главное, "
        "что нужно знать про turn_ask_human у субагентов"
    )
