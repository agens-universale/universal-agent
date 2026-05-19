from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


def _project_root() -> str:
    """
    Определяет корень проекта относительно текущего файла.
    Ожидается структура: project_root/src/tools/skills/registry.py
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _system_skills_root() -> str:
    """
    Корень System-слоя — встроенные скиллы пакета.

    По умолчанию: <project_root>/skills.
    """
    return os.path.join(_project_root(), "skills")


def _user_skills_root() -> str:
    """
    Корень User-слоя — общие пользовательские скиллы.
    Через gc_paths()["data"] / "skills" — на Linux/macOS это
    ~/.local/share/gc/skills/, на Windows ~/.gc/skills/.
    """
    from config_modules.env_vars import gc_paths
    return str(gc_paths()["data"] / "skills")


def _project_skills_root() -> str:
    """
    Корень Project-слоя — скиллы конкретного проекта.
    <cwd>/.gc/skills/. Считается на каждом вызове (cwd может меняться).
    """
    from config_modules.env_vars import project_gc_dir
    return str(project_gc_dir() / "skills")


def _parse_skill_markdown(path: str) -> tuple[Dict[str, Any], str]:
    """
    Разобрать SKILL.md на frontmatter (YAML) и тело инструкции.
    Если frontmatter отсутствует или сломан — метаданные пустые, тело = весь файл.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, ""

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text

    fm_lines: List[str] = []
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        fm_lines.append(lines[i])
        i += 1

    # Не нашли закрывающий --- — считаем, что frontmatter нет
    if i >= len(lines) or lines[i].strip() != "---":
        return {}, text

    fm_text = "".join(fm_lines)
    body = "".join(lines[i + 1 :])

    try:
        metadata = yaml.safe_load(fm_text) or {}
        if not isinstance(metadata, dict):
            metadata = {}
    except Exception:
        metadata = {}

    return metadata, body


def _walk_resource_dir(base_dir: str) -> Dict[str, str]:
    """
    Построить словарь slug -> абсолютный путь для ресурсов в указанной папке.

    Правила slug:
    - slug = относительный путь от base_dir без расширения
    - разделитель каталогов всегда "/", независимо от ОС
    """
    mapping: Dict[str, str] = {}
    if not os.path.isdir(base_dir):
        return mapping

    for root, _, files in os.walk(base_dir):
        for filename in files:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, base_dir)
            rel_no_ext, _ = os.path.splitext(rel_path)
            slug = rel_no_ext.replace(os.sep, "/")
            mapping[slug] = full_path
    return mapping


def _guess_content_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".md", ".markdown"):
        return "text/markdown"
    if ext in (".txt",):
        return "text/plain"
    if ext in (".json",):
        return "application/json"
    if ext in (".yml", ".yaml"):
        return "text/x-yaml"
    return "text/plain"


@dataclass
class Skill:
    id: str
    name: str
    description: str
    version: Optional[str]
    root_path: str
    instructions: str
    has_scripts: bool
    references: Dict[str, str] = field(default_factory=dict)  # slug -> path
    assets: Dict[str, str] = field(default_factory=dict)  # slug -> path
    source: str = "system"

    @property
    def references_slugs(self) -> List[str]:
        return sorted(self.references.keys())

    @property
    def assets_slugs(self) -> List[str]:
        return sorted(self.assets.keys())


