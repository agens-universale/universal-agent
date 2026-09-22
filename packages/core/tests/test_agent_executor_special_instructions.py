from gc_core.utils.local_render_system_prompt import render_system_prompt


def _load_special(path):
    if isinstance(path, str) and path.strip():
        with open(path.strip(), encoding="utf-8") as fd:
            return fd.read().strip() or None
    return None


def test_special_instructions_loaded_and_injected(tmp_path):
    f = tmp_path / "si.md"
    f.write_text("### Asking the user\nUSE-TOOL", encoding="utf-8")
    tmpl = "## Clarifications\n<special_instructions></special_instructions>"
    out = render_system_prompt(tmpl, tools_spec=[], project_info={}, specific_role=None, special_instructions=_load_special(str(f)))
    assert "USE-TOOL" in out


def test_special_instructions_absent_path_empty_slot():
    tmpl = "## Clarifications\n<special_instructions></special_instructions>"
    out = render_system_prompt(tmpl, tools_spec=[], project_info={}, specific_role=None, special_instructions=_load_special(None))
    assert "<special_instructions></special_instructions>" in out
