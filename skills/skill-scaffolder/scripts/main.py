from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict


def _project_root() -> str:
    """
    Определить корень проекта относительно текущего файла.
    Ожидается структура:
      <project_root>/skills/skill-scaffolder/scripts/main.py
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "..", ".."))


def _default_skills_root() -> str:
    """
    Дефолтный путь к skills для генерации новых skills.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _read_args() -> Dict[str, Any]:
    """
    Прочитать JSON из stdin и вернуть dict.
    Пустой ввод трактуется как {}.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _print_error(f"Invalid JSON in stdin: {exc}")
        sys.exit(1)


def _print(result: Dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False), end="")


def _print_error(message: str) -> None:
    _print({"status": "error", "message": message})


def main() -> None:
    args = _read_args()

    skill_id = args.get("skill_id")
    if not isinstance(skill_id, str) or not skill_id.strip():
        _print_error("Missing or invalid 'skill_id' (must be non-empty string).")
        sys.exit(1)
    skill_id = skill_id.strip()

    skills_root = args.get("skills_root")
    if skills_root is not None and not isinstance(skills_root, str):
        _print_error("'skills_root' must be a string when provided.")
        sys.exit(1)

    if not skills_root:
        skills_root = _default_skills_root()

    # skills_root здесь может быть как абсолютным, так и относительным путём —
    # ответственность за корректность значения лежит на вызывающем коде.

    target_dir = os.path.abspath(os.path.join(skills_root, skill_id))

    if os.path.exists(target_dir):
        _print_error(f"Target skill directory already exists: {target_dir}")
        sys.exit(1)

    scripts_dir = os.path.join(target_dir, "scripts")
    references_dir = os.path.join(target_dir, "references")
    assets_dir = os.path.join(target_dir, "assets")

    try:
        os.makedirs(scripts_dir, exist_ok=True)
        os.makedirs(references_dir, exist_ok=True)
        os.makedirs(assets_dir, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        _print_error(f"Failed to create directories for new skill: {exc}")
        sys.exit(1)

    # Подготовка данных для шаблона SKILL.md
    skill_name = args.get("name")
    if not isinstance(skill_name, str) or not skill_name.strip():
        skill_name = skill_id
    else:
        skill_name = skill_name.strip()

    skill_description = args.get("description")
    if not isinstance(skill_description, str) or not skill_description.strip():
        skill_description = f"Skill {skill_id}."
    else:
        skill_description = skill_description.strip()

    # Загрузка шаблона SKILL.md из текущего skill-scaffolder
    scaffolder_assets_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets"))
    template_path = os.path.join(scaffolder_assets_dir, "SKILL_template.md")

    if os.path.isfile(template_path):
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template = f.read()
        except Exception as exc:  # noqa: BLE001
            _print_error(f"Failed to read SKILL_template.md: {exc}")
            sys.exit(1)
    else:
        # Запасной вариант, если по какой-то причине шаблон недоступен.
        template = (
            "---\n"
            f'id: "{skill_id}"\n'
            f'name: "{skill_name}"\n'
            f'description: "{skill_description}"\n'
            'version: "0.1.0"\n'
            "---\n\n"
            f"# {skill_name}\n\n"
            "Этот skill был создан автоматически.\n"
        )

    content = (
        template.replace("{{skill_id}}", skill_id)
        .replace("{{skill_name}}", skill_name)
        .replace("{{skill_description}}", skill_description)
    )

    skill_md_path = os.path.join(target_dir, "SKILL.md")

    try:
        with open(skill_md_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:  # noqa: BLE001
        _print_error(f"Failed to write SKILL.md for new skill: {exc}")
        sys.exit(1)

    _print(
        {
            "status": "ok",
            "message": "Skill skeleton created successfully.",
            "skill": {
                "id": skill_id,
                "name": skill_name,
                "description": skill_description,
                "root": target_dir,
                "skill_md": skill_md_path,
            },
        }
    )


if __name__ == "__main__":
    main()

