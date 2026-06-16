import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

REQUIRED = [
    "prompts/chat_completions_system_prompt.md",
    "prompts/chat_completions_tui_special_instructions.md",
    "tui/styles.tcss",
    "recommended_mcp/config.yml",
    "config.default.yml",
]


def test_wheel_bundles_root_assets(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    out = tmp_path / "dist"
    r = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(out)],
        cwd=str(REPO), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, [w.name for w in wheels]
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    for needed in REQUIRED:
        assert needed in names, f"{needed} missing from wheel; got sample {sorted(names)[:20]}"
    assert any(n.startswith("skills/") for n in names), "skills/ missing"
    assert any(n.startswith("recommended_mcp/") and n.endswith(".py") for n in names), "recommended_mcp .py missing"
    # core src modules must map to top-level
    assert "config.py" in names, "src/config.py not mapped to top-level config.py"
    assert any(n.startswith("tui/") and n.endswith(".py") for n in names), "tui package .py missing"
