# umbra-py

**A Python-first toolkit to make [Umbra](https://umbra.space/open-data/) SAR
open data easy to discover, load, download, and analyze.**

Umbra publishes very-high-resolution (down to ~16–25 cm) synthetic aperture
radar (SAR) imagery as open data under a permissive **CC BY 4.0** license. The
data is excellent, but getting started is hard: it ships in specialized formats
(SICD, SIDD, CPHD, GEC), is indexed by a large static STAC catalog, and the
existing tooling is low-level. `umbra-py` makes working with it feel as
approachable as working with Sentinel-1 or Landsat.

!!! tip "Try it now — no install"
    Browse and search the whole open archive from your browser in the
    **[live showcase](https://reesehammer.github.io/umbra-py/showcase/)**: a
    zoomable whole-catalog map and an
    interactive explorer, hosted on GitHub Pages, no account or download
    required. Build your own with [`umbra showcase`](cli.md).

!!! note "Status"
    v0.1 / early alpha. The discovery + download core works against Umbra's
    live catalog today; processing helpers are minimal and growing.

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
  (`umbra serve`), native LangChain / LlamaIndex tools, plus model-backed
  natural-language search, scene description, and visual similarity.

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
