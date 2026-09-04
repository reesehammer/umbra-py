# umbra-py

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/reesehammer/umbra-py/actions/workflows/ci.yml/badge.svg)](https://github.com/reesehammer/umbra-py/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/reesehammer/umbra-py/branch/main/graph/badge.svg)](https://codecov.io/gh/reesehammer/umbra-py)
[![Docs](https://img.shields.io/badge/docs-umbra--py.space-informational.svg)](https://umbra-py.space/)

**Search, preview, load, and convert [Umbra](https://umbra.space/open-data/) open SAR data.**

Umbra publishes 16–25 cm SAR as CC BY 4.0 open data, but **no search API** —
only a 17+ TB S3 bucket and a static STAC tree. `umbra-py` is that layer:
search, preview, download, and analysis-ready arrays without the usual 500
lines of glue. A community STAC API (`umbra serve`) and MCP server sit on
the same host, so `pystac-client` and Claude can query the archive with
nothing installed.

📖 **Docs:** [umbra-py.space](https://umbra-py.space/)
· **Showcase:** [browse the archive in the browser](https://umbra-py.space/showcase/)
(no install)

> **Status:** v0.1.2. Discovery, download, xarray loading,
> SICD → geocoded COG, change/timescan composites, chips, a STAC API
> (`umbra serve`, with a community host), and an MCP server all ship. This is
> **not** an InSAR toolbox (phase is not preserved through convert). Not
> affiliated with Umbra Lab, Inc.

## Install

```bash
pip install umbra-py              # core: search + download + metadata
pip install "umbra-py[load]"      # + xarray / rasterio
pip install "umbra-py[viz]"       # + quicklooks, maps, galleries
pip install "umbra-py[convert]"   # + SICD → geocoded COG
pip install "umbra-py[all]"       # convert + load + viz + export
```

Python 3.10+. Other extras (`dask`, `serve`, `mcp`, `ai`, `langchain`,
`llamaindex`) are listed in the [install guide](https://umbra-py.space/install/).

## Five minutes to a scene

Fetch the weekly catalog snapshot, then search and preview offline. A live
walk of the bucket (`umbra search` without `--local`) works but is slow.

```bash
pip install "umbra-py[viz,load]"
umbra index fetch
umbra search --local --area Centerfield --product GEC --limit 3
umbra gallery --local --area Centerfield --limit 6 --out gallery.html --db
```

```python
from umbra_py import CatalogIndex, to_xarray

with CatalogIndex.from_release() as index:
    item = next(iter(index.search(area="Centerfield", product_types=["GEC"], limit=1)))

# Stream a downsampled window over HTTP — no multi-GB download. Needs [load].
da = to_xarray(item, max_size=1024, db=True)
print(item.summary())
```

If the snapshot is missing, the same search against the live bucket is
`UmbraCatalog().search(...)` / `umbra search --area Centerfield`.

## What you can do

More detail, options, and caveats live in the
[docs](https://umbra-py.space/).

**Search** by bbox, place name, polygon, or Umbra task (`area=`).
`--local` reads the snapshot; omit it to walk S3.

```python
from umbra_py import UmbraCatalog

for item in UmbraCatalog().search(area="Centerfield", product_types=["GEC"], limit=5):
    print(item.summary())
```

**Preview** without downloading the scene: `umbra gallery`, `umbra quicklook
<stac-url> --out scene.png --db`, `umbra view <stac-url>` (full-res tiles),
or `umbra change --area Centerfield --out change.png`.

**Load** a geocoded GEC into xarray or a GeoTIFF (`to_xarray`, `to_geotiff`,
`to_stack`). Needs `[load]`.

**Convert** a SICD to a north-up COG (`sicd_to_geocoded_cog`, `umbra convert`).
Needs `[convert]`. Open products generally have no radiometric metadata, so
`--calibrate` / `--noise-model measured` refuse rather than invent numbers.
See [limitations](https://umbra-py.space/guides/limitations/).

**Chip** scenes into georeferenced ML tiles for SR / ATR-style benchmarks from
open Umbra GEC/SICD: `umbra chips --area Centerfield --out chips/`. See the
[ISR training-set cookbook](https://github.com/reesehammer/umbra-py/blob/main/examples/09_isr_training_set.ipynb)
and [Used in research](https://umbra-py.space/guides/research/).

**Drive it from an agent.** Copy-paste recipes for Claude Desktop and Claude
Code: [Connect Claude (MCP)](https://umbra-py.space/mcp/).

Zero-install remote MCP (no `uvx`):

```bash
# Claude Code
claude mcp add --transport http umbra https://api.umbra-py.space/mcp --scope user
```

```json
{
  "mcpServers": {
    "umbra": {
      "url": "https://api.umbra-py.space/mcp"
    }
  }
}
```

Paste that JSON into Claude Desktop (`claude_desktop_config.json`). Claude
Code needs `"type": "http"` on the same URL — see the MCP page.

Local stdio (server on your machine):

```bash
uvx --from 'umbra-py[mcp]' umbra-mcp
```

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

That command is published to the [MCP registry](https://registry.modelcontextprotocol.io/)
as `io.github.reesehammer/umbra-mcp`. STAC for `pystac-client` / QGIS is
[https://api.umbra-py.space/](https://api.umbra-py.space/) (not `/mcp`).
`docker compose up` is the one-command self-host.

<!-- mcp-name: io.github.reesehammer/umbra-mcp -->

## What the data looks like

| Asset | What it is | Use it for |
|-------|------------|------------|
| `GEC`  | Geocoded cloud-optimized GeoTIFF | Map-ready imagery. **Start here.** |
| `CSI`  | Color sub-aperture GeoTIFF | Quick-look RGB, not a measurement |
| `SIDD` | Geocoded detected image (NITF) | Detected imagery in a standard format |
| `SICD` | Complex data in the radar slant plane (NITF) | Phase-preserving work, InSAR *inputs* |
| `CPHD` | Compensated phase history | Custom image formation |

`umbra-py` downloads SICD/CPHD and can geocode a SICD to amplitude. It does
not form interferograms or compute coherence.

## Data license & attribution

Umbra's imagery is **CC BY 4.0**. If you use or redistribute the data or
derived products you must attribute Umbra, e.g.:

> Contains Umbra open data, licensed under CC BY 4.0.

`umbra-py` itself is **Apache 2.0** ([LICENSE](LICENSE)). The two licenses
are independent and compatible.

## Citing umbra-py

Machine-readable metadata lives in [CITATION.cff](CITATION.cff). GitHub
renders it as a **"Cite this repository"** button. Please also honor the
CC BY 4.0 line above for any Umbra data you use.

## Community

- [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md) · [Security](SECURITY.md)
- [Example notebooks](examples/) · [Limitations](https://umbra-py.space/guides/limitations/)

## Acknowledgements

Built on the SAR open-source community, including
[`sarpy`](https://github.com/ngageoint/sarpy) and Umbra's open data program.
**Not affiliated with or endorsed by Umbra Lab, Inc.**
