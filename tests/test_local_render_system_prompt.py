import unittest


from utils.local_render_system_prompt import render_system_prompt


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
            specific_prompt="ROLE_X",
        )

        self.assertIn("<item>projectDir: /tmp/x</item>", out)
        self.assertIn("<item>currentDate: 2026-03-25</item>", out)
        self.assertIn("<specific_role>ROLE_X</specific_role>", out)


if __name__ == "__main__":
    unittest.main()

