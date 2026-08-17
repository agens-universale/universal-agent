from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import FsBackend
from gc_core.bundled_assets import bundled_root

if TYPE_CHECKING:
    from gc_core.tools.skills.skill_arg_resolver import SkillCatalog

logger = logging.getLogger(__name__)


def _basename(path: str) -> str:
    return path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]


class AmbiguousSkillId(Exception):
    """
    Raised by SkillsRegistry._get_skill when a short skill_id matches more than
    one canonical id (e.g., "brainstorming" matches both "a:brainstorming" and
    "b:brainstorming"). Callers should surface candidates to the user/agent.
    """

    def __init__(self, short_id: str, candidates: list[str]) -> None:
        self.short_id = short_id
        self.candidates = list(candidates)
        super().__init__(
            f"Skill id '{short_id}' is ambiguous; candidates: {self.candidates}"
        )


def _system_skills_root() -> str:
    """
    Корень System-слоя — встроенные скиллы пакета.

    Через bundled_root() (тот же резолвер, что для prompts/ и config.default.yml):
    editable → <repo>/skills, wheel → site-packages/skills. Завязка на ../../..
    ломалась в wheel, где src/ схлопнут и уровень тот же промахивался мимо skills/.
    """
    return os.path.join(str(bundled_root()), "skills")


def _user_skills_root() -> str:
    """
    Корень User-слоя — общие пользовательские скиллы.
    Через gc_paths()["data"] / "skills" — на Linux/macOS это
    ~/.local/share/gc/skills/, на Windows ~/.gc/skills/.
    """
    from gc_core.config_modules.env_vars import gc_paths
    return str(gc_paths()["data"] / "skills")


def _project_skills_root() -> str:
    """
    Корень Project-слоя — скиллы конкретного проекта.
    <cwd>/.gc/skills/. Считается на каждом вызове (cwd может меняться).
    """
    from gc_core.config_modules.env_vars import project_gc_dir
    return str(project_gc_dir() / "skills")


def _parse_skill_markdown(backend: FsBackend, path: str) -> tuple[Dict[str, Any], str]:
    """
    Разобрать SKILL.md на frontmatter (YAML) и тело инструкции.
    Если frontmatter отсутствует или сломан — метаданные пустые, тело = весь файл.
    """
    try:
        text = backend.read(path)
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


def _walk_resource_dir(backend: FsBackend, base_dir: str) -> Dict[str, str]:
    """
    Построить словарь slug -> абсолютный путь для ресурсов в указанной папке.

    Правила slug:
    - slug = относительный путь от base_dir без расширения
    - разделитель каталогов всегда "/", независимо от ОС
    """
    mapping: Dict[str, str] = {}
    if not backend.is_dir(base_dir):
        return mapping

    for rel_path in backend.iter_files(base_dir):
        rel_no_ext, _ = os.path.splitext(rel_path)
        slug = rel_no_ext.replace("\\", "/")
        mapping[slug] = backend.join(base_dir, rel_path)
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


def _load_pack_manifest(
    backend: FsBackend, pack_dir: str, dir_name: str
) -> Dict[str, Any]:
    """
    Загрузить pack.yaml. Best-effort: при отсутствии файла или ошибке парсинга
    возвращает дефолты на основе имени директории.

    Возвращает dict с ключами:
      id, name, description, version, source_url, bootstrap_skill

    Поле в YAML называется `source` (URL), но в payload — `source_url`,
    чтобы не пересекаться с `source` слоя (system/user/project) в registry.
    """
    defaults: Dict[str, Any] = {
        "id": dir_name,
        "name": dir_name,
        "description": "",
        "version": None,
        "source_url": None,
        "bootstrap_skill": None,
    }
    manifest_path = backend.join(pack_dir, "pack.yaml")
    if not backend.is_file(manifest_path):
        return defaults

    try:
        raw = yaml.safe_load(backend.read(manifest_path)) or {}
    except (OSError, yaml.YAMLError):
        return defaults

    if not isinstance(raw, dict):
        return defaults

    pack_id = raw.get("id")
    if not isinstance(pack_id, str) or not pack_id.strip():
        pack_id = dir_name

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        name = pack_id

    description = raw.get("description")
    if not isinstance(description, str):
        description = ""

    version = raw.get("version")
    if isinstance(version, (int, float)):
        version = str(version)
    elif not isinstance(version, str):
        version = None

    source_url = raw.get("source")
    if not isinstance(source_url, str):
        source_url = None

    bootstrap_skill = raw.get("bootstrap_skill")
    if not isinstance(bootstrap_skill, str) or not bootstrap_skill.strip():
        bootstrap_skill = None

    return {
        "id": pack_id,
        "name": name,
        "description": description,
        "version": version,
        "source_url": source_url,
        "bootstrap_skill": bootstrap_skill,
    }


