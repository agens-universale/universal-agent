from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .analytics import (
    _to_jsonable,
    apply_filter,
    apply_groupby_agg,
    apply_limit,
    apply_pivot,
    apply_select,
    apply_sort,
    dataframe_to_xlsx_bytes,
    load_table,
    summarize_dataframe,
)
from .analytics_schema import OutputSpec, SortSpec, parse_filter
from .errors import ErrorCategory, OfficeReaderError
from .logging_config import mask_base64_in_args
from .reader import (
    read_document_from_bytes,
    read_file_from_disk,
)
from .security import validate_path
from .writer import generate_docx, generate_xlsx
from .writer_io import encode_base64_checked, write_bytes_checked
from .writer_schema import WorkbookSpec

log = logging.getLogger(__name__)


def _validate_workbook(raw: dict) -> WorkbookSpec:
    try:
        return WorkbookSpec.model_validate(raw)
    except ValidationError as ve:
        # pydantic errors are verbose; pick the first one for the user message
        first = ve.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise OfficeReaderError(
            ErrorCategory.INVALID_INPUT,
            reason=f"{loc}: {first['msg']}",
        ) from ve


@dataclass
class ServerConfig:
    allowed_dirs: list[str]
    allow_any: bool
    max_file_size: int
    max_output_chars: int
    timeout_s: int
    max_output_base64_size: int = 20 * 1024 * 1024
    output_allowed_dirs: list[str] = field(default_factory=list)
    max_query_rows: int = 10000


