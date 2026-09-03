# Connect Claude (MCP)

Zero-install: point Claude at the community MCP host. Local `uvx` is the
fallback when you want the server on your machine.

| What | URL |
| --- | --- |
| **MCP** (Claude Desktop / Claude Code) | `https://api.umbra-py.space/mcp` |
| **STAC** (`pystac-client`, QGIS) | `https://api.umbra-py.space/` |
| OpenAPI | `https://api.umbra-py.space/docs` |
| Health | `https://api.umbra-py.space/healthz` |

Do not point a STAC client at `/mcp`, and do not point Claude at the STAC
root. Same host, different paths.

This is an unofficial community host of Umbra's *open* data (CC BY 4.0), not
an Umbra product. No account. 120 requests/minute (`429` + `Retry-After`).
Do not send `UMBRA_CANOPY_TOKEN` or model API keys at this URL.

On this host, `quicklook` and `describe_scene` serve **baked catalog previews**
(small PNGs shipped with the weekly index). Your Claude session is the vision
model: `describe_scene` returns the picture plus a SAR-literacy prompt, and
you read it. Tools that would stream Umbra GeoTIFFs through the host
(`change_composite`, `timescan`, `stack_stats`, `narrate_change`, …) refuse
and tell you to run a local server or open the asset `href` from `get_item`.
For a site name you roughly know (`beet piler`), use `search_catalog(area=…,
fuzzy=True)` or `find_repeat_sites` — not `semantic=True` (that needs an
embedding key this host does not hold).

## Claude Code (recommended: one command)

Remote Streamable HTTP. Claude Code calls this transport `http`:

```bash
claude mcp add --transport http umbra https://api.umbra-py.space/mcp --scope user
```

Check it:

```bash
claude mcp get umbra
# inside a session: /mcp
```

`--scope user` makes it available in every project. Omit it for this project
only, or pass `--scope project` to write `.mcp.json` for the team.

### Claude Code JSON

If you would rather paste config (`.mcp.json` or `claude mcp add-json`),
**include `"type": "http"`**. A `url` with no `type` is treated as stdio and
the server never connects
([Claude Code MCP docs](https://code.claude.com/docs/en/mcp)):

```json
{
  "mcpServers": {
    "umbra": {
      "type": "http",
      "url": "https://api.umbra-py.space/mcp"
    }
  }
}
```

```bash
claude mcp add-json umbra '{"type":"http","url":"https://api.umbra-py.space/mcp"}' --scope user
```

## Claude Desktop

**Settings → Connectors** (or **Developer**), then add a custom connector
with URL `https://api.umbra-py.space/mcp`.

Or paste into `claude_desktop_config.json` and restart Desktop:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "umbra": {
      "url": "https://api.umbra-py.space/mcp"
    }
  }
}
```

Desktop does not use `claude mcp add`. Do not copy the Desktop block into
Claude Code without adding `"type": "http"`.

Already configured Desktop? Claude Code can import it:

```bash
claude mcp add-from-claude-desktop
```

## Local stdio (no public host)

Use this when you want the server on your laptop (`UMBRA_INDEX_DB`, a
Canopy token, or offline). Needs [uv](https://docs.astral.sh/uv/).

```bash
uvx --from 'umbra-py[mcp]' umbra-mcp
```

**Claude Desktop** (`command` / `args`):

```json
{
  "mcpServers": {
    "umbra": {
      "command": "uvx",
      "args": ["--from", "umbra-py[mcp]", "umbra-mcp"]
    }
  }
}
```

**Claude Code** — put the launch command after `--` so `uvx` flags are not
eaten by the CLI:

```bash
claude mcp add --transport stdio umbra --scope user -- uvx --from 'umbra-py[mcp]' umbra-mcp
```

The same command is published to the
[MCP registry](https://registry.modelcontextprotocol.io/) as
`io.github.reesehammer/umbra-mcp`.

Optional env on *local* stdio only: `UMBRA_INDEX_DB` (fetched catalog
snapshot — without it each search walks S3), `UMBRA_CANOPY_TOKEN` (paid
archive), `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (optional: with a key,
`describe_scene` / `narrate_change` call a vision model on the server;
without one they return a reading kit for the client's model). Fetch
thumbnails (`umbra index fetch-thumbnails`) so `quicklook` can serve a
picture without streaming a COG.

## Self-host

[`umbra serve --public`](deploy.md) serves STAC at `/` and MCP at `/mcp` on
one process. `umbra mcp --http` is MCP-only. See [Deploy](deploy.md).
