#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable skill-<id>.zip archive of a skill folder

Usage:
    python -m scripts.package_skill <path/to/skill-folder> [output-directory]

Example:
    python -m scripts.package_skill skills/my-skill
    python -m scripts.package_skill skills/my-skill ./dist
"""

import fnmatch
import sys
import zipfile
from datetime import date as _date
from pathlib import Path

# Force UTF-8 stdout/stderr — emoji в print()/error messages иначе ломают
# вывод на Windows (cp1251 default) с UnicodeEncodeError. Python 3.7+ API.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        # AttributeError: старый Python без reconfigure; ValueError:
        # stream уже не-text (e.g. в pytest capture). Не критично.
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quick_validate import validate_skill

# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules", "_dev"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals", ".eval_workspaces"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging. rel_path is relative to skill_path."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # ROOT_EXCLUDE_DIRS applies to top-level only.
    if parts and parts[0] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


# Required artifacts in _dev/ when self-check flow was started.
# intent_brief.md служит триггером: его наличие = flow был запущен,
# значит должны быть и остальные. Если intent_brief нет — gate skipped.
REQUIRED_DEV_ARTIFACTS = [
    "intent_brief.md",
    "completion_report.md",
    "examples/prompt_01.md",
    "examples/prompt_02.md",
    "examples/prompt_01_output.md",
    "examples/prompt_02_output.md",
]


def _validate_dev_artifacts(skill_path: Path) -> list[str]:
    """Return list of missing required artifacts (relative paths).

    Returns empty list if validation passes OR _dev/intent_brief.md does not
    exist (flow wasn't started — gate skipped, упаковка пройдёт без demo/).
    """
    dev_dir = skill_path / "_dev"
    if not (dev_dir / "intent_brief.md").exists():
        return []
    missing = []
    for rel in REQUIRED_DEV_ARTIFACTS:
        if not (dev_dir / rel).exists():
            missing.append(rel)
    return missing


def _extract_frontmatter(skill_path: Path) -> dict:
    """Return frontmatter dict from SKILL.md, or empty dict on parse error."""
    skill_md = skill_path / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    import re

    import yaml
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _extract_skill_id(skill_path: Path) -> str:
    """Extract `id` from SKILL.md frontmatter, fallback to folder name."""
    fm = _extract_frontmatter(skill_path)
    return str(fm.get("id") or skill_path.name)


def _system_skills_root() -> Path:
    """Каталог `skills/`, которому принадлежит этот скрипт.

    Скрипт лежит в `<skills>/skill-creator/scripts/`, поэтому родитель второго
    уровня — сам каталог скиллов. Раньше считали от корня репозитория
    (`parents[3] / "skills"`), но после разбиения на пакеты ассеты уехали в
    `packages/core/assets/`, и фиксированная глубина перестала совпадать.
    """
    return Path(__file__).resolve().parents[2]


def _repo_root():
    """Корень НАШЕЙ рабочей копии, если скрипт запущен из неё, иначе None.

    Маркер не свой — спрашиваем ядро. `gc_core.bundled_assets.repo_root_from`
    ищет `packages/core/pyproject.toml` со строкой `name = "gc-core"`; ядро
    пришло к такому маркеру ровно потому, что прежний («у родителя есть
    packages/ или .git») объявлял нашим корнем ЛЮБОЕ чужое монорепо. Здесь
    стоял именно прежний, слабый: разбиение на пакеты сдвинуло скрипт на
    уровень глубже, фиксированную глубину заменили маркерным поиском и взяли
    тот, от которого ядро уже ушло. Своей копии маркера тут нет намеренно:
    `tests/test_repo_root_marker.py` стережёт, что копий ровно три и каждая —
    там, где ядро физически недоступно.

    Ядра может не быть на пути: скрипт запускают через `sys.executable`, но в
    песочнице бэкенд подставляет голый `python3`. Тогда честный None — корня
    рабочей копии мы не знаем, и project-слой определяется по cwd.
    """
    try:
        from gc_core.bundled_assets import repo_root_from  # type: ignore
    except Exception:
        return None
    return repo_root_from(Path(__file__))


def _detect_source_layer(skill_path: Path) -> str:
    """
    Detect skill source layer by inspecting the skill path against standard
    project/user/system locations.

    Order:
    1. Under <cwd>/.gc/skills/ → 'project'
    2. Under <user-skills-path> (gc_paths or hardcoded fallback) → 'user'
    3. Under the skills root this script belongs to → 'system'
    4. Otherwise → 'unknown'
    """
    skill_resolved = skill_path.resolve()

    # 1. Project — сначала cwd, затем корень рабочей копии (если скрипт запущен
    # из неё). cwd покрывает обычный случай «запустили из корня проекта»,
    # корень рабочей копии — запуск из подкаталога.
    _repo = _repo_root()
    project_candidates = [Path.cwd().resolve() / ".gc" / "skills"]
    if _repo is not None:
        project_candidates.append(_repo / ".gc" / "skills")
    for project_root in project_candidates:
        try:
            skill_resolved.relative_to(project_root.resolve())
            return "project"
        except (ValueError, OSError):
            continue

    # 2. User layer — try gc_paths() if available
    user_skills_path = None
    try:
        # gc_paths() живёт в ядре и импортируется штатно: в колесе gc_core лежит
        # в site-packages, в рабочей копии его кладёт editable-установка.
        # Раньше перед этим импортом сюда доставляли на sys.path все
        # `<корень>/packages/*/src`. В колесе это не срабатывало никогда (корень
        # считался фиксированной глубиной и упирался в site-packages, где нет ни
        # packages/, ни src/), а после разбиения глубину заменили слабым
        # маркером — и доставка ожила в ЧУЖОМ дереве: колесо в .venv чужого
        # репозитория молча клало его `packages/*/src` в НАЧАЛО sys.path того
        # процесса, который агент запускает от имени пользователя. Убрано: там,
        # где ядро не установлено, честнее уйти в fallback ниже, чем угадывать
        # исходники по соседнему дереву.
        from gc_core.config_modules.env_vars import gc_paths  # type: ignore
        user_skills_path = (gc_paths()["data"] / "skills").resolve()
    except Exception:
        # Hardcoded fallback per-OS
        home = Path.home()
        if sys.platform.startswith(("linux", "darwin")):
            user_skills_path = (home / ".local" / "share" / "gc" / "skills").resolve()
        else:
            user_skills_path = (home / ".gc" / "skills").resolve()

    if user_skills_path is not None:
        try:
            skill_resolved.relative_to(user_skills_path)
            return "user"
        except ValueError:
            pass

    # 3. System — скиллы, приехавшие с дистрибутивом. Раньше лежали в
    #    <репо>/skills/, после переезда ассетов — в packages/core/assets/skills/
    #    (в колесе: <site-packages>/gc_core/assets/skills/).
    repo_skills = _system_skills_root()
    try:
        skill_resolved.relative_to(repo_skills.resolve())
        return "system"
    except ValueError:
        pass

    return "unknown"


def _generate_readme(
    skill_path: Path,
    skill_id: str,
    source_layer: str,
    has_demo: bool,
) -> str:
    """Generate README.md content for archive root."""
    fm = _extract_frontmatter(skill_path)
    version = fm.get("version") or "—"
    description = (fm.get("description") or "").strip().replace("\n  ", " ").replace("\n", " ")
    today = _date.today().isoformat()

    demo_section = ""
    if has_demo:
        demo_section = (
            "- `demo/` — артефакты разработки скилла (intent_brief, "
            "text_review_report, self_check_report, completion_report, "
            "тестовые промпты и outputs).\n"
        )

    return (
        f"# Skill: {skill_id}\n\n"
        f"**Краткое описание:**\n"
        f"> {description}\n\n"
        f"**Создан / упакован:** {today}\n"
        f"**Версия:** {version}\n"
        f"**Слой-источник:** {source_layer}\n"
        f"**Путь источника:** `{skill_path}`\n\n"
        f"## Как установить\n\n"
        f"Скопируй папку `{skill_id}/` из этого архива в один из путей:\n\n"
        f"- `<your-project>/.gc/skills/{skill_id}/` — только для одного проекта\n"
        f"- `~/.gc/skills/{skill_id}/` (Windows) или\n"
        f"  `~/.local/share/gc/skills/{skill_id}/` (Linux/Mac) — для всех проектов\n\n"
        f"После копирования скилл подхватится агентом автоматически — "
        f"перезапуск сессии не нужен.\n\n"
        f"## Что в архиве\n\n"
        f"- `{skill_id}/` — сам скилл, копируется как есть.\n"
        f"{demo_section}"
    )


def _skill_folder_ready(skill_path):
    """Папка скилла существует, содержит SKILL.md и проходит валидацию (иначе печатает причину)."""
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return False
    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return False
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return False
    # Run validation before packaging
    print("🔍 Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return False
    print(f"✅ {message}\n")
    return True


def _report_incomplete_dev(missing):
    print("❌ Refusing to package: _dev/ is incomplete.\n", file=sys.stderr)
    print(
        "Found:    intent_brief.md (self-check flow was started)",
        file=sys.stderr,
    )
    print("Missing:", file=sys.stderr)
    for rel in missing:
        print(f"  - _dev/{rel}", file=sys.stderr)
    print("\nRequired artifacts when _dev/ exists:", file=sys.stderr)
    for rel in REQUIRED_DEV_ARTIFACTS:
        print(f"  - _dev/{rel}", file=sys.stderr)
    print(
        "\nДоделай self-check flow или удали _dev/intent_brief.md\n"
        "(тогда упаковка пройдёт без demo/).",
        file=sys.stderr,
    )


def _add_tree(zipf, root, arc_root, *, skip_label):
    """Добавить файлы дерева root в архив под arc_root/..., пропуская исключённые."""
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(root)
        if should_exclude(rel):
            print(f"  {skip_label}: {rel}")
            continue
        arcname = str(arc_root / rel).replace("\\", "/")
        zipf.write(file_path, arcname)
        print(f"  Added: {arcname}")


def _cleanup_dev_dir(dev_dir):
    try:
        import shutil
        shutil.rmtree(dev_dir)
        print(f"  Cleaned up: {dev_dir}")
    except OSError as e:
        print(
            f"⚠️  Warning: _dev/ remains at {dev_dir}; clean up manually if needed. ({e})",
            file=sys.stderr,
        )


def package_skill(skill_path, output_dir=None, source_layer=None):
    """
    Package a skill folder into a skill-<id>.zip archive.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the archive (defaults to current directory)
        source_layer: Optional explicit source layer ("system" | "user" | "project");
            filled into the generated README. Defaults to "unknown".

    Returns:
        Path to the created skill-<id>.zip archive, or None if error
    """
    skill_path = Path(skill_path).resolve()

    # Validate skill folder exists
    if not _skill_folder_ready(skill_path):
        return None

    # Validate _dev/ artifacts (if flow was started)
    missing = _validate_dev_artifacts(skill_path)
    if missing:
        _report_incomplete_dev(missing)
        return None

    # Determine output location
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_id = _extract_skill_id(skill_path)
    if source_layer is None:
        source_layer = _detect_source_layer(skill_path)
    skill_filename = output_path / f"skill-{skill_id}.zip"

    dev_dir = skill_path / "_dev"
    has_demo = dev_dir.is_dir() and any(dev_dir.iterdir())

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            # Generate README.md (always at archive root, before skill content)
            readme_content = _generate_readme(
                skill_path=skill_path,
                skill_id=skill_id,
                source_layer=source_layer or "unknown",
                has_demo=has_demo,
            )
            zipf.writestr("README.md", readme_content)

            _add_tree(zipf, skill_path, Path(skill_id), skip_label="Skipped")
            # Bundle _dev/ as demo/ in archive
            if dev_dir.is_dir():
                _add_tree(zipf, dev_dir, Path("demo"), skip_label="Skipped (demo)")

        print(f"\nSuccessfully packaged skill to: {skill_filename}")

        # Cleanup _dev/ after successful packaging (transient workspace)
        if dev_dir.is_dir():
            _cleanup_dev_dir(dev_dir)

        return skill_filename

    except Exception as e:
        print(f"Error creating skill archive: {e}")
        return None


def _resolve_invocation() -> tuple[str, str | None, str | None]:
    """Return (skill_path, output_dir, source_layer) from stdin JSON or CLI."""
    import argparse
    import json

    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"Error: invalid stdin JSON: {exc}", file=sys.stderr)
                sys.exit(2)
            if not isinstance(payload, dict):
                print("Error: stdin JSON must be an object", file=sys.stderr)
                sys.exit(2)
            skill_path = payload.get("skill_path")
            if not skill_path:
                print("Error: 'skill_path' is required in stdin JSON", file=sys.stderr)
                sys.exit(2)
            return (
                str(skill_path),
                payload.get("output_dir"),
                payload.get("source_layer"),
            )

    parser = argparse.ArgumentParser(
        description="Package a skill folder into skill-<id>.zip"
    )
    parser.add_argument("skill_path", help="Path to the skill folder")
    parser.add_argument(
        "output_dir_pos",
        nargs="?",
        default=None,
        help="Output directory (positional, for backwards compat)",
    )
    parser.add_argument(
        "--source-layer",
        choices=["system", "user", "project"],
        default=None,
        help="Skill source layer (filled into README; auto-detected if omitted)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (preferred over positional)",
    )
    args = parser.parse_args()
    output_dir = args.output_dir or args.output_dir_pos
    return args.skill_path, output_dir, args.source_layer


def main():
    skill_path, output_dir, source_layer = _resolve_invocation()

    print(f"📦 Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    if source_layer:
        print(f"   Source layer (explicit): {source_layer}")
    print()

    result = package_skill(
        skill_path,
        output_dir=output_dir,
        source_layer=source_layer,
    )

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
