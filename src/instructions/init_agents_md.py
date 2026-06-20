from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from security.confirm_port import confirm_available, request_confirm
from bundled_assets import bundled_root


def _prompt_template() -> str:
    return (bundled_root() / "prompts" / "init_agents_md_prompt.md").read_text(encoding="utf-8")


def resolve_init_mode(*, exists: bool, ctx: Any) -> str:
    if not exists:
        return "fresh"
    if not confirm_available(ctx):
        return "cancel"  # headless — не трогаем существующий файл
    ok = request_confirm(
        title="/init",
        question="AGENTS.md exists. Overwrite from scratch? (No = update/augment)",
        fields=[("file", "AGENTS.md")],
        ctx=ctx,
    )
    return "overwrite" if ok else "update"


def run_init_agents_md(*, cwd: str, ctx: Any, generate_fn: Callable[[str], str]) -> dict:
    target = Path(cwd) / "AGENTS.md"
    mode = resolve_init_mode(exists=target.exists(), ctx=ctx)
    if mode == "cancel":
        return {"status": "cancelled", "path": str(target), "mode": mode}

    existing_block = ""
    if mode == "update" and target.exists():
        existing_block = (
            "Update the existing AGENTS.md below — refresh and augment, keep manual "
            "edits and structure:\n\n" + target.read_text(encoding="utf-8")
        )
    elif mode in ("fresh", "overwrite"):
        existing_block = "Generate from scratch."

    prompt = _prompt_template().replace("{existing_block}", existing_block)
    content = generate_fn(prompt)
    if not content or not content.strip():
        return {"status": "empty", "path": str(target), "mode": mode}
    target.write_text(content, encoding="utf-8")  # привилегированный writer (в обход guard)
    return {"status": "written", "path": str(target), "mode": mode}
