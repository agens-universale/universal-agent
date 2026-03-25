import json
import re
from typing import Dict, List, Optional, Any


def _replace_tag_block(template: str, tag: str, new_inner: str) -> str:
    """
    Replace the inner content between <tag>...</tag> with new_inner.
    If tag doesn't exist in template - returns template as-is.
    """

    # Use non-greedy match across newlines.
    pattern = rf"(<{re.escape(tag)}>)(.*?)(</{re.escape(tag)}>)"
    if not re.search(pattern, template, flags=re.DOTALL):
        return template

    return re.sub(pattern, rf"\1{new_inner}\3", template, flags=re.DOTALL)


def render_system_prompt(
    system_prompt: str,
    tools_spec: List[Dict[str, Any]],
    project_info: Dict[str, Any],
    specific_prompt: Optional[str],
) -> str:
    """
    Local rendering of chat_completions system prompt.

    Supports both:
    - legacy placeholders: <tool_data>, <user_info>, <specific_role>
    - tag blocks: <user_info>...</user_info>, <specific_role>...</specific_role>
    """
    if not isinstance(system_prompt, str):
        raise ValueError(f"system_prompt is not string. got: {type(system_prompt)}")
    if not isinstance(tools_spec, list):
        raise ValueError(f"tools_spec is not list. got: {type(tools_spec)}")
    if not isinstance(project_info, dict):
        raise ValueError(f"project_info is not dict. got: {type(project_info)}")

    # --- tool data (if tools rendered locally - by default it should be on backend) ---
    if "<tool_data>" in system_prompt:
        tools_tmpl = json.dumps(tools_spec, ensure_ascii=False)
        system_prompt = system_prompt.replace("<tool_data>", tools_tmpl)

    # --- user info ---
    user_items: List[str] = []
    for k, v in project_info.items():
        user_items.append(f"<item>{k}: {v}</item>")
    user_info_inner = "\n".join(user_items) + ("\n" if user_items else "")

    # Tag block variant
    if "</user_info>" in system_prompt:
        system_prompt = _replace_tag_block(
            system_prompt, "user_info", user_info_inner
        )
    # Legacy placeholder variant (no closing tag present)
    elif "<user_info>" in system_prompt:
        system_prompt = system_prompt.replace("<user_info>", user_info_inner)

    # --- specific role ---
    if specific_prompt is None:
        specific_prompt = "Specific role is not provided. Follow the common prompt instructions."

    if "</specific_role>" in system_prompt:
        system_prompt = _replace_tag_block(
            system_prompt, "specific_role", str(specific_prompt)
        )
    elif "<specific_role>" in system_prompt:
        system_prompt = system_prompt.replace("<specific_role>", str(specific_prompt))

    return system_prompt

