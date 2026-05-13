"""Cross-platform launcher for the LiteLLM Proxy battery.

Mirrors `recommended_mcp/run.py`. Reads litellm/.env, sanity-checks the
callback package is importable as `callbacks.provider_normalizer`, then
renders `proxy_config.yaml` into `proxy_config.local.yaml` (with
`__VAR__` placeholders substituted from env), and execs
`litellm --config proxy_config.local.yaml --port <PORT>`.

The placeholder pass keeps any user-specific identifier (e.g.
`YANDEX_FOLDER_ID`) out of the tracked config file — it lives in
`litellm/.env` (gitignored) and is resolved at launch.

Run:
    python litellm/run.py

The proxy keeps running in the foreground. Stop with Ctrl-C.
The agent does NOT control this process — it only hits
http://localhost:<PORT>/v1/chat/completions when configured.
"""
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
    port: int, cli: str | None = None, config_filename: str = "proxy_config.local.yaml"
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


_PLACEHOLDER_RE = re.compile(r"__([A-Z_][A-Z0-9_]*)__")


def render_config(template: Path, target: Path, env: dict) -> list[str]:
    """Substitute `__VAR__` placeholders in `template` from `env` and write
    the result to `target`. Returns the list of placeholder names that
    were missing from `env` (empty list = success).

    The output is always written — placeholders for which env had no value
    stay as `__VAR__` in the rendered file. Aliases referencing those
    placeholders will fail at call time (upstream / LiteLLM error), but
    every other alias keeps working. This lets a user configure only
    Yandex or only GigaChat without forcing them to fill irrelevant env vars.
    """
    text = template.read_text()
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        value = env.get(name)
        if value is None or value == "":
            if name not in missing:
                missing.append(name)
            return match.group(0)
        return value

    rendered = _PLACEHOLDER_RE.sub(repl, text)
    target.write_text(rendered)
    return missing


def main() -> int:
    root = Path(__file__).resolve().parent

    env_loaded = load_env_file(root / ".env")
    for k, v in env_loaded.items():
        os.environ.setdefault(k, v)

    missing_creds = [
        k
        for k in ("GIGACHAT_CREDENTIALS", "YANDEX_API_KEY")
        if not os.environ.get(k)
    ]
    if missing_creds:
        print(
            f"WARNING: missing env vars: {', '.join(missing_creds)} — "
            f"models depending on them will fail at runtime.",
            file=sys.stderr,
        )

    missing_placeholders = render_config(
        root / "proxy_config.yaml",
        root / "proxy_config.local.yaml",
        os.environ,
    )
    if missing_placeholders:
        print(
            f"WARNING: placeholders left unfilled: {', '.join(missing_placeholders)}. "
            f"Aliases referencing them will fail at call time. "
            f"Fill them in litellm/.env if you plan to use those providers.",
            file=sys.stderr,
        )
    print("[ok] rendered proxy_config.local.yaml")

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
