from gc_core.tools.skills.registry import SkillsRegistry
from gc_core.tools.skills.skills_tools import get_skills_tools_data


def test_skills_run_script_args_schema_is_strict_friendly(tmp_path):
    registry = SkillsRegistry(str(tmp_path))
    tools = get_skills_tools_data(registry)
    parameters = tools["skills_run_script"]["spec"]["parameters"]
    args_schema = parameters["properties"]["args"]

    assert args_schema["type"] == "string"
    assert parameters["properties"]["argv"]["type"] == "string"
    assert parameters["required"] == ["skill_id", "script_name"]
