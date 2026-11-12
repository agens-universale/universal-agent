"""Серверу fs_view_image не положен: тул читает файлы машины сервера (спека §9.2)."""

import yaml

from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_testkit.paths import REPO


def test_demo_server_config_stops_view_image():
    cfg = yaml.safe_load(
        (REPO / "demo_config_server.yml").read_text(encoding="utf-8")
    )
    stop_tools = cfg["webhook"]["agent_overrides"]["agent.stop_tools"]
    assert FS_VIEW_IMAGE_NAME in stop_tools
