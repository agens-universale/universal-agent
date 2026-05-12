"""Cross-platform launcher for the LiteLLM Proxy battery.

Mirrors `recommended_mcp/run.py`. Reads litellm/.env, sanity-checks the
callback package is importable as `callbacks.provider_normalizer`, then
execs `litellm --config proxy_config.yaml --port <PORT>`.

Run:
    python litellm/run.py

The proxy keeps running in the foreground. Stop with Ctrl-C.
The agent does NOT control this process — it only hits
http://localhost:<PORT>/v1/chat/completions when configured.
"""
from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class RunError(RuntimeError):
    """Cannot start LiteLLM Proxy."""


_EXPORT_RE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple `export KEY=VALUE` style .env file.

    Returns {} if file does not exist. Quoted values have outer quotes stripped.
    Lines starting with `#` or empty lines are ignored.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def check_callback_importable(litellm_root: Path) -> bool:
    """Verify `callbacks.provider_normalizer` can be imported with
    `litellm_root` on sys.path. Returns True on success."""
    sys.path.insert(0, str(litellm_root))
    # Drop a previously-cached `callbacks` package from a different root.
    cached = [m for m in sys.modules if m == "callbacks" or m.startswith("callbacks.")]
    for m in cached:
        del sys.modules[m]
    try:
        mod = importlib.import_module("callbacks.provider_normalizer")
        return hasattr(mod, "proxy_handler_instance")
    except ImportError:
        return False
    finally:
        try:
            sys.path.remove(str(litellm_root))
        except ValueError:
            pass


def find_litellm_cli() -> str:
    """Locate the `litellm` CLI executable.

    Prefer the one bundled with the current Python interpreter (venv `bin/` or
    Windows `Scripts/`), falling back to whatever is on `$PATH`. The PyPI
    `litellm` package does not expose `__main__`, so `python -m litellm` does
    not work — we must call the console script directly.
    """
    bin_dir = Path(sys.executable).parent
    candidates = [bin_dir / "litellm", bin_dir / "litellm.exe"]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which("litellm")
    if found:
        return found
    raise RunError(
        "`litellm` CLI not found. Install with `pip install -e .` (or "
        "`uv sync`) from repo root."
    )


def build_command(port: int, cli: str | None = None) -> list[str]:
    """Build the `litellm` CLI command (no shell, OS-agnostic).

    `cli` defaults to `find_litellm_cli()` — overridable for tests.
    """
    return [
        cli or find_litellm_cli(),
        "--config",
        "proxy_config.yaml",
        "--port",
        str(port),
    ]


def main() -> int:
    root = Path(__file__).resolve().parent

    env_loaded = load_env_file(root / ".env")
    for k, v in env_loaded.items():
        os.environ.setdefault(k, v)

    missing = [
        k
        for k in ("GIGACHAT_CREDENTIALS", "YANDEX_API_KEY", "OPENROUTER_API_KEY")
        if not os.environ.get(k)
    ]
    if missing:
        print(
            f"WARNING: missing env vars: {', '.join(missing)} — "
            f"models depending on them will fail at runtime.",
            file=sys.stderr,
        )

    if not check_callback_importable(root):
        print(
            "ERROR: callbacks.provider_normalizer not importable from "
            f"{root}. Check litellm/callbacks/__init__.py + provider_normalizer.py.",
            file=sys.stderr,
        )
        return 1
    print("[ok] callbacks.provider_normalizer importable")

    port = int(os.environ.get("LITELLM_PORT", "4000"))
    cmd = build_command(port)
    print(f"[run] cwd={root} cmd={' '.join(shlex.quote(c) for c in cmd)}")
    print(f"      proxy will listen on http://localhost:{port}/v1/chat/completions")
    print()

    # cd into litellm/ so that `callbacks.provider_normalizer` is importable
    # via cwd-as-sys.path[0]. Pass env explicitly so the subprocess sees
    # the credentials we loaded.
    try:
        proc = subprocess.run(cmd, cwd=str(root), env=os.environ.copy())
        return proc.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
