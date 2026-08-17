import unittest


from gc_core.utils.local_render_system_prompt import render_system_prompt


def _base(slot: str = "<special_instructions></special_instructions>") -> str:
    return f"Header\n## Clarifications\n{slot}\nFooter"


def test_special_instructions_injected_into_slot():
    out = render_system_prompt(_base(), tools_spec=[], project_info={}, specific_role=None, special_instructions="RULES-X")
    assert "<special_instructions>RULES-X</special_instructions>" in out


def test_special_instructions_none_leaves_slot_empty():
    out = render_system_prompt(_base(), tools_spec=[], project_info={}, specific_role=None, special_instructions=None)
    assert "<special_instructions></special_instructions>" in out


def test_special_instructions_no_tag_is_noop():
    tmpl = "Header\n## Clarifications\nFooter"
    out = render_system_prompt(tmpl, tools_spec=[], project_info={}, specific_role=None, special_instructions="RULES-X")
    assert out == tmpl


class LocalRenderSystemPromptTest(unittest.TestCase):
    def test_replaces_user_info_and_specific_role_tags(self):
        tmpl = (
            "Hello\n<user_info>\n</user_info>\n"
            "<specific_role>\n</specific_role>\n"
        )
        out = render_system_prompt(
            system_prompt=tmpl,
            tools_spec=[],
            project_info={"projectDir": "/tmp/x", "currentDate": "2026-03-25"},
            specific_role="ROLE_X",
        )

        self.assertIn("<item>projectDir: /tmp/x</item>", out)
        self.assertIn("<item>currentDate: 2026-03-25</item>", out)
        self.assertIn("<specific_role>ROLE_X</specific_role>", out)


if __name__ == "__main__":
    unittest.main()

