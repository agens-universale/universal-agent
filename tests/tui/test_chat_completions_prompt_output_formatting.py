"""A: Output formatting nudge — секция в системных промптах chat_completions."""
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_prompt(name: str) -> str:
    return (_REPO_ROOT / "prompts" / name).read_text(encoding="utf-8")


def test_main_prompt_has_output_formatting_section():
    body = _read_prompt("chat_completions_system_prompt.md")
    assert "### Output formatting" in body
    assert "fenced markdown" in body
    # Проверяем явное упоминание language-tag (агент должен помечать язык).
    assert "language tag" in body
    # Покрытие inline-кода тоже упомянуто.
    assert "single backticks" in body


def test_subagent_prompt_has_no_branch_output_formatting():
    """Решение интеграции (merge master): сабагентный промпт берётся из master
    целиком — он machine-facing (отдаёт JSON родителю), поэтому TUI-секция
    Output formatting туда не добавляется."""
    body = _read_prompt("chat_completions_subagent_system_prompt.md")
    assert "### Output formatting" not in body


def _section(body: str, header: str) -> str:
    """Текст секции от заголовка до следующего markdown-заголовка (## или ###)."""
    start = body.index(header)
    rest = body[start + len(header):]
    m = re.search(r"\n#{2,3} ", rest)
    return rest[: m.start()] if m else rest


def test_output_formatting_section_is_tool_agnostic():
    """Сама секция Output formatting обобщённая — не привязана к имени
    финализирующего тула (внутри секции submit_solution не упоминается;
    форматирование применяется ко всем ответам). Имя submit_solution в §6
    (Asking vs finalizing) — намеренно, это другая часть промпта."""
    body = _read_prompt("chat_completions_system_prompt.md")
    section = _section(body, "### Output formatting")
    assert "submit_solution" not in section


def test_main_prompt_steers_questions_to_asking_tool():
    """Решение пользователя (option A после live-теста на MiniMax-M2.7): системный
    промпт ПРОАКТИВНО направляет интерактивные вопросы в asking-тул. Без этого
    слабые модели пишут вопрос прозой и зацикливаются в no-tool-call bounce. Имя
    тула НЕ хардкодим (master-инвариант test_orchestrator_prompt_has_no_hardcoded_builtin_tool_names)
    — проверяем обобщённую (tool-agnostic) формулировку."""
    body = _read_prompt("chat_completions_system_prompt.md")
    assert "asking the user" in body
    assert "designated channel" in body


def test_main_prompt_asking_vs_finalizing_rule():
    """Под правило лида: системный промпт различает направления — нужен ответ
    ЧТОБЫ ПРОДОЛЖИТЬ ход → asking-тул; отдаёшь ход → финализация (final report
    может предлагать дальнейшее). submit_solution НЕ хардкодим — есть master-
    инвариант test_orchestrator_prompt_has_no_hardcoded_builtin_tool_names;
    финал описан абстрактно ('finalize'/'final report')."""
    body = _read_prompt("chat_completions_system_prompt.md")
    assert "asking the user" in body  # asking-направление, tool-agnostic (без имени ask_human)
    assert "submit_solution" not in body  # master-инвариант: builtin-тул не хардкодим
    assert "finalize the turn" in body
    assert "propose" in body or "recommend" in body
