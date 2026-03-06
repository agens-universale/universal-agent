"""
Инструменты для работы со скиллами (skills_data/*) в формате tools_data.

Экспортируемая функция:
- get_skills_tools_data() -> Dict[str, Dict[str, Any]]
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from tools.skills.registry import SkillsRegistry

logger = logging.getLogger(__name__)


class SkillsListTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, **kwargs: Any) -> Dict[str, Any]:
        try:
            skills = self._registry.list_skills()
            return {"skills": skills}
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_list failed: %s", exc, exc_info=True)
            return {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"skills_list failed: {exc}",
                }
            }


class LoadSkillTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, skill_id: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            detail = self._registry.load_skill_detail(skill_id)
            return {"skill": detail}
        except KeyError as exc:
            return {
                "error": {
                    "code": "SKILL_NOT_FOUND",
                    "message": str(exc),
                }
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_load failed: %s", exc, exc_info=True)
            return {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"skills_load failed: {exc}",
                }
            }


class RunSkillScriptTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(
        self,
        skill_id: str,
        script_name: str | None = None,
        args: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            result = self._registry.run_script(
                skill_id=skill_id,
                script_name=script_name,
                args=args,
            )
            return result
        except FileNotFoundError as exc:
            return {
                "error": {
                    "code": "SCRIPT_NOT_FOUND",
                    "message": str(exc),
                }
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_run_script failed: %s", exc, exc_info=True)
            return {
                "error": {
                    "code": "SCRIPT_EXECUTION_FAILED",
                    "message": f"skills_run_script failed: {exc}",
                }
            }


class LoadReferenceTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, skill_id: str, slug: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            return self._registry.load_reference(skill_id=skill_id, slug=slug)
        except KeyError as exc:
            return {
                "error": {
                    "code": "RESOURCE_NOT_FOUND",
                    "message": str(exc),
                }
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_load_reference failed: %s", exc, exc_info=True)
            return {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"skills_load_reference failed: {exc}",
                }
            }


class LoadAssetTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, skill_id: str, slug: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            return self._registry.load_asset(skill_id=skill_id, slug=slug)
        except KeyError as exc:
            return {
                "error": {
                    "code": "RESOURCE_NOT_FOUND",
                    "message": str(exc),
                }
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_load_asset failed: %s", exc, exc_info=True)
            return {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"skills_load_asset failed: {exc}",
                }
            }


def get_skills_tools_data(skills_registry: SkillsRegistry) -> Dict[str, Dict[str, Any]]:
    """
    Собрать словарь инструментов работы со скиллами в формате tools_data.
    """
    def _make_fabric(tool_cls: type):
        def fabric(**_kwargs: Any):
            return tool_cls(skills_registry)

        return fabric

    tools: Dict[str, Dict[str, Any]] = {
        "skills_list": {
            "tool_fabric": _make_fabric(SkillsListTool),
            "spec": {
                "type": "function",
                "name": "skills_list",
                "description": (
                    "List all available local skills discovered in the skills_data directory. "
                    "Use this tool to see which skills (id, name, description) you can work with."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            "description": (
                "Tool name: skills_list\n"
                "Tool description: Lists all available local skills from skills_data/*.\n"
                "Input schema: {}\n"
                "\n"
                "Usage:\n"
                "- Call this tool when you want to know which skills are available.\n"
                "- Each skill has an id, name and description that you can use to select it."
            ),
        },
        "skills_load": {
            "tool_fabric": _make_fabric(LoadSkillTool),
            "spec": {
                "type": "function",
                "name": "skills_load",
                "description": (
                    "Load a specific skill by its id. Returns metadata, instructions, "
                    "and lists of available references and assets."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "The id of the skill to load.",
                        }
                    },
                    "required": ["skill_id"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            "description": (
                "Tool name: skills_load\n"
                "Tool description: Loads a skill's instructions and metadata by id.\n"
                "Input schema: {\"type\": \"object\", \"properties\": {\"skill_id\": {\"type\": \"string\"}}, "
                "\"required\": [\"skill_id\"], \"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Use this tool after skills_list to inspect a particular skill.\n"
                "- The result contains the instructions text you should follow when using this skill.\n"
                "- It also returns which references and assets are available for this skill."
            ),
        },
        "skills_run_script": {
            "tool_fabric": _make_fabric(RunSkillScriptTool),
            "spec": {
                "type": "function",
                "name": "skills_run_script",
                "description": (
                    "Run a Python script that belongs to a specific skill. "
                    "Arguments are passed as JSON via stdin. "
                    "If script_name is omitted, scripts/main.py is used."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "The id of the skill whose script should be executed.",
                        },
                        "script_name": {
                            "type": "string",
                            "description": (
                                "Optional script file name inside the skill's scripts/ directory. "
                                "Defaults to 'main.py' if not provided."
                            ),
                        },
                        "args": {
                            "type": "object",
                            "description": (
                                "Optional JSON object with arguments to pass to the script via stdin."
                            ),
                        },
                    },
                    "required": ["skill_id"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            "description": (
                "Tool name: skills_run_script\n"
                "Tool description: Runs a Python script of a specific skill.\n"
                "Input schema: {\"type\": \"object\", \"properties\": {\"skill_id\": {\"type\": \"string\"}, "
                "\"script_name\": {\"type\": \"string\"}, \"args\": {\"type\": \"object\"}}, "
                "\"required\": [\"skill_id\"], \"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Use this tool when a skill defines executable logic in its scripts/ directory.\n"
                "- If you don't know the script name, omit it to run scripts/main.py by default.\n"
                "- Provide any arguments the script expects inside the 'args' JSON object.\n"
                "- The result includes stdout, stderr and the exit code."
            ),
        },
        "skills_load_reference": {
            "tool_fabric": _make_fabric(LoadReferenceTool),
            "spec": {
                "type": "function",
                "name": "skills_load_reference",
                "description": (
                    "Load a reference document for a given skill by slug. "
                    "References live under the skill's references/ directory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "The id of the skill whose reference you want to load.",
                        },
                        "slug": {
                            "type": "string",
                            "description": (
                                "Slug of the reference. Maps to a file path inside references/, "
                                "without extension, with '/' as path separator."
                            ),
                        },
                    },
                    "required": ["skill_id", "slug"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            "description": (
                "Tool name: skills_load_reference\n"
                "Tool description: Loads a reference document for a skill by slug.\n"
                "Input schema: {\"type\": \"object\", \"properties\": {\"skill_id\": {\"type\": \"string\"}, "
                "\"slug\": {\"type\": \"string\"}}, \"required\": [\"skill_id\", \"slug\"], "
                "\"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Use this tool to fetch additional documentation or examples for a skill.\n"
                "- Slug corresponds to the relative path under references/ without file extension.\n"
                "- The result includes filename, content_type and textual content."
            ),
        },
        "skills_load_asset": {
            "tool_fabric": _make_fabric(LoadAssetTool),
            "spec": {
                "type": "function",
                "name": "skills_load_asset",
                "description": (
                    "Load an asset file for a given skill by slug. "
                    "Assets live under the skill's assets/ directory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "The id of the skill whose asset you want to load.",
                        },
                        "slug": {
                            "type": "string",
                            "description": (
                                "Slug of the asset. Maps to a file path inside assets/, "
                                "without extension, with '/' as path separator."
                            ),
                        },
                    },
                    "required": ["skill_id", "slug"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            "description": (
                "Tool name: skills_load_asset\n"
                "Tool description: Loads an asset file for a skill by slug.\n"
                "Input schema: {\"type\": \"object\", \"properties\": {\"skill_id\": {\"type\": \"string\"}, "
                "\"slug\": {\"type\": \"string\"}}, \"required\": [\"skill_id\", \"slug\"], "
                "\"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Use this tool to fetch templates, prompts or other text assets for a skill.\n"
                "- Slug corresponds to the relative path under assets/ without file extension.\n"
                "- In this minimal version all assets are treated as text."
            ),
        },
    }

    return tools


__all__ = [
    "get_skills_tools_data",
]

