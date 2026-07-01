"""Путь к файлу промпта суммаризации по умолчанию и чтение шаблона."""

from summarize.summary_spec import (
    MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
    SUMMARY_OUTPUT_SHAPE_PLACEHOLDER,
    SUMMARY_SECTIONS_PLACEHOLDER,
    render_output_shape_for_prompt,
    render_sections_for_prompt,
    render_summary_tags_for_memory_patch_prompt,
)
from summarize.working_memory_gateway import (
    inject_working_memory_prompt_placeholders,
)
from bundled_assets import bundled_root


def default_prompt_path() -> str:
    """Возвращает путь к файлу системного промпта суммаризации по умолчанию.

    Назначение: единая точка, откуда оркестратор суммаризации берёт шаблон
    инструкций для LLM, если в конфиге не задан свой путь.

    Returns:
        Абсолютный путь к `prompts/chat_completions_summary_prompt.md` в корне репозитория.
    """
    return str(bundled_root() / "prompts" / "chat_completions_summary_prompt.md")


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
    if SUMMARY_OUTPUT_SHAPE_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            SUMMARY_OUTPUT_SHAPE_PLACEHOLDER,
            render_output_shape_for_prompt(),
        )
    return inject_working_memory_prompt_placeholders(prompt)


def _load_memory_patch_prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fd:
        prompt = fd.read().strip()
    if not prompt:
        raise ValueError(f"Memory patch prompt file is empty: {path}")
    if MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
            render_summary_tags_for_memory_patch_prompt(),
        )
    return inject_working_memory_prompt_placeholders(prompt)


def load_memory_patch_replace_prompt(path: str) -> str:
    return _load_memory_patch_prompt(path)


def load_memory_patch_append_prompt(path: str) -> str:
    return _load_memory_patch_prompt(path)
