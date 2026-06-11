"""Резолв путей к фиксированным prompts-ассетам TUI (package-relative)."""
import os


def tui_special_instructions_path() -> str:
    """Абсолютный путь к prompts/chat_completions_tui_special_instructions.md.

    Package-relative резолв от корня репозитория (тот же приём, что в
    summarize.prompt_loading.default_prompt_path): src/tui/services/ — три
    уровня вверх до корня. Фиксированный product-asset, не config-overridable."""
    base_dir = os.path.dirname(os.path.abspath(__file__))   # src/tui/services
    project_root = os.path.abspath(os.path.join(base_dir, "..", "..", ".."))
    return os.path.join(project_root, "prompts", "chat_completions_tui_special_instructions.md")
