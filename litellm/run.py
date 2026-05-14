"""Cross-platform launcher for the LiteLLM Proxy battery.

Sanity-checks `callbacks.provider_normalizer` is importable, then execs
`litellm --config proxy_config.yaml --port <PORT>`.

Run:
    python litellm/run.py

The proxy keeps running in the foreground. Stop with Ctrl-C.
The agent does NOT control this process — it only hits
http://localhost:<PORT>/v1/chat/completions when configured.
"""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class RunError(RuntimeError):
    """Cannot start LiteLLM Proxy."""


_CALLBACK_PROBE = (
    "import callbacks.provider_normalizer as m; "
    "assert hasattr(m, 'proxy_handler_instance'), 'proxy_handler_instance missing'"
)


def check_callback_importable(litellm_root: Path) -> bool:
    """Verify `callbacks.provider_normalizer` can be imported when the
    process has `cwd=litellm_root` (the exact environment LiteLLM Proxy
    will run in once started).

    Runs the check in a subprocess on purpose: the parent process's
    sys.modules / sys.path may already contain entries that mask or
    conflict with the namespace-package resolution of `litellm` itself
    (this directory's name shadows the PyPI package). A clean subprocess
    mirrors the real proxy startup and gives a deterministic result.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _CALLBACK_PROBE],
        cwd=str(litellm_root),
        capture_output=True,
    )
    return proc.returncode == 0


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


def build_command(
    port: int, cli: str | None = None, config_filename: str = "proxy_config.yaml"
) -> list[str]:
    """Build the `litellm` CLI command (no shell, OS-agnostic).

    `cli` defaults to `find_litellm_cli()` — overridable for tests.
    """
    return [
        cli or find_litellm_cli(),
        "--config",
        config_filename,
        "--port",
        str(port),
    ]


def main() -> int:
    root = Path(__file__).resolve().parent

    if not (root / "proxy_config.yaml").exists():
        print(
            "ERROR: litellm/proxy_config.yaml not found. "
            "Copy litellm/proxy_config.example.yaml → litellm/proxy_config.yaml "
            "and fill in credentials, then re-run.",
            file=sys.stderr,
        )
        return 1

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
    # via cwd-as-sys.path[0].
    try:
        proc = subprocess.run(cmd, cwd=str(root))
        return proc.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