def build_server(cfg: ServerConfig) -> FastMCP:
    mcp = FastMCP("office-mcp")

    @mcp.tool(name="read_file")
    def read_file_tool(path: str) -> str:
        """Read an office document from the server's filesystem and return markdown.
        Use when the file is available at a filesystem path (e.g. via a shared volume).
        Supported: .pdf, .docx, .xls, .xlsx, .pptx, .csv, .html, .msg."""
        args = {"path": path}
        try:
            real = validate_path(path, cfg.allowed_dirs, allow_any=cfg.allow_any)
            result = read_file_from_disk(
                real,
                max_file_size=cfg.max_file_size,
                max_output_chars=cfg.max_output_chars,
                timeout_s=cfg.timeout_s,
            )
            log.info(
                "read_file ok",
                extra={
                    "event": "tool_call",
                    "tool": "read_file",
                    "tool_args": args,
                    "output_chars": len(result),
                },
            )
            return result
        except OfficeReaderError as e:
            log.warning(
                "read_file failed",
                extra={
                    "event": "tool_error",
                    "tool": "read_file",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="read_document")
    def read_document_tool(filename: str, content_base64: str) -> str:
        """Read an office document provided as base64-encoded bytes.
        Use when the file is not available on the server's filesystem.
        Supported: .pdf, .docx, .xls, .xlsx, .pptx, .csv, .html, .msg."""
        args = mask_base64_in_args(
            {"filename": filename, "content_base64": content_base64}
        )
        try:
            result = read_document_from_bytes(
                filename=filename,
                content_base64=content_base64,
                max_file_size=cfg.max_file_size,
                max_output_chars=cfg.max_output_chars,
                timeout_s=cfg.timeout_s,
            )
            log.info(
                "read_document ok",
                extra={
                    "event": "tool_call",
                    "tool": "read_document",
                    "tool_args": args,
                    "output_chars": len(result),
                },
            )
            return result
        except OfficeReaderError as e:
            log.warning(
                "read_document failed",
                extra={
                    "event": "tool_error",
                    "tool": "read_document",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="create_docx_file")
    def create_docx_file_tool(
        path: str, markdown: str, overwrite: bool = False
    ) -> dict:
        """Generate a .docx file from markdown and write it to the server filesystem.
        Use when the caller has filesystem access to the server (e.g. via a shared volume).
        Markdown supports headings, paragraphs, inline bold/italic/code/links, bulleted
        and numbered lists (nestable), GFM tables, blockquotes, fenced code blocks,
        horizontal rules. No images in this version.
        Writes only to paths under --output-allowed-dirs (default: same as --allowed-dirs).
        Fails with file_exists if the target already exists and overwrite=False."""
        args = {
            "path": path,
            "overwrite": overwrite,
            "markdown_chars": len(markdown),
        }
        try:
            data = generate_docx(markdown)
            n = write_bytes_checked(
                path, data,
                output_allowed_dirs=cfg.output_allowed_dirs,
                allow_any=cfg.allow_any,
                overwrite=overwrite,
            )
            log.info(
                "create_docx_file ok",
                extra={
                    "event": "tool_call",
                    "tool": "create_docx_file",
                    "tool_args": args,
                    "bytes_written": n,
                },
            )
            return {"path": path, "bytes_written": n}
        except OfficeReaderError as e:
            log.warning(
                "create_docx_file failed",
                extra={
                    "event": "tool_error",
                    "tool": "create_docx_file",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="create_docx")
    def create_docx_tool(filename: str, markdown: str) -> dict:
        """Generate a .docx file from markdown and return its content as base64.
        Use when the caller does not have filesystem access to the server (e.g. agent
        chat attaching the result). Size-capped by --max-output-base64-size.
        Markdown supports headings, paragraphs, inline bold/italic/code/links, bulleted
        and numbered lists (nestable), GFM tables, blockquotes, fenced code blocks,
        horizontal rules. No images in this version."""
        args = {"filename": filename, "markdown_chars": len(markdown)}
        try:
            data = generate_docx(markdown)
            enc = encode_base64_checked(
                data, max_base64_bytes=cfg.max_output_base64_size
            )
            log.info(
                "create_docx ok",
                extra={
                    "event": "tool_call",
                    "tool": "create_docx",
                    "tool_args": args,
                    "raw_bytes": enc["raw_bytes"],
                },
            )
            return {"filename": filename, **enc}
        except OfficeReaderError as e:
            log.warning(
                "create_docx failed",
                extra={
                    "event": "tool_error",
                    "tool": "create_docx",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="create_xlsx_file")
    def create_xlsx_file_tool(
        path: str, workbook: dict, overwrite: bool = False
    ) -> dict:
        """Generate an .xlsx file from a structured workbook spec and write it to disk.
        Use when the caller has filesystem access to the server (e.g. via a shared volume).
        workbook shape: {sheets: [{name, columns: [{name, type, format?, width?}],
        rows: [[...]], freeze_header?, auto_filter?, merges?, cell_styles?}, ...]}.
        Types: text, number, integer, date, datetime, bool, currency, percent.
        Dates as ISO-8601 strings. No formulas.
        Ranges in `merges` and `cell_styles` use A1-notation anchored to the final grid:
        row 1 is the auto-generated bold header, data starts at row 2.
        Writes only to paths under --output-allowed-dirs (default: same as --allowed-dirs).
        Fails with file_exists if the target already exists and overwrite=False.
        See README section "Создание документов" for the full schema."""
        sheet_count = len(workbook.get("sheets", [])) if isinstance(workbook, dict) else 0
        args = {
            "path": path,
            "overwrite": overwrite,
            "sheet_count": sheet_count,
        }
        try:
            spec = _validate_workbook(workbook)
            data = generate_xlsx(spec)
            n = write_bytes_checked(
                path, data,
                output_allowed_dirs=cfg.output_allowed_dirs,
                allow_any=cfg.allow_any,
                overwrite=overwrite,
            )
            log.info(
                "create_xlsx_file ok",
                extra={
                    "event": "tool_call",
                    "tool": "create_xlsx_file",
                    "tool_args": args,
                    "bytes_written": n,
                },
            )
            return {"path": path, "bytes_written": n}
        except OfficeReaderError as e:
            log.warning(
                "create_xlsx_file failed",
                extra={
                    "event": "tool_error",
                    "tool": "create_xlsx_file",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="create_xlsx")
    def create_xlsx_tool(filename: str, workbook: dict) -> dict:
        """Generate an .xlsx file from a structured workbook spec and return its content as base64.
        Use when the caller does not have filesystem access to the server (e.g. agent
        chat attaching the result). Size-capped by --max-output-base64-size.
        workbook shape: {sheets: [{name, columns: [{name, type, format?, width?}],
        rows: [[...]], freeze_header?, auto_filter?, merges?, cell_styles?}, ...]}.
        Types: text, number, integer, date, datetime, bool, currency, percent.
        Dates as ISO-8601 strings. No formulas.
        Ranges in `merges` and `cell_styles` use A1-notation anchored to the final grid:
        row 1 is the auto-generated bold header, data starts at row 2.
        See README section "Создание документов" for the full schema."""
        sheet_count = len(workbook.get("sheets", [])) if isinstance(workbook, dict) else 0
        args = {"filename": filename, "sheet_count": sheet_count}
        try:
            spec = _validate_workbook(workbook)
            data = generate_xlsx(spec)
            enc = encode_base64_checked(
                data, max_base64_bytes=cfg.max_output_base64_size
            )
            log.info(
                "create_xlsx ok",
                extra={
                    "event": "tool_call",
                    "tool": "create_xlsx",
                    "tool_args": args,
                    "raw_bytes": enc["raw_bytes"],
                },
            )
            return {"filename": filename, **enc}
        except OfficeReaderError as e:
            log.warning(
                "create_xlsx failed",
                extra={
                    "event": "tool_error",
                    "tool": "create_xlsx",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="inspect_xlsx")
    def inspect_xlsx_tool(path: str, sheet: str | None = None) -> dict:
        """Inspect a tabular file (xlsx/xls/csv) and return schema + sample.

        Returns: {sheets: [{name, n_rows, columns: [...], head: [...]}]}.
        Each column has: {name, dtype, n_null, n_unique, sample|min/max/mean}.
        Use this BEFORE query_xlsx/aggregate_xlsx/pivot_xlsx to discover columns.
        Output size is fixed (does not scale with file size)."""
        args = {"path": path, "sheet": sheet}
        try:
            real = validate_path(path, cfg.allowed_dirs, allow_any=cfg.allow_any)
            df = load_table(real, sheet=sheet)
            sheet_name = sheet if sheet else "Sheet1"
            summary = summarize_dataframe(df)
            summary["name"] = sheet_name
            log.info(
                "inspect_xlsx ok",
                extra={
                    "event": "tool_call",
                    "tool": "inspect_xlsx",
                    "tool_args": args,
                    "n_rows": summary["n_rows"],
                },
            )
            return {"sheets": [summary]}
        except OfficeReaderError as e:
            log.warning(
                "inspect_xlsx failed",
                extra={
                    "event": "tool_error",
                    "tool": "inspect_xlsx",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    def _dispatch_output(df, out_spec, *, n_total_after_filter, args, tool):
        if out_spec.kind == "data":
            if len(df) > cfg.max_query_rows:
                raise OfficeReaderError(
                    ErrorCategory.OUTPUT_TOO_LARGE,
                    size_mb=len(df),
                    max_mb=cfg.max_query_rows,
                    unit=" rows",
                )
            rows = [
                {str(k): _to_jsonable(v) for k, v in record.items()}
                for record in df.to_dict(orient="records")
            ]
            log.info(
                f"{tool} ok",
                extra={
                    "event": "tool_call", "tool": tool, "tool_args": args,
                    "n_returned": len(rows),
                },
            )
            return {
                "n_total_after_filter": n_total_after_filter,
                "n_returned": len(rows),
                "rows": rows,
            }
        # output=file
        data = dataframe_to_xlsx_bytes(df, sheet_name=out_spec.sheet)
        n = write_bytes_checked(
            out_spec.path, data,
            output_allowed_dirs=cfg.output_allowed_dirs,
            allow_any=cfg.allow_any,
            overwrite=out_spec.overwrite,
        )
        log.info(
            f"{tool} ok (file)",
            extra={
                "event": "tool_call", "tool": tool, "tool_args": args,
                "bytes_written": n, "n_rows_written": len(df),
            },
        )
        return {"path": out_spec.path, "bytes_written": n, "n_rows_written": len(df)}

    @mcp.tool(name="query_xlsx")
    def query_xlsx_tool(
        path: str,
        sheet: str | None = None,
        filter: list | None = None,
        select: list[str] | None = None,
        sort: list[dict] | None = None,
        limit: int | None = None,
        output=None,
    ) -> dict:
        """Filter, select, sort, and limit rows from a tabular file (xlsx/xls/csv).

        filter: list of {col, op, value} clauses; AND-ed; use {"or": [...]} for OR.
        select: list of columns to return; default = all.
        sort: list of {col, order: "asc"|"desc"}.
        limit: max rows; capped by --max-query-rows for inline (output="data").
        output: "data" (default) returns rows in response,
        {path, sheet?, overwrite?} writes xlsx.
        Operators: ==, !=, >, >=, <, <=, in, not_in, contains, startswith,
        endswith, is_null, not_null, between."""
        args = {
            "path": path, "sheet": sheet,
            "has_filter": bool(filter), "select": select,
            "limit": limit, "output_kind": "file" if isinstance(output, dict) else "data",
        }
        try:
            real = validate_path(path, cfg.allowed_dirs, allow_any=cfg.allow_any)
            df = load_table(real, sheet=sheet)
            df = apply_filter(df, parse_filter(filter or []))
            n_total = len(df)
            df = apply_select(df, select)
            sort_specs = [SortSpec.model_validate(s) for s in (sort or [])]
            df = apply_sort(df, sort_specs)
            if limit is not None:
                df = apply_limit(df, limit)
            out_spec = OutputSpec.parse(output)
            return _dispatch_output(
                df, out_spec, n_total_after_filter=n_total,
                args=args, tool="query_xlsx",
            )
        except OfficeReaderError as e:
            log.warning(
                "query_xlsx failed",
                extra={
                    "event": "tool_error",
                    "tool": "query_xlsx",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="pivot_xlsx")
    def pivot_xlsx_tool(
        path: str,
        sheet: str | None = None,
        filter: list | None = None,
        index=None,
        columns=None,
        values: str | None = None,
        aggfunc: str = "sum",
        fill_value=None,
        sort: list[dict] | None = None,
        limit: int | None = None,
        output=None,
    ) -> dict:
        """Cross-tab pivot.

        index = row groups, columns = pivoted column dimension, values = aggregated.
        Result column names: <values>_<column_value> for scalar columns
        (e.g. pivot of "id" by status -> id_open, id_closed).
        aggfunc: any AggFunc (default sum).
        Other params same as query_xlsx."""
        if index is None or columns is None or values is None:
            raise OfficeReaderError(
                ErrorCategory.INVALID_INPUT,
                reason="index, columns, and values are required",
            )
        args = {
            "path": path, "sheet": sheet,
            "index": index, "columns": columns, "values": values, "aggfunc": aggfunc,
        }
        try:
            real = validate_path(path, cfg.allowed_dirs, allow_any=cfg.allow_any)
            df = load_table(real, sheet=sheet)
            df = apply_filter(df, parse_filter(filter or []))
            df = apply_pivot(
                df, index=index, columns=columns, values=values,
                aggfunc=aggfunc, fill_value=fill_value,
            )
            n_total = len(df)
            sort_specs = [SortSpec.model_validate(s) for s in (sort or [])]
            df = apply_sort(df, sort_specs)
            if limit is not None:
                df = apply_limit(df, limit)
            out_spec = OutputSpec.parse(output)
            return _dispatch_output(
                df, out_spec, n_total_after_filter=n_total,
                args=args, tool="pivot_xlsx",
            )
        except OfficeReaderError as e:
            log.warning(
                "pivot_xlsx failed",
                extra={
                    "event": "tool_error",
                    "tool": "pivot_xlsx",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    @mcp.tool(name="aggregate_xlsx")
    def aggregate_xlsx_tool(
        path: str,
        sheet: str | None = None,
        filter: list | None = None,
        groupby: list[str] | None = None,
        agg: dict | None = None,
        having: list | None = None,
        sort: list[dict] | None = None,
        limit: int | None = None,
        output=None,
    ) -> dict:
        """Group + aggregate. agg is dict[col -> AggFunc | list[AggFunc]].

        AggFuncs: sum, mean, count, min, max, median, std, first, last,
        nunique.
        groupby: list of columns; empty list = totals over whole dataset.
        having: filter applied AFTER aggregation; result-column names are
        <col>_<func> (e.g. hours_sum).
        Other params same as query_xlsx."""
        if agg is None:
            raise OfficeReaderError(ErrorCategory.INVALID_INPUT, reason="agg is required")
        args = {
            "path": path, "sheet": sheet,
            "groupby": groupby, "agg_keys": list(agg.keys()),
            "has_having": bool(having), "limit": limit,
        }
        try:
            real = validate_path(path, cfg.allowed_dirs, allow_any=cfg.allow_any)
            df = load_table(real, sheet=sheet)
            df = apply_filter(df, parse_filter(filter or []))
            df = apply_groupby_agg(df, groupby=groupby or [], agg=agg)
            df = apply_filter(df, parse_filter(having or []))
            n_total = len(df)
            sort_specs = [SortSpec.model_validate(s) for s in (sort or [])]
            df = apply_sort(df, sort_specs)
            if limit is not None:
                df = apply_limit(df, limit)
            out_spec = OutputSpec.parse(output)
            return _dispatch_output(
                df, out_spec, n_total_after_filter=n_total,
                args=args, tool="aggregate_xlsx",
            )
        except OfficeReaderError as e:
            log.warning(
                "aggregate_xlsx failed",
                extra={
                    "event": "tool_error",
                    "tool": "aggregate_xlsx",
                    "tool_args": args,
                    "error_category": e.category.value,
                },
            )
            raise

    return mcp


def build_asgi_app(cfg: ServerConfig) -> Starlette:
    mcp = build_server(cfg)
    inner = mcp.streamable_http_app()

    async def health(_request):
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=inner),
        ],
        lifespan=inner.router.lifespan_context,
    )
