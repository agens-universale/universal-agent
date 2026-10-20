#!/usr/bin/env python3
"""Синхронный bump версий во всех пакетах uv-workspace.

Правит:
  • `version = "X.Y.Z"` в корневом pyproject.toml и packages/*/pyproject.toml
  • exact-pins `gc-*==X.Y.Z` в dependencies пакетов
  • `uv.lock` (`uv lock`)

Использование:
    python scripts/bump_version.py 0.3.1
    python scripts/bump_version.py 0.3.1 --dry-run

Коммит и тег — руками. Доки и тесты не трогает: упаковочный тест читает
версию из манифеста.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Константы вместо повторяющихся литералов (Sonar S1192).
_MANIFEST_NAME = "pyproject.toml"


ROOT = Path(__file__).resolve().parent.parent

MANIFESTS = [
    ROOT / _MANIFEST_NAME,
    ROOT / "packages" / "core" / _MANIFEST_NAME,
    ROOT / "packages" / "cli" / _MANIFEST_NAME,
    ROOT / "packages" / "tui" / _MANIFEST_NAME,
    ROOT / "packages" / "server" / _MANIFEST_NAME,
    ROOT / "packages" / "mcp" / _MANIFEST_NAME,
    ROOT / "packages" / "acp" / _MANIFEST_NAME,
    ROOT / "packages" / "stores" / _MANIFEST_NAME,
    ROOT / "packages" / "testkit" / _MANIFEST_NAME,
]

# Цифры в имени дистрибутива допустимы: `[a-z]+` пин вроде gc-a2a не видел.
PIN_RE = re.compile(r'"(gc-[a-z0-9]+)==\d+\.\d+\.\d+"')
VERSION_RE = re.compile(
    r'^(\s*version\s*=\s*")(\d+\.\d+\.\d+)("\s*)$', re.MULTILINE
)
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def _bump_manifest(path: Path, new: str, dry: bool) -> list[str]:
    text = path.read_text(encoding="utf-8")
    matches = list(VERSION_RE.finditer(text))
    if not matches:
        return []
    changes: list[str] = []
    new_text = text
    for m in matches:
        old_ver = m.group(2)
        if old_ver == new:
            continue
        new_text = new_text.replace(
            m.group(0), f"{m.group(1)}{new}{m.group(3)}", 1
        )
        changes.append(
            f"  {path.relative_to(ROOT)}: version {old_ver} → {new}"
        )
    if new_text != text and not dry:
        path.write_text(new_text, encoding="utf-8")
    return changes


def _bump_pins(path: Path, new: str, dry: bool) -> list[str]:
    text = path.read_text(encoding="utf-8")
    changes: list[str] = []
    new_text = text
    for m in PIN_RE.finditer(text):
        old_pin = m.group(0)
        pkg = m.group(1)
        replacement = f'"{pkg}=={new}"'
        if old_pin == replacement:
            continue
        new_text = new_text.replace(old_pin, replacement, 1)
        old_ver = old_pin.split("==")[1][:-1]
        changes.append(f"  {path.relative_to(ROOT)}: {pkg} {old_ver} → {new}")
    if new_text != text and not dry:
        path.write_text(new_text, encoding="utf-8")
    return changes


def _verify(new: str) -> bool:
    ok = True
    for p in MANIFESTS:
        text = p.read_text(encoding="utf-8")
        for m in VERSION_RE.finditer(text):
            if m.group(2) != new:
                print(
                    f"  MISMATCH {p.relative_to(ROOT)}: version={m.group(2)} (expected {new})",
                    file=sys.stderr,
                )
                ok = False
        for m in PIN_RE.finditer(text):
            actual = m.group(0).split("==")[1].rstrip('"')
            if actual != new:
                print(
                    f"  MISMATCH {p.relative_to(ROOT)}: {m.group(1)}=={actual} (expected {new})",
                    file=sys.stderr,
                )
                ok = False
    return ok


def _uv_lock() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit(
            "ERROR: uv not found in PATH; cannot regenerate uv.lock"
        )
    print("\nRegenerating uv.lock...")
    subprocess.run([uv, "lock"], check=True, cwd=ROOT)
    print("  uv.lock updated")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bump all workspace versions and regenerate uv.lock."
    )
    ap.add_argument("version", help="New version, e.g. 0.3.1")
    ap.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing"
    )
    args = ap.parse_args()

    if not VERSION_PATTERN.match(args.version):
        print(
            f"ERROR: '{args.version}' is not a valid X.Y.Z version",
            file=sys.stderr,
        )
        return 2

    new = args.version
    dry = args.dry_run

    print(f"Bumping to {new}{' (dry-run)' if dry else ''}...")
    all_changes: list[str] = []

    for p in MANIFESTS:
        if not p.exists():
            print(f"  SKIP {p.relative_to(ROOT)} (not found)", file=sys.stderr)
            continue
        all_changes += _bump_manifest(p, new, dry)
        all_changes += _bump_pins(p, new, dry)

    if not all_changes:
        print(f"All versions already at {new}. Nothing to do.")
        return 0

    for line in all_changes:
        print(line)
    print(f"\n{len(all_changes)} change(s).")

    if dry:
        print("(dry-run — no files written, uv.lock not touched)")
        return 0

    print("\nVerifying...")
    if _verify(new):
        print(f"OK: all manifests and pins at {new}.")
    else:
        print("VERIFICATION FAILED — see mismatches above.", file=sys.stderr)
        return 1

    _uv_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
