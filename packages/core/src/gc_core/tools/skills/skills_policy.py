"""Policy and prompt presentation for the skills catalog.

Pure functions — no I/O, no registry. Callers pass list_skills() payloads;
this module owns how they are rendered for the agent system prompt.
"""
from __future__ import annotations


def _skill_line(s: dict, *, with_pack_size: bool = False) -> str:
    line = f"- {s.get('id', '')} — {s.get('description', '')}"
    location = s.get("location", "")
    if location:
        line += f" <location>{location}</location>"
    size = s.get("pack_size") if with_pack_size else None
    if size is not None:
        line += f" (pack size: {size})"
    return line


def render_skills_catalog(skills_list: list[dict]) -> str:
    """Render the available-skills catalog for the agent prompt.

    Sectioned and generic: groups strictly by the registry's
    is_pack_bootstrap flag (no skill/pack names hard-coded), deterministic
    (sorted by id). The rule steers selection by fit to the OVERALL task,
    not the first decomposed sub-step; pack bootstraps are presented as
    whole-task candidates alongside standalone skills, without ranking one
    above the other.

    Each entry also exposes its backend-visible location so the agent can read
    skill files through ordinary filesystem tools when needed.
    """
    bootstraps = sorted(
        (s for s in skills_list if s.get("is_pack_bootstrap")),
        key=lambda s: s.get("id", ""),
    )
    flats = sorted(
        (s for s in skills_list if not s.get("is_pack_bootstrap")),
        key=lambda s: s.get("id", ""),
    )

    lines: list[str] = [
        "For solving the task you can use skills. Use the skills tools to "
        "load skill data and resources.",
        "",
    ]

    rule = (
        'Pick a skill by fit to the OVERALL task, not the first sub-step '
        'you decompose it into (e.g. "explore the code" is usually a '
        'sub-step of "build a feature", not the task itself). '
    )
    if bootstraps:
        rule += (
            "A pack's entry (bootstrap) skill opens a guided multi-step "
            "workflow — weigh it as a whole-task candidate alongside "
            "standalone skills, and pick whichever best fits the overall "
            "task. "
        )
    rule += "Exception: trivial / read-only / nothing fits — just proceed."
    lines.append(rule)

    if bootstraps:
        lines.append("")
        lines.append(
            "## Skill packs — load the bootstrap skill to enter a guided "
            "multi-step workflow"
        )
        lines.extend(_skill_line(s, with_pack_size=True) for s in bootstraps)

    if flats:
        lines.append("")
        lines.append("## Standalone skills")
        lines.extend(_skill_line(s) for s in flats)

    return "\n".join(lines)
