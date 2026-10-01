"""Reuse the download tool's validation, URL guards, TLS and result envelope."""

from gc_core.tools.exec.download_file import DownloadFile


class DurableDownload(DownloadFile):
    max_retries = 0  # The preparing activity owns the retry budget.

    async def _http_get_bytes(self, url, max_bytes):
        captured = getattr(self._ctx, "download_content", None)
        if captured is not None:
            return captured, None
        return await super()._http_get_bytes(url, max_bytes)


def download_destination(ctx, arguments):
    tool = DurableDownload(ctx=ctx)
    filename = tool._validate_filename(arguments.get("filename"))
    if isinstance(filename, dict):
        raise ValueError(filename["type"])
    settings = tool._resolve_download_settings(ctx.workspace.root)
    if isinstance(settings, dict):
        raise ValueError(settings["type"])
    from pathlib import Path

    return str(Path(settings[0]) / filename[0])
