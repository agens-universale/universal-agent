# science-mcp

MCP server for unified scientific literature search. Wraps 10 public APIs
(OpenAlex, CrossRef, PubMed/PMC, arXiv, Semantic Scholar, DBLP, CORE,
OpenCitations, bioRxiv/medRxiv) behind 7 normalized tools.

## Install

From the parent repo (`background-agent/`):

```bash
python recommended_mcp/install.py
```

Or directly:

```bash
pip install /Users/gkave/SberAI/background-agent/external/science-mcp
```

After install, the `science-mcp` entry-point is on `$PATH` of the venv.

## Run the daemon

science-mcp runs as a long-lived process so in-memory throttling persists
across agent calls. Default: HTTP on `127.0.0.1:8765`, endpoint path `/mcp`.

```bash
# Foreground (default transport is http)
science-mcp --transport http

# Custom bind
science-mcp --transport http --host 0.0.0.0 --port 9000

# Background
nohup science-mcp --transport http > /tmp/science-mcp.log 2>&1 &
```

Stdio mode is kept as a legacy fallback for one-shot debugging (process
spawned per agent call, no shared throttling):

```bash
science-mcp --transport stdio
```

## Configure

Primary source: YAML config file. Env vars remain as fallback (useful in
systemd/docker where editing files is awkward). Lookup order per field:
**YAML key > env var > built-in default**.

### YAML (recommended)

Copy the tracked template and fill in what applies:

```bash
cp science-mcp.example.yaml science-mcp.yaml
$EDITOR science-mcp.yaml
science-mcp --transport http --config ./science-mcp.yaml
```

If `--config` is omitted, the daemon auto-discovers `./science-mcp.yaml` then
`~/.config/science-mcp.yaml`. `science-mcp.yaml` is gitignored — only the
`.example` template is tracked.

Available keys (see `science-mcp.example.yaml` for full annotated example):

| Key | Default | Purpose |
|---|---|---|
| `mailto` | `team@company.example` | OpenAlex polite-pool + CrossRef UA |
| `host` | `127.0.0.1` | Daemon bind host |
| `port` | `8765` | Daemon bind port |
| `ncbi_api_key` | `null` | Raises NCBI rate limit 3/s → 10/s |
| `semantic_scholar_api_key` | `null` | Required for usable S2 (no-key = instant 429) |
| `core_api_key` | `null` | More stable CORE access |
| `cache_dir` | `~/.cache/science-mcp` | Disk JSON cache directory |
| `cache_enabled` | `true` | `false` disables disk cache |
| `http_timeout_sec` | `15` | Default per-request timeout, seconds |
| `queue_timeout_sec` | `120` | Max wait in throttler queue, seconds |

Unknown keys in YAML raise loudly — typo guard.

### Env vars (fallback)

Useful for docker/systemd. Each YAML key has a matching env var with the
`SCIENCE_MCP_` prefix (or, for keys, the canonical `NCBI_API_KEY` /
`SEMANTIC_SCHOLAR_API_KEY` / `CORE_API_KEY`):

| Variable | Maps to |
|---|---|
| `SCIENCE_MCP_MAILTO` | `mailto` |
| `SCIENCE_MCP_HOST` | `host` |
| `SCIENCE_MCP_PORT` | `port` |
| `NCBI_API_KEY` | `ncbi_api_key` |
| `SEMANTIC_SCHOLAR_API_KEY` | `semantic_scholar_api_key` |
| `CORE_API_KEY` | `core_api_key` |
| `SCIENCE_MCP_CACHE_DIR` | `cache_dir` |
| `SCIENCE_MCP_CACHE` | `cache_enabled` (`0` → false) |
| `SCIENCE_MCP_HTTP_TIMEOUT` | `http_timeout_sec` |
| `SCIENCE_MCP_QUEUE_TIMEOUT` | `queue_timeout_sec` |

## Connect to agent

Copy `recommended_mcp/science/agent_config.snippet.yml` (in the parent repo)
into the `mcp-servers:` section of your agent's `config.yml`. Default
connection: `transport: stream_http`, `url: http://127.0.0.1:8765/mcp`.

## Documentation

Implementation plan and design: `.docs/superpowers/plans/2026-05-19-science-mcp-implementation.md`.

## Architecture

Layered: utilities → adapters → tools → server. See
`.docs/superpowers/plans/2026-05-19-science-mcp-implementation.md` for the
full implementation plan.

## Operational constraints (from live probes 2026-05-18)

- Semantic Scholar without an API key returns immediate 429 — adapter raises `unauthorized`.
- CrossRef `filter=member:246` includes CSHL journals, not only preprints — `cshl_preprints` adapter secondary-filters by DOI prefix `10.1101/YYYY.MM.DD.NNNNNN`.
- PMC `efetch` returns full-text XML up to 236KB — `ncbi_fetch` defaults to `summary`; `fetch` mode capped at 50KB.
- OpenCitations `citations` returns ~5MB for popular DOIs — disk cache TTL 24h is mandatory.
- arXiv enforces 1 req / 3s; `preprint_search` is isolated from `literature_search`.
- CORE endpoint requires trailing slash: `/v3/search/works/?q=...`.
- OpenCitations endpoint moved: `https://api.opencitations.net/index/v1/...`.
- `stream_stdio` transport in `gc-cli-pub` whitelists inherited env to `HOME/LOGNAME/PATH/SHELL/TERM/USER` — list all `SCIENCE_MCP_*` and provider keys explicitly under `env:` in agent config. HTTP transport bypasses this — daemon inherits its own shell env.
