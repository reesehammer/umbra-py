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
Limits, uptime, CORS, and the release fallback: [Hosted API limits](hosted-limits.md).
Do not send `UMBRA_CANOPY_TOKEN` or model API keys at this URL.

On this host, `quicklook` and `describe_scene` serve **baked catalog previews**
(typically 512 px, shipped with the weekly index). That is not a silent
fallback: the caption says `BAKED PREVIEW` and, if you asked for a larger
`max_size`, that it was ignored. For a higher-resolution render (typically
1024 px), run a local `uvx --from 'umbra-py[mcp]' umbra-mcp` server, or
download the GEC href from `get_item`.
`describe_scene` does **not** call a vision model here (no key on the host).
It returns the picture plus SAR rules; you produce the JSON and call
`stamp_description` so the reading is validated and provenance-stamped.
Unstamped prose is your look at a small thumbnail, not pipeline output.
Tools that would stream Umbra GeoTIFFs through the host
(`change_composite`, `timescan`, `stack_stats`, `narrate_change`, …) refuse
and tell you to run a local server or open the asset `href` from `get_item`.
For a site name you roughly know (`beet piler`), use `search_catalog(area=…,
fuzzy=True)` or `find_repeat_sites` — not `semantic=True` (that needs an
embedding key this host does not hold).

### Stable tools

Six tools are the stable core through every 0.x minor: `search_catalog`,
`get_item`, `quicklook`, `download_asset`, `geocode_place`, and
`index_stats`. Their names stay. An existing argument keeps its name, and
stays optional when it is optional today. A new argument is optional, and
its default preserves the behavior of a call that omits it. A name or
argument that is retired stays registered as an alias for at least one
minor release. The old tool's description starts with `DEPRECATED: use X`,
the change is in `CHANGELOG.md`, and this page notes it.

Result cards follow the published JSON schemas: stable within a minor
version, and changed only with a changelog entry. A new field may appear.
Removing or retyping a field is a deprecation, on the same alias rule.

A refusal on this host (a tool that would stream an Umbra GeoTIFF) is
behavior. The tool stays listed. The call returns a tool error that names
the next step. `download_asset` with `confirm=true` does not error and does
not write a file on this host. It returns a link plan. Small metadata and
preview files come back inline.

## download_asset

`asset` is a schema v2 key (`gec`, `gec_2`, `csi`, `sidd`, `csi_sidd`,
`sicd`, `cphd`, `metadata`, `stac_metadata`, `di_gif`, `file`, `file_tif`),
a product type in any case (`GEC`), or the original filename
(`umbra-py:filename`). `gec` is the primary GEC; `gec_2` is the next file
of that product, not an alias. A name that is not on the item lists those
v2 keys.

`url` is a STAC JSON URL, or a product filename or acquisition stem resolved
like `get_item`. On this host the catalog index resolves a filename or
stem. A local server resolves one the same way after `umbra index fetch`.

`confirm=false` (the default) returns the byte size and the asset href
without downloading. That shape is unchanged on this host and on a local
server.

On this host `confirm=true` returns a **download plan**, not a file and not
an error. `mode` is `"link"`, `downloaded` is false, and the plan names
`asset`, `filename`, the direct public `href`, and `bytes` (from the STAC
asset's `file:size` or `size`, otherwise HEAD `Content-Length`, or null
when neither is known). `how_to_fetch` has two commands:

```bash
curl -L -C - -o <filename> <href>
umbra download <item> --asset <asset> --dest <dir>
```

The hosted server does not write files and does not proxy radar products.
GEC, CSI, SIDD, SICD, and CPHD are always a link, whatever their size. An
unknown size is a link too. To save the file on your own disk, run a local
server and call `download_asset` with `confirm=true` there:

```bash
uvx --from 'umbra-py[mcp]' umbra-mcp
```

Metadata (`metadata`, `stac_metadata`) and detected-image previews
(`di_gif`) at or under 2 MiB come back inline (`mode` `"inline"`): JSON in
the result, a preview as an image block. A body over that cap is a link.
The host stops reading once the cap is crossed.

A local server still downloads when `confirm=true` (`{asset, path, bytes}`).

## Claude Code (recommended: one command)

Remote Streamable HTTP. Claude Code calls this transport `http`:

```bash
claude mcp add --transport http umbra https://api.umbra-py.space/mcp --scope user
```

Use this server for search, quicklooks, and download links. It does not
save Umbra files to your disk. For that, add a second, local server and
call `download_asset` with `confirm=true` on `umbra-local`:

```bash
claude mcp add --transport stdio umbra-local --scope user -- uvx --from 'umbra-py[mcp]' umbra-mcp
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

An optional plugin packages this same host, limited to the core tools, with
a short skill: `integrations/agent-plugin/umbra-sar/`. In Claude Code,
`/plugin marketplace add reesehammer/umbra-py`.

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
    },
    "umbra-local": {
      "command": "uvx",
      "args": ["--from", "umbra-py[mcp]", "umbra-mcp"]
    }
  }
}
```

`umbra` is the hosted server: search, quicklooks, and download links.
`umbra-local` is optional. Use it when you want `download_asset` to write
the file on your computer. The hosted server does not do that.

Desktop does not use `claude mcp add`. Do not copy the Desktop block into
Claude Code without adding `"type": "http"` on the hosted entry.

Already configured Desktop? Claude Code can import it:

```bash
claude mcp add-from-claude-desktop
```

## Other MCP clients (desktop GIS agents)

Any Streamable HTTP client uses `https://api.umbra-py.space/mcp`. No auth
and no session id.

A usual flow is `search_catalog`, then the `bbox` on each card as a
footprint, then `quicklook`, then `get_item`, then the GEC product `url`.
Umbra's bucket allows a cross-origin GET with Range requests. Whether a
given browser COG reader works is not verified. `download_asset` with
`confirm=true` on this host returns the direct href and a `curl` command
(`mode` `"link"`). It does not save the file. A local
`uvx --from 'umbra-py[mcp]' umbra-mcp` server downloads to disk.

A client that can limit which tools it loads can keep the
[stable core](#stable-tools).

## Local stdio (no public host)

Use this when you want the server on your laptop (`UMBRA_INDEX_DB`, a
Canopy token, offline use, or a download onto your own disk). The hosted
server hands back a link; this one writes the file when `download_asset`
is called with `confirm=true`. Needs [uv](https://docs.astral.sh/uv/).

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
