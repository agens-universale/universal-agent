"""Единый резолвер корневых ассетов (prompts/, skills/, recommended_mcp/,
config.default.yml). Работает в editable-инсталле (репо-root) и в wheel
(site-packages). Технический слой — без бизнес-логики."""
from pathlib import Path


def bundled_root() -> Path:
    """Каталог, содержащий prompts/ (маркер) и прочие bundled-ассеты.

    editable: src/bundled_assets.py → here=<repo>/src → here.parent=<repo>.
    wheel:    site-packages/bundled_assets.py → here=site-packages (туда
              force-include положил prompts/, skills/, recommended_mcp/,
              config.default.yml)."""
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent):
        if (cand / "prompts").is_dir():
            return cand
    raise FileNotFoundError(
        "bundled assets not found: prompts/ marker missing near "
        f"{here} (editable repo-root или wheel site-packages)"
    )