@dataclass
class Skill:
    id: str
    name: str
    description: str
    version: Optional[str]
    root_path: str
    instructions: str
    has_scripts: bool
    scripts: List[str] = field(default_factory=list)
    references: Dict[str, str] = field(default_factory=dict)  # slug -> path
    assets: Dict[str, str] = field(default_factory=dict)  # slug -> path
    source: str = "system"
    pack_id: Optional[str] = None

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
        backend: FsBackend | None = None,
        system_root: Optional[str] = None,
        user_root: Optional[str] = None,
        project_root: Optional[str] = None,
        disable_skills: Optional[List[str]] = None,
    ) -> None:
        self._backend = backend or LocalFsBackend()
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
            self._walk_packs_enabled = False
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
            self._walk_packs_enabled = True
        # Canonical id'ы скиллов / паков, исключаемых при загрузке.
        # Пакет (`pack-id`) убирает и bootstrap, и все inner. Flat-скилл
        # (`skill-id`) убирает один. Поведение list_skills() / load_skill()
        # неизменно — отключённого скилла «как будто нет».
        self._disabled: set[str] = set(disable_skills or [])
        self._skills_by_id: Dict[str, Skill] = {}
        self._packs_by_id: Dict[str, Dict[str, Any]] = {}
        self._loaded: bool = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_all_skills()
        self._loaded = True

    def reload(self) -> None:
        """Сбросить кэш скиллов — следующий доступ пересканирует диск.
        Нужно, чтобы созданный посреди прогона скилл стал виден без рестарта."""
        self._loaded = False

    def _load_all_skills(self) -> None:
        self._skills_by_id.clear()
        self._packs_by_id.clear()
        # Идём СНИЗУ ВВЕРХ: system → user → project.
        # Верхний слой перетирает нижний по skill_id (silent override).
        for source, root in (
            ("system", self._system_root),
            ("user", self._user_root),
            ("project", self._project_root),
        ):
            if not root or not self._backend.is_dir(root):
                continue
            for entry, is_dir in self._backend.listdir(root):
                entry_path = self._backend.join(root, entry)
                if not is_dir:
                    continue
                has_pack_yaml = self._backend.is_file(self._backend.join(entry_path, "pack.yaml"))
                has_skill_md = self._backend.is_file(self._backend.join(entry_path, "SKILL.md"))
                if has_pack_yaml and self._walk_packs_enabled:
                    if has_skill_md:
                        logger.warning(
                            "Directory '%s' has both pack.yaml and SKILL.md on its root. "
                            "Treating as a pack; the root-level SKILL.md is ignored.",
                            entry_path,
                        )
                    self._load_pack_contents(entry_path, dir_name=entry, source=source)
                elif has_skill_md:
                    skill = self._load_skill_dir(entry_path, dir_name=entry, source=source)
                    if skill is not None and skill.id not in self._disabled:
                        self._skills_by_id[skill.id] = skill
                # else: пусто, скип

    def _load_skill_dir(
        self,
        skill_dir: str,
        dir_name: str,
        source: str,
        pack_id: Optional[str] = None,
    ) -> Optional[Skill]:
        skill_md = self._backend.join(skill_dir, "SKILL.md")
        if not self._backend.is_file(skill_md):
            return None

        metadata, instructions = _parse_skill_markdown(self._backend, skill_md)

        meta_id = metadata.get("id") if isinstance(metadata.get("id"), str) else None
        skill_id_segment = meta_id or dir_name

        # Canonical = pack_id:skill_id для pack-скиллов, иначе плоский.
        canonical_id = (
            f"{pack_id}:{skill_id_segment}" if pack_id else skill_id_segment
        )

        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            name = skill_id_segment

        description = metadata.get("description")
        if not isinstance(description, str):
            description = ""

        version = metadata.get("version")
        if isinstance(version, (int, float)):
            version = str(version)
        elif not isinstance(version, str):
            version = None

        scripts_dir = self._backend.join(skill_dir, "scripts")
        scripts = sorted(self._backend.iter_files(scripts_dir)) if self._backend.is_dir(scripts_dir) else []
        has_scripts = bool(scripts)

        references_dir = self._backend.join(skill_dir, "references")
        assets_dir = self._backend.join(skill_dir, "assets")

        references = _walk_resource_dir(self._backend, references_dir)
        assets = _walk_resource_dir(self._backend, assets_dir)

        return Skill(
            id=canonical_id,
            name=name,
            description=description,
            version=version,
            root_path=skill_dir,
            instructions=instructions,
            has_scripts=has_scripts,
            scripts=scripts,
            references=references,
            assets=assets,
            source=source,
            pack_id=pack_id,
        )

    def _load_pack_contents(self, pack_dir: str, dir_name: str, source: str) -> None:
        """
        Загрузить пакет: pack.yaml → canonical pack_id, inner-скиллы из подпапок.
        Каждый inner-скилл регистрируется в self._skills_by_id с canonical
        '<pack-id>:<inner-id>'. Pack-manifest регистрируется в self._packs_by_id
        под manifest['id'].
        """
        manifest = _load_pack_manifest(self._backend, pack_dir, dir_name=dir_name)
        manifest["source"] = source
        canonical_pack_id = manifest["id"]
        # Disable целого пакета: ни manifest, ни inner-скиллы не регистрируем.
        if canonical_pack_id in self._disabled:
            return
        self._packs_by_id[canonical_pack_id] = manifest

        for inner_entry, is_dir in self._backend.listdir(pack_dir):
            inner_path = self._backend.join(pack_dir, inner_entry)
            if not is_dir:
                continue
            if not self._backend.is_file(self._backend.join(inner_path, "SKILL.md")):
                continue
            skill = self._load_skill_dir(
                inner_path,
                dir_name=inner_entry,
                source=source,
                pack_id=canonical_pack_id,
            )
            if skill is not None and skill.id not in self._disabled:
                self._skills_by_id[skill.id] = skill

    # Публичный API реестра

    def list_skills(
        self,
        *,
        pack: Optional[str] = None,
        include_all: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Вернуть список метаданных скиллов.

        - default (pack=None, include_all=False): flat-скиллы + bootstrap-скилл
          каждого пакета. Internal-скиллы скрыты.
        - pack="<pack-id>": только internal-скиллы указанного пакета.
        - include_all=True: все скиллы реестра без фильтрации.

        Если оба заданы — pack имеет приоритет.
        """
        self.reload()
        self._ensure_loaded()

        bootstrap_ids: Dict[str, str] = {}  # pack_id -> canonical bootstrap skill id
        pack_sizes: Dict[str, int] = {}
        for pid, manifest in self._packs_by_id.items():
            pack_sizes[pid] = sum(
                1 for s in self._skills_by_id.values() if s.pack_id == pid
            )
            bs = manifest.get("bootstrap_skill")
            if not (isinstance(bs, str) and bs.strip()):
                continue
            # 1) Direct canonical lookup: bootstrap_skill matches frontmatter id.
            bootstrap_canonical = f"{pid}:{bs}"
            if bootstrap_canonical in self._skills_by_id:
                bootstrap_ids[pid] = bootstrap_canonical
                continue
            # 2) Fallback: bootstrap_skill matches the folder name of an inner skill.
            # Allows pack authors to write the inner skill's directory name (intuitive)
            # even when the SKILL.md frontmatter overrides id. Without this, a folder
            # named "entry/" with frontmatter id "actual-entry" plus
            # bootstrap_skill: entry in pack.yaml would silently make the pack invisible.
            for skill in self._skills_by_id.values():
                if skill.pack_id == pid and _basename(skill.root_path) == bs:
                    bootstrap_ids[pid] = skill.id
                    break

        result: List[Dict[str, Any]] = []
        for skill in self._skills_by_id.values():
            # Решаем, попадает ли скилл в фильтр
            if pack is not None:
                if skill.pack_id != pack:
                    continue
            elif include_all:
                pass  # все
            else:
                # default: flat + bootstraps
                if skill.pack_id is not None:
                    # internal — показываем только если это bootstrap
                    if bootstrap_ids.get(skill.pack_id) != skill.id:
                        continue

            entry: Dict[str, Any] = {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "root_path": skill.root_path,
                "location": skill.root_path,
                "scripts": skill.scripts,
                "pack_id": skill.pack_id,
                "is_pack_bootstrap": False,
            }
            if skill.pack_id and bootstrap_ids.get(skill.pack_id) == skill.id:
                entry["is_pack_bootstrap"] = True
                entry["pack_size"] = pack_sizes[skill.pack_id]
            result.append(entry)
        return result

    def _resolve_pack_bootstrap(self, pack_id: str) -> Optional[Skill]:
        """Resolve a bare pack-id to that pack's bootstrap (entry) skill.

        Mirrors the bootstrap resolution in ``list_skills``: try the canonical
        ``<pack>:<bootstrap_skill>`` id, then fall back to an inner skill whose
        folder name matches ``bootstrap_skill``. Returns ``None`` when the id is
        not a known pack or the pack declares no resolvable bootstrap.
        """
        normalized = pack_id.replace("_", "-")
        canonical_pid = (
            pack_id if pack_id in self._packs_by_id
            else normalized if normalized in self._packs_by_id
            else None
        )
        if canonical_pid is None:
            return None
        bs = self._packs_by_id[canonical_pid].get("bootstrap_skill")
        if not (isinstance(bs, str) and bs.strip()):
            return None
        direct = self._skills_by_id.get(f"{canonical_pid}:{bs}")
        if direct is not None:
            return direct
        for skill in self._skills_by_id.values():
            if skill.pack_id == canonical_pid and _basename(skill.root_path) == bs:
                return skill
        return None

    def _get_skill(self, skill_id: str) -> Skill:
        try:
            return self._lookup_skill(skill_id)
        except KeyError:
            # Промах id — возможно, скилл создан только что: один rescan и
            # повтор. Второго reload в этом вызове нет → цикла нет.
            # AmbiguousSkillId сюда не попадает (не KeyError) — не ретраим.
            self.reload()
            return self._lookup_skill(skill_id)

    def _candidate_ids_for(self, skill_id: str) -> List[str]:
        """Кандидаты по совпадению хвоста — для подсказки в not-found ошибке."""
        target = skill_id.split(":")[-1].replace("_", "-")
        cands: List[str] = []
        for canonical in self._skills_by_id:
            tail = canonical.split(":", 1)[1] if ":" in canonical else canonical
            if target and (target in tail or tail in target):
                cands.append(canonical)
        return sorted(cands)

    def _not_found_message(self, skill_id: str) -> str:
        cands = self._candidate_ids_for(skill_id)
        base = f"Skill '{skill_id}' not found"
        return f"{base}. Did you mean: {cands}?" if cands else base

    def _lookup_skill(self, skill_id: str) -> Skill:
        self._ensure_loaded()
        if not isinstance(skill_id, str):
            raise KeyError(f"Skill '{skill_id}' not found")
        skill_id = skill_id.strip()
        if not skill_id:
            raise KeyError("Skill id is empty")

        # 1) Прямой canonical lookup (если содержит ':').
        if ":" in skill_id:
            skill = self._skills_by_id.get(skill_id)
            if skill is None:
                # Нормализация только skill-сегмента (часть после ':').
                pack_part, _, tail = skill_id.partition(":")
                normalized_tail = tail.replace("_", "-")
                skill = self._skills_by_id.get(f"{pack_part}:{normalized_tail}")
                if skill is None and pack_part in (tail, normalized_tail):
                    # Удвоённый namespace (pack==skill, напр. 'x:x') — модель угадала
                    # форму pack:skill для flat-скилла. Безопасный bare-lookup по хвосту.
                    skill = (self._skills_by_id.get(tail)
                             or self._skills_by_id.get(normalized_tail))
            if skill is None:
                raise KeyError(self._not_found_message(skill_id))
            return skill

        # 2) Short alias — собрать кандидатов по skill-сегменту.
        normalized = skill_id.replace("_", "-")
        candidates: List[Skill] = []
        for canonical, s in self._skills_by_id.items():
            tail = canonical.split(":", 1)[1] if ":" in canonical else canonical
            if tail == normalized or tail.replace("-", "_") == normalized.replace("-", "_"):
                candidates.append(s)

        if len(candidates) == 0:
            # Bare pack-id (e.g. "science-article-creator") → resolve to the
            # pack's bootstrap skill so an agent that loads a pack by its bare
            # id gets the entry skill instead of "not found" (hop1). Last resort:
            # only reached when no flat/inner skill matched the alias above.
            bootstrap = self._resolve_pack_bootstrap(skill_id)
            if bootstrap is not None:
                return bootstrap
            raise KeyError(self._not_found_message(skill_id))
        if len(candidates) > 1:
            raise AmbiguousSkillId(
                short_id=skill_id,
                candidates=sorted(s.id for s in candidates),
            )
        return candidates[0]

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
            "scripts": skill.scripts,
            "references": skill.references_slugs,
            "assets": skill.assets_slugs,
            "source": skill.source,
            "root_path": skill.root_path,
            "location": skill.root_path,
            "pack_id": skill.pack_id,
        }

    def skill_catalog(self) -> "SkillCatalog":
        """Read-only snapshot for the CLI -s resolver (skill_arg_resolver).

        Built from the public list_skills(include_all=True); does not expose
        private internals. Local import avoids import-time coupling between
        the registry and the pure resolver module.
        """
        from gc_core.tools.skills.skill_arg_resolver import SkillCatalog

        entries = self.list_skills(include_all=True)
        canonical = frozenset(e["id"] for e in entries)
        bare: dict[str, list[str]] = {}
        for e in entries:
            sid = e["id"]
            tail = sid.split(":", 1)[1] if ":" in sid else sid
            bare.setdefault(tail, []).append(sid)
        pack_bootstrap = {
            e["pack_id"]: e["id"]
            for e in entries
            if e.get("is_pack_bootstrap") and e.get("pack_id")
        }
        return SkillCatalog(
            canonical_ids=canonical,
            bare_to_canonical={k: tuple(sorted(v)) for k, v in bare.items()},
            pack_bootstrap=pack_bootstrap,
        )

    def load_reference(self, skill_id: str, slug: str) -> Dict[str, Any]:
        """
        Загрузить reference по slug.
        """
        skill = self._get_skill(skill_id)
        path = skill.references.get(slug)
        if not path:
            available = ", ".join(sorted(skill.references)) or "(no references)"
            raise KeyError(
                f"Reference '{slug}' not found for skill '{skill_id}'. "
                f"Available references: {available}"
            )

        content = self._backend.read(path)

        filename = _basename(path)
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
            available = ", ".join(sorted(skill.assets)) or "(no assets)"
            raise KeyError(
                f"Asset '{slug}' not found for skill '{skill_id}'. "
                f"Available assets: {available}"
            )

        content = self._backend.read(path)

        filename = _basename(path)
        content_type = _guess_content_type(filename)

        return {
            "skill_id": skill.id,
            "slug": slug,
            "filename": filename,
            "content_type": content_type,
            "content": content,
        }

    def resolve_script(self, skill_id: str, script_name: str) -> Dict[str, Any]:
        """Resolve an explicitly named script on the backend filesystem."""
        skill = self._get_skill(skill_id)
        available_scripts = skill.scripts
        normalized = (script_name or "").strip().replace("\\", "/")
        if normalized.startswith("scripts/"):
            normalized = normalized[len("scripts/"):]
        if not normalized or normalized not in available_scripts:
            raise FileNotFoundError(
                f"Script '{script_name}' not found for skill '{skill_id}'. "
                f"Available scripts: {available_scripts}"
            )
        path = self._backend.join(self._backend.join(skill.root_path, "scripts"), normalized)
        if not self._backend.is_file(path):
            raise FileNotFoundError(
                f"Script '{script_name}' not found for skill '{skill_id}'. "
                f"Available scripts: {available_scripts}"
            )
        return {
            "path": path,
            "script_name": normalized,
            "available_scripts": available_scripts,
        }


def get_skills_registry(
    config_dict: Optional[Dict[str, Any]] = None,
    *,
    backend: FsBackend | None = None,
) -> SkillsRegistry:
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
    disable_skills: List[str] = []
    if config_dict is not None:
        raw = config_dict.get("skills_data_path")
        if isinstance(raw, str):
            path = raw.strip()
            if path:
                skills_root = path
        raw_disabled = config_dict.get("disable_skills")
        if isinstance(raw_disabled, list):
            disable_skills = [
                str(item).strip()
                for item in raw_disabled
                if isinstance(item, (str, int)) and str(item).strip()
            ]

    if skills_root is not None:
        return SkillsRegistry(
            skills_root=skills_root, disable_skills=disable_skills, backend=backend
        )
    return SkillsRegistry(disable_skills=disable_skills, backend=backend)


__all__ = [
    "AmbiguousSkillId",
    "Skill",
    "SkillsRegistry",
    "get_skills_registry",
]
