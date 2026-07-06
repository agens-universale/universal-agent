"""
Инструменты для работы со скиллами (skills_data/*) в формате tools_data.

Экспортируемая функция:
- get_skills_tools_data() -> Dict[str, Dict[str, Any]]
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools.schema_optional import coalesce_optional_string
from tools.skills.registry import AmbiguousSkillId, SkillsRegistry
from utils.arg_decorators import parse_json_object_argument
from utils.json_arg_parser import JsonArgParseError

logger = logging.getLogger(__name__)


def _json_sanitize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "binary data"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = k if isinstance(k, str) else str(k)
            out[key] = _json_sanitize(v)
        return out
    if isinstance(value, list):
        return [_json_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return [_json_sanitize(v) for v in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _wrap(
    *,
    tool: str,
    ok: bool,
    result: Any = None,
    error: Optional[Dict[str, Any]] = None,
    **meta: Any,
) -> str:
    payload = {
        "tool": tool,
        "ok": ok,
        **{k: _json_sanitize(v) for k, v in meta.items()},
        "error": _json_sanitize(error),
        "result": _json_sanitize(result),
    }
    return json.dumps(payload, ensure_ascii=False)


class SkillsListTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, pack: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        pack = coalesce_optional_string(pack)
        try:
            skills = self._registry.list_skills(pack=pack)
            return _wrap(tool="skills_list", ok=True, result={"skills": skills})
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_list failed: %s", exc, exc_info=True)
            return _wrap(
                tool="skills_list",
                ok=False,
                result=None,
                error={"code": "INTERNAL_ERROR", "message": f"skills_list failed: {exc}"},
            )


class LoadSkillTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, skill_id: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            detail = self._registry.load_skill_detail(skill_id)
            return _wrap(
                tool="skills_load",
                ok=True,
                skill_id=skill_id,
                result={"skill": detail},
            )
        except AmbiguousSkillId as exc:
            return _wrap(
                tool="skills_load",
                ok=False,
                skill_id=skill_id,
                result=None,
                error={
                    "code": "AMBIGUOUS_SKILL_ID",
                    "message": str(exc),
                    "candidates": exc.candidates,
                },
            )
        except KeyError as exc:
            return _wrap(
                tool="skills_load",
                ok=False,
                skill_id=skill_id,
                result=None,
                error={"code": "SKILL_NOT_FOUND", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_load failed: %s", exc, exc_info=True)
            return _wrap(
                tool="skills_load",
                ok=False,
                skill_id=skill_id,
                result=None,
                error={"code": "INTERNAL_ERROR", "message": f"skills_load failed: {exc}"},
            )


class RunSkillScriptTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    @parse_json_object_argument(
        source_arg="args",
        target_arg="args_data",
        error_handler_method="_json_arg_error",
    )
    def __call__(
        self,
        skill_id: str,
        script_name: str | None = None,
        args: str | None = None,
        args_data: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            result = self._registry.run_script(
                skill_id=skill_id,
                script_name=script_name,
                args=args_data,
            )
            script_ok = result.get("exit_code", 1) == 0
            return _wrap(
                tool="skills_run_script",
                ok=True,
                script_ok=script_ok,
                skill_id=skill_id,
                script_name=script_name,
                args=args_data,
                result=result,
            )
        except AmbiguousSkillId as exc:
            return _wrap(
                tool="skills_run_script",
                ok=False,
                skill_id=skill_id,
                script_name=script_name,
                args=args_data,
                result=None,
                error={
                    "code": "AMBIGUOUS_SKILL_ID",
                    "message": str(exc),
                    "candidates": exc.candidates,
                },
            )
        except FileNotFoundError as exc:
            return _wrap(
                tool="skills_run_script",
                ok=False,
                skill_id=skill_id,
                script_name=script_name,
                args=args_data,
                result=None,
                error={"code": "SCRIPT_NOT_FOUND", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_run_script failed: %s", exc, exc_info=True)
            return _wrap(
                tool="skills_run_script",
                ok=False,
                skill_id=skill_id,
                script_name=script_name,
                args=args_data,
                result=None,
                error={
                    "code": "SCRIPT_EXECUTION_FAILED",
                    "message": f"skills_run_script failed: {exc}",
                },
            )

    def _json_arg_error(self, exc: JsonArgParseError, call_kwargs: Dict[str, Any]) -> str:
        return _wrap(
            tool="skills_run_script",
            ok=False,
            skill_id=call_kwargs.get("skill_id"),
            script_name=call_kwargs.get("script_name"),
            args=call_kwargs.get("args"),
            result=None,
            error={"code": "INVALID_ARGUMENT", "field": exc.field, "message": exc.message},
        )


class LoadReferenceTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, skill_id: str, slug: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            res = self._registry.load_reference(skill_id=skill_id, slug=slug)
            return _wrap(
                tool="skills_read_reference",
                ok=True,
                skill_id=skill_id,
                slug=slug,
                result=res,
            )
        except AmbiguousSkillId as exc:
            return _wrap(
                tool="skills_read_reference",
                ok=False,
                skill_id=skill_id,
                slug=slug,
                result=None,
                error={
                    "code": "AMBIGUOUS_SKILL_ID",
                    "message": str(exc),
                    "candidates": exc.candidates,
                },
            )
        except KeyError as exc:
            return _wrap(
                tool="skills_read_reference",
                ok=False,
                skill_id=skill_id,
                slug=slug,
                result=None,
                error={"code": "RESOURCE_NOT_FOUND", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_read_reference failed: %s", exc, exc_info=True)
            return _wrap(
                tool="skills_read_reference",
                ok=False,
                skill_id=skill_id,
                slug=slug,
                result=None,
                error={"code": "INTERNAL_ERROR", "message": f"skills_read_reference failed: {exc}"},
            )


class LoadAssetTool:
    def __init__(self, registry: SkillsRegistry, *args: Any, **kwargs: Any) -> None:
        self._registry = registry

    def __call__(self, skill_id: str, slug: str, **kwargs: Any) -> Dict[str, Any]:
        try:
            res = self._registry.load_asset(skill_id=skill_id, slug=slug)
            return _wrap(
                tool="skills_read_asset",
                ok=True,
                skill_id=skill_id,
                slug=slug,
                result=res,
            )
        except AmbiguousSkillId as exc:
            return _wrap(
                tool="skills_read_asset",
                ok=False,
                skill_id=skill_id,
                slug=slug,
                result=None,
                error={
                    "code": "AMBIGUOUS_SKILL_ID",
                    "message": str(exc),
                    "candidates": exc.candidates,
                },
            )
        except KeyError as exc:
            return _wrap(
                tool="skills_read_asset",
                ok=False,
                skill_id=skill_id,
                slug=slug,
                result=None,
                error={"code": "RESOURCE_NOT_FOUND", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("skills_read_asset failed: %s", exc, exc_info=True)
            return _wrap(
                tool="skills_read_asset",
                ok=False,
                skill_id=skill_id,
                slug=slug,
                result=None,
                error={"code": "INTERNAL_ERROR", "message": f"skills_read_asset failed: {exc}"},
            )


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
                    "List available local skills. By default returns flat skills plus one entry "
                    "per pack (the pack's bootstrap skill, marked with 'is_pack_bootstrap': true and 'pack_size': N). "
                    "Internal pack skills are hidden by default. "
                    "Use the 'pack' parameter to drill into a specific pack's internal skills. "
                    "Each list entry has fields: id (canonical, '<pack-id>:<skill-id>' for pack skills), name, description, "
                    "source (system/user/project), pack_id (string for pack skills, null for flat), "
                    "is_pack_bootstrap (bool). Bootstrap entries also have 'pack_size' (total internal skills count). "
                    "Returns a JSON string envelope: {tool, ok, error, result}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pack": {
                            "type": "string",
                            "description": (
                                "Pack ID to drill into internal skills only (including bootstrap). "
                                "When unset, returns flat skills plus one bootstrap entry per pack."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            "description": (
                "Tool name: skills_list\n"
                "Tool description: Lists available local skills with smart pack filtering.\n"
                "Default returns flat skills + pack bootstrap entries (one per pack).\n"
                "Use pack='<pack-id>' to drill into a specific pack.\n"
                "Each entry: id, name, description, source, pack_id, is_pack_bootstrap.\n"
                "Bootstrap entries also have pack_size.\n"
                "Pack skills have canonical id '<pack-id>:<skill-id>'.\n"
            ),
        },
        "skills_load": {
            "tool_fabric": _make_fabric(LoadSkillTool),
            "spec": {
                "type": "function",
                "name": "skills_load",
                "description": (
                    "Load a specific skill by its id — the FIRST step for any skill, before "
                    "skills_read_reference or skills_read_asset. Returns metadata, instructions, "
                    "and lists of available references and assets (load those by name afterwards if needed). "
                    "Returns a JSON string envelope: {tool, ok, skill_id, error, result}. On success, result.skill contains the skill detail."
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
                "- This is the FIRST tool to call for a skill — before any skills_read_reference or skills_read_asset.\n"
                "- The result contains the instructions text you should follow when using this skill.\n"
                "- It also returns which references and assets are available — load those by name afterwards if needed."
            ),
        },
        "skills_run_script": {
            "tool_fabric": _make_fabric(RunSkillScriptTool),
            "spec": {
                "type": "function",
                "name": "skills_run_script",
                "description": (
                    "Run a Python script that belongs to a specific skill. "
                    "Arguments are passed as JSON via stdin; the script must read and parse stdin "
                    "(argparse CLI positional/options are not auto-converted from args). "
                    "If script_name is omitted, scripts/main.py is used. "
                    "Returns a JSON string envelope: {tool, ok, script_ok, skill_id, script_name, args, error, result}. "
                    "ok=true means the wrapper launched the script; script_ok=false when result.exit_code != 0. "
                    "On success, the script output is inside result."
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
                            "type": "string",
                            "description": (
                                "Must decode to a JSON object (dictionary), not an array, scalar, or null. "
                                "When provided, pass script arguments as one JSON string "
                                "(this field is string-typed, not a nested object). "
                                "The script receives the parsed object on stdin and must read it explicitly "
                                "(keys are not converted to CLI flags or positionals). "
                                "Example: '{\"input\":\"hello\",\"limit\":3}'."
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
                "\"script_name\": {\"type\": \"string\"}, \"args\": {\"type\": \"string\"}}, "
                "\"required\": [\"skill_id\"], \"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Use this tool when a skill defines executable logic in its scripts/ directory.\n"
                "- If you don't know the script name, omit it to run scripts/main.py by default.\n"
                "- Provide script arguments in args only when the skill expects them.\n"
                "- args must decode to a JSON object; pass it as a string, not a nested object "
                "(e.g. '{\"input\":\"hello\"}').\n"
                "- Scripts must read stdin JSON themselves; args are not mapped to argparse CLI.\n"
                "- The result includes stdout, stderr, exit_code, and script_ok (false when exit_code != 0)."
            ),
        },
        "skills_read_reference": {
            "tool_fabric": _make_fabric(LoadReferenceTool),
            "spec": {
                "type": "function",
                "name": "skills_read_reference",
                "description": (
                    "Load ONE supplementary reference file of a skill by slug. "
                    "This does NOT load the skill itself — call skills_load(skill_id) FIRST to get "
                    "the skill's instructions and the exact reference names. "
                    "Use only slug values listed by skills_load (or returned in this tool's error); never invent a slug. "
                    "References live under the skill's references/ directory. "
                    "Returns a JSON string envelope: {tool, ok, skill_id, slug, error, result}. "
                    "On success, the reference content is inside result."
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
                "Tool name: skills_read_reference\n"
                "Tool description: Loads a reference document for a skill by slug.\n"
                "Input schema: {\"type\": \"object\", \"properties\": {\"skill_id\": {\"type\": \"string\"}, "
                "\"slug\": {\"type\": \"string\"}}, \"required\": [\"skill_id\", \"slug\"], "
                "\"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Prerequisite: call skills_load(skill_id) FIRST. This tool does NOT load the skill; "
                "it fetches one supplementary reference named in skills_load's result.\n"
                "- Use only slug values from skills_load's available references (or from this tool's error message); "
                "do not guess or invent slugs.\n"
                "- Slug corresponds to the relative path under references/ without file extension.\n"
                "- The result includes filename, content_type and textual content."
            ),
        },
        "skills_read_asset": {
            "tool_fabric": _make_fabric(LoadAssetTool),
            "spec": {
                "type": "function",
                "name": "skills_read_asset",
                "description": (
                    "Load ONE supplementary asset file of a skill by slug. "
                    "This does NOT load the skill itself — call skills_load(skill_id) FIRST to get "
                    "the skill's instructions and the exact asset names. "
                    "Use only slug values listed by skills_load (or returned in this tool's error); never invent a slug. "
                    "Assets live under the skill's assets/ directory. "
                    "Returns a JSON string envelope: {tool, ok, skill_id, slug, error, result}. "
                    "On success, the asset content is inside result."
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
                "Tool name: skills_read_asset\n"
                "Tool description: Loads an asset file for a skill by slug.\n"
                "Input schema: {\"type\": \"object\", \"properties\": {\"skill_id\": {\"type\": \"string\"}, "
                "\"slug\": {\"type\": \"string\"}}, \"required\": [\"skill_id\", \"slug\"], "
                "\"additionalProperties\": false}\n"
                "\n"
                "Usage:\n"
                "- Prerequisite: call skills_load(skill_id) FIRST. This tool does NOT load the skill; "
                "it fetches one supplementary asset named in skills_load's result.\n"
                "- Use only slug values from skills_load's available assets (or from this tool's error message); "
                "do not guess or invent slugs.\n"
                "- Slug corresponds to the relative path under assets/ without file extension.\n"
                "- In this minimal version all assets are treated as text."
            ),
        },
    }

    return tools


__all__ = [
    "get_skills_tools_data",
]

