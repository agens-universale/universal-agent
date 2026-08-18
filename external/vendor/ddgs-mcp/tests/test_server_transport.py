from unittest.mock import patch

import ddgs_mcp.server as server


def test_main_runs_stdio_transport():
    with patch.object(server.mcp, "run") as run_mock:
        server.main()
    run_mock.assert_called_once_with(transport="stdio")
