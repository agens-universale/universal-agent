"""Cross-platform install for the LiteLLM Proxy battery.

Mirrors the `recommended_mcp/install.py` pattern: this script is the
single point that installs everything needed for the proxy. LiteLLM is
NOT a project dependency in `pyproject.toml` — by design, so that anyone
who does not use the proxy never pulls `litellm[proxy]` and its ~200 MB
of transitive deps (uvicorn / fastapi / orjson / …).

Responsibilities:
  1. `pip install 'litellm[proxy]==<PIN>'` into the current interpreter
     (idempotent — skipped if already importable at the pinned version).
  2. Seed `litellm/proxy_config.yaml` from `litellm/proxy_config.example.yaml`
     if missing. The local file is gitignored and holds real credentials;
     the example is the template under git.

Agent-side configs are NOT seeded into the repo root — they live as
read-only examples in `litellm/agent_config_examples/`. The user copies
or references them manually.

No Docker, no system packages — pure pip+stdlib.

Run:
    python litellm/install.py
"""
import importlib
import importlib.metadata
import shutil
import subprocess
import sys
from pathlib import Path

LITELLM_PIN = "litellm[proxy]==1.83.14"
LITELLM_VERSION = "1.83.14"


class InstallError(RuntimeError):
    """Cannot complete LiteLLM Proxy battery install."""


def installed_litellm_version() -> str | None:
    """Return installed `litellm` version, or None if not importable."""
    try:
        importlib.import_module("litellm")
    except ImportError:
        return None
    try:
        return importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        return None


def pip_install_litellm(pin: str = LITELLM_PIN) -> None:
    """Run `pip install <pin>` in the current interpreter."""
    cmd = [sys.executable, "-m", "pip", "install", pin]
    print(f"[install] {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise InstallError(
            f"pip install failed (exit {result.returncode}). "
            f"Inspect the output above for the underlying cause."
        )


def ensure_litellm_installed(
    pin: str = LITELLM_PIN, expected_version: str = LITELLM_VERSION
) -> bool:
    """Install LiteLLM at the pinned version if not already present.

    Returns True if `pip install` was invoked, False if already satisfied.
    """
    current = installed_litellm_version()
    if current == expected_version:
        return False
    pip_install_litellm(pin)
    return True


def seed_proxy_config(root: Path) -> bool:
    """Copy `proxy_config.example.yaml` to `proxy_config.yaml` if missing.

    Returns True if a new `proxy_config.yaml` was created.
    """
    target = root / "proxy_config.yaml"
    template = root / "proxy_config.example.yaml"
    if target.exists():
        return False
    if not template.exists():
        raise InstallError(f"{template} missing — cannot seed proxy_config.yaml")
    shutil.copyfile(template, target)
    return True


def main() -> int:
    root = Path(__file__).resolve().parent

    try:
        installed_now = ensure_litellm_installed()
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if installed_now:
        print(f"[ok] installed {LITELLM_PIN}")
    else:
        print(f"[ok] litellm {LITELLM_VERSION} already present")

    if seed_proxy_config(root):
        print(
            f"[ok] seeded {root / 'proxy_config.yaml'} from proxy_config.example.yaml "
            f"— fill in credentials"
        )
    else:
        print(f"[ok] {root / 'proxy_config.yaml'} already exists")

    print()
    print("Next steps:")
    print(f"  1. Edit {root / 'proxy_config.yaml'} — fill api_key for each provider")
    print(f"  2. (Optional) Copy a template from {root / 'agent_config_examples'}")
    print(f"     into the repo root if you want a ready gc-cli config")
    print("  3. python litellm/run.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
