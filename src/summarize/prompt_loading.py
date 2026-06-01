"""Путь к файлу промпта суммаризации по умолчанию и чтение шаблона."""

import os

from summarize.summary_spec import SUMMARY_SECTIONS_PLACEHOLDER, render_sections_for_prompt


def default_prompt_path() -> str:
    """Возвращает путь к файлу системного промпта суммаризации по умолчанию.

    Назначение: единая точка, откуда оркестратор суммаризации берёт шаблон
    инструкций для LLM, если в конфиге не задан свой путь.

    Returns:
        Абсолютный путь к `prompts/chat_completions_summary_prompt.md` в корне репозитория.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
    return os.path.join(project_root, "prompts", "chat_completions_summary_prompt.md")


def load_prompt_template(path: str) -> str:
    """Читает файл с системным промптом суммаризации и проверяет, что он не пустой.

    Args:
        path: Путь к markdown/текстовому файлу с промптом.

    Returns:
        Содержимое файла без ведущих/конечных пробельных строк.

    Raises:
        ValueError: Если после `strip()` текст пустой — иначе LLM получил бы
            пустой system prompt.
    """
    with open(path, "r", encoding="utf-8") as fd:
        prompt = fd.read().strip()
    if not prompt:
        raise ValueError(f"Summary prompt file is empty: {path}")
    if SUMMARY_SECTIONS_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            SUMMARY_SECTIONS_PLACEHOLDER,
            render_sections_for_prompt(),
        )
    return prompt