class SkillsRegistry:
    """
    Простой реестр скиллов, основанный только на файловой структуре skills_data/.
    """

    def __init__(
        self,
        skills_root: Optional[str] = None,    # legacy single-source
        *,
        system_root: Optional[str] = None,
        user_root: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> None:
        # Legacy: если передан skills_root и никаких новых — режим single-source.
        # В этом режиме все скиллы помечаются как source="user".
        if (
            skills_root is not None
            and system_root is None
            and user_root is None
            and project_root is None
        ):
            self._system_root = None
            self._user_root = skills_root
            self._project_root = None
        else:
            self._system_root = (
                system_root if system_root is not None else _system_skills_root()
            )
            self._user_root = (
                user_root if user_root is not None else _user_skills_root()
            )
            self._project_root = (
                project_root if project_root is not None else _project_skills_root()
            )
        self._skills_by_id: Dict[str, Skill] = {}
        self._loaded: bool = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_all_skills()
        self._loaded = True

    def _load_all_skills(self) -> None:
        self._skills_by_id.clear()
        # Идём СНИЗУ ВВЕРХ: system → user → project.
        # Верхний слой перетирает нижний по skill_id (silent override).
        for source, root in (
            ("system", self._system_root),
            ("user", self._user_root),
            ("project", self._project_root),
        ):
            if not root or not os.path.isdir(root):
                continue
            for entry in os.listdir(root):
                skill_dir = os.path.join(root, entry)
                if not os.path.isdir(skill_dir):
                    continue
                skill = self._load_skill_dir(skill_dir, dir_name=entry, source=source)
                if skill is not None:
                    self._skills_by_id[skill.id] = skill

    def _load_skill_dir(self, skill_dir: str, dir_name: str, source: str) -> Optional[Skill]:
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            return None

        metadata, instructions = _parse_skill_markdown(skill_md)

        meta_id = metadata.get("id") if isinstance(metadata.get("id"), str) else None
        skill_id = meta_id or dir_name

        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            name = skill_id

        description = metadata.get("description")
        if not isinstance(description, str):
            description = ""

        version = metadata.get("version")
        if isinstance(version, (int, float)):
            version = str(version)
        elif not isinstance(version, str):
            version = None

        scripts_dir = os.path.join(skill_dir, "scripts")
        has_scripts = os.path.isdir(scripts_dir)

        references_dir = os.path.join(skill_dir, "references")
        assets_dir = os.path.join(skill_dir, "assets")

        references = _walk_resource_dir(references_dir)
        assets = _walk_resource_dir(assets_dir)

        return Skill(
            id=skill_id,
            name=name,
            description=description,
            version=version,
            root_path=skill_dir,
            instructions=instructions,
            has_scripts=has_scripts,
            references=references,
            assets=assets,
            source=source,
        )

    # Публичный API реестра

    def list_skills(self) -> List[Dict[str, Any]]:
        """
        Вернуть краткое описание всех доступных скиллов.
        """
        self._ensure_loaded()
        result: List[Dict[str, Any]] = []
        for skill in self._skills_by_id.values():
            result.append(
                {
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                }
            )
        return result

    def _get_skill(self, skill_id: str) -> Skill:
        self._ensure_loaded()
        skill = self._skills_by_id.get(skill_id)
        if not skill and isinstance(skill_id, str):
            # Backward-compatible aliasing: accept snake_case requests for
            # kebab-case skill ids and vice versa.
            normalized = skill_id.strip().replace("_", "-")
            skill = self._skills_by_id.get(normalized)
            if not skill:
                normalized_alt = skill_id.strip().replace("-", "_")
                for candidate in self._skills_by_id.values():
                    if candidate.id.replace("-", "_") == normalized_alt:
                        skill = candidate
                        break
        if not skill:
            raise KeyError(f"Skill '{skill_id}' not found")
        return skill

    def load_skill_detail(self, skill_id: str) -> Dict[str, Any]:
        """
        Подробное описание скилла, включая инструкции и списки slug-ов ресурсов.
        """
        skill = self._get_skill(skill_id)
        return {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "instructions": skill.instructions,
            "has_scripts": skill.has_scripts,
            "references": skill.references_slugs,
            "assets": skill.assets_slugs,
            "source": skill.source,
        }

    def load_reference(self, skill_id: str, slug: str) -> Dict[str, Any]:
        """
        Загрузить reference по slug.
        """
        skill = self._get_skill(skill_id)
        path = skill.references.get(slug)
        if not path:
            raise KeyError(f"Reference '{slug}' not found for skill '{skill_id}'")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        filename = os.path.basename(path)
        content_type = _guess_content_type(filename)

        return {
            "skill_id": skill.id,
            "slug": slug,
            "filename": filename,
            "content_type": content_type,
            "content": content,
        }

    def load_asset(self, skill_id: str, slug: str) -> Dict[str, Any]:
        """
        Загрузить asset по slug.
        В минимальной версии считаем, что все ассеты текстовые.
        """
        skill = self._get_skill(skill_id)
        path = skill.assets.get(slug)
        if not path:
            raise KeyError(f"Asset '{slug}' not found for skill '{skill_id}'")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        filename = os.path.basename(path)
        content_type = _guess_content_type(filename)

        return {
            "skill_id": skill.id,
            "slug": slug,
            "filename": filename,
            "content_type": content_type,
            "content": content,
        }

    def run_script(
        self,
        skill_id: str,
        script_name: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """
        Выполнить python-скрипт скилла.

        - Если script_name не указан — пробуем scripts/main.py.
        - Аргументы передаются в stdin как JSON.
        """
        import subprocess

        skill = self._get_skill(skill_id)
        scripts_dir = os.path.join(skill.root_path, "scripts")

        if not os.path.isdir(scripts_dir):
            raise FileNotFoundError(f"Skill '{skill_id}' has no scripts directory")

        if not script_name:
            script_name = "main.py"

        script_path = os.path.join(scripts_dir, script_name)
        if not os.path.isfile(script_path):
            raise FileNotFoundError(
                f"Script '{script_name}' not found for skill '{skill_id}'"
            )

        python_executable = sys.executable or "python"
        cmd = [python_executable, script_path]

        input_data: Optional[bytes] = None
        if args is not None:
            try:
                json_payload = json.dumps(args, ensure_ascii=False)
            except TypeError as exc:
                raise ValueError(f"Failed to serialize args to JSON: {exc}") from exc
            input_data = json_payload.encode("utf-8")

        completed = subprocess.run(
            cmd,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

        stdout_text = completed.stdout.decode("utf-8", errors="replace")
        stderr_text = completed.stderr.decode("utf-8", errors="replace")

        return {
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": completed.returncode,
        }


def get_skills_registry(config_dict: Optional[Dict[str, Any]] = None) -> SkillsRegistry:
    """
    Фабрика реестра скиллов.

    Поведение:
    - Если config_dict содержит непустой "skills_data_path" — single-source режим:
      registry видит ТОЛЬКО эту папку (escape-hatch для production-сценариев,
      curated team set, тестов / CI с фикстурами). Все скиллы помечаются source="user".
    - Иначе — three-layer режим: System (<repo>/skills/) + User (gc_paths()["data"]/skills)
      + Project (<cwd>/.gc/skills/), приоритет project > user > system.
    """
    skills_root: Optional[str] = None
    if config_dict is not None:
        raw = config_dict.get("skills_data_path")
        if isinstance(raw, str):
            path = raw.strip()
            if path:
                skills_root = path

    if skills_root is not None:
        return SkillsRegistry(skills_root=skills_root)
    return SkillsRegistry()


__all__ = [
    "Skill",
    "SkillsRegistry",
    "get_skills_registry",
]

