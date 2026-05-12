"""Cross-platform install for the LiteLLM Proxy battery.

Mirrors the `recommended_mcp/install.py` pattern. Verifies the `litellm`
package is importable (declared in project's pyproject.toml dependencies),
seeds `.env` from `.env.example` if missing.

No Docker, no system packages — pure pip+stdlib.

Run:
    python litellm/install.py
"""
import importlib
import shutil
import sys
from pathlib import Path


class InstallError(RuntimeError):
    """Cannot complete LiteLLM Proxy battery install."""


def check_litellm_installed() -> bool:
    """Return True if `litellm` package is importable."""
    try:
        importlib.import_module("litellm")
        return True
    except ImportError:
        return False


def seed_env_file(root: Path) -> bool:
    """Copy `.env.example` to `.env` if `.env` does not exist.

    Returns True if a new `.env` was created.
    """
    target = root / ".env"
    template = root / ".env.example"
    if target.exists():
        return False
    if not template.exists():
        raise InstallError(f"{template} missing — cannot seed .env")
    shutil.copyfile(template, target)
    return True


def main() -> int:
    root = Path(__file__).resolve().parent
    if not check_litellm_installed():
        print(
            "ERROR: `litellm` not installed. Run `pip install -e .` (or "
            "`uv sync`) from repo root to install project dependencies.",
            file=sys.stderr,
        )
        return 1
    print("[ok] litellm package importable")

    if seed_env_file(root):
        print(f"[ok] seeded {root / '.env'} from .env.example — fill in credentials")
    else:
        print(f"[ok] {root / '.env'} already exists")

    print()
    print("Next: edit litellm/.env with provider credentials, then:")
    print("  python litellm/run.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
