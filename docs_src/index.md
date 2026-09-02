# umbra-py

**Search, preview, load, and convert [Umbra](https://umbra.space/open-data/)
open SAR data.**

Umbra publishes 16–25 cm SAR as **CC BY 4.0** open data, but **no search
API** — only a static STAC tree on S3. `umbra-py` is that layer, so the
archive feels as approachable as Sentinel-1 or Landsat. A community STAC
API (`umbra serve`) and MCP server sit on the same host.

!!! tip "Try it now — no install"
    Browse and search the whole open archive from your browser in the
    **[live showcase](https://umbra-py.space/showcase/)**: a
    zoomable whole-catalog map and an
    interactive explorer, hosted on GitHub Pages, no account or download
    required. Build your own with [`umbra showcase`](cli.md).

!!! note "Status"
    **v0.1.2.** Discovery, download, xarray loading,
    SICD → geocoded COG, change composites, chips, a STAC API
    (`umbra serve`, with a community host), and an MCP server all ship.
    This is not an InSAR toolbox — see [limitations](guides/limitations.md).
    Not affiliated with Umbra Lab, Inc.

## What it gives you

- **Discovery** — search Umbra's 17+ TB static STAC catalog by area, date, and
  product type, by place name, or by polygon — no search API required. Build a
  local SQLite index (or fetch the prebuilt weekly snapshot) for near-instant
  offline repeats.
- **Download** — resume-safe HTTPS downloads with integrity verification.
- **Convert** — turn a complex SICD product into a map-ready, geocoded
  cloud-optimized GeoTIFF (`umbra convert`), with optional DEM terrain
  orthorectification, geoid correction, and radiometric terrain flattening.
- **Load** — read a clipped/decimated scene straight into `xarray`.
- **Visualize** — interactive Folium maps, HTML thumbnail galleries, full-res
  browser viewers, before/after swipes, change composites, and time scans.
- **ML prep** — cut scenes into georeferenced training chips with metadata.
- **AI-native surfaces** — an MCP server (`umbra-mcp`), a read-only STAC API
  (`umbra serve`, hosted with MCP on the same Railway URL), native LangChain /
  LlamaIndex tools, plus model-backed natural-language search, scene
  description, and visual similarity.

## Next steps

- [Install](install.md) the right extras for what you need.
- Work through the [Quickstart](quickstart.md).
- Explore the [example notebooks](guides/notebooks.md).
- Reach for the [CLI reference](cli.md) or the API reference in the sidebar.

## License & attribution

`umbra-py` is Apache-2.0. Umbra's open imagery is licensed **CC BY 4.0** —
attribute *"Umbra Lab, Inc."* when you publish derived products. This is an
independent, unofficial toolkit and is not affiliated with or endorsed by
Umbra Lab, Inc.
