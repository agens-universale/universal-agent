from tools.skills.registry import SkillsRegistry
from tools.skills.skills_tools import get_skills_tools_data


def test_skills_run_script_args_schema_is_strict_friendly(tmp_path):
    registry = SkillsRegistry(str(tmp_path))
    tools = get_skills_tools_data(registry)
    args_schema = tools["skills_run_script"]["spec"]["parameters"]["properties"]["args"]

    assert args_schema["type"] == "string"
