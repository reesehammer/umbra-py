---
name: umbra-sar
description: |
  Search, preview, and open Umbra open SAR (CC BY 4.0) when the user asks for SAR, radar, Umbra, or night- and cloud-independent imagery of a place.
  搜索、预览并打开 Umbra 开放 SAR 数据（CC BY 4.0）。
---

# Umbra open SAR

Use this when the user asks for SAR, radar, Umbra, or imagery of a place that
should not depend on daylight or clear skies.

## Search

Call `search_catalog` with a place or a bbox, optional `start` and `end`,
`limit` of 20 or less, and `canonical` true. Report how many cards came back
and the newest `datetime` on them. The open archive can lag weeks behind
Umbra's newest collects, so always state the newest date you found and never
say "latest" without it.

## Map

Turn each card's `bbox` into a GeoJSON polygon. `bbox` is
`[west, south, east, north]`. Assemble one FeatureCollection and set
properties `id`, `datetime`, `platform`, `resolution`, and `polarizations`.
Add that collection with the host map's GeoJSON tool, then fit the extent.

## Preview

Call `quicklook` with `url` set to the card's `stac_href`. The picture is a
512 px baked preview. Do not describe detail finer than that image shows.

## Open pixels

Call `get_item` on the chosen `stac_href`, then take the `GEC` product `url`.
That file is a cloud-optimized GeoTIFF on Umbra's public bucket. The bucket
allows cross-origin GET with Range requests, so a map's COG layer may be able
to stream it directly. This is not verified for every reader, and if it fails,
fall back to the quicklook plus a local download.

Call `download_asset` with `confirm` true. On the hosted server that returns
a link plan (`mode` `link`, `downloaded` false) with the public `href` and
a `curl -L -C -` command. It does not save the file. Small metadata and
preview files may come back inline. For a copy on your disk, run a local
server and call `download_asset` there:
`uvx --from 'umbra-py[mcp]' umbra-mcp`.

## Honesty

A GEC is detected amplitude, already map-projected. SICD and CPHD are complex
products for the user's own tools. umbra-py does not form images, do InSAR, or
preserve phase. Never difference HH against VV. Always carry this line:
Contains Umbra open data, licensed under CC BY 4.0.

## Limits

The hosted host allows 120 requests per minute. A refused tool (a composite,
a stack, or narration) means run a local server:
`uvx --from 'umbra-py[mcp]' umbra-mcp`.
