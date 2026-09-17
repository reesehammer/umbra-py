# Complex SICD/CPHD for downstream processors

This page is for people who need **phase** — a SICD or CPHD as input to
sarpy, isce3, a custom former, or any other processor outside umbra-py.

umbra-py already **searches and downloads** those products. It does **not**
re-form Polar Format geometry, load a complex array, or build an
interferogram. The rest of the docs lean toward `GEC` and `umbra convert`
because those are the map-ready, amplitude paths. Convert **detects
amplitude and discards phase**. If you need the complex pixels, download
and stop.

## Open Umbra SICD is RGAZIM / PFA

Umbra's published open SICDs are **spotlight** products on an **RGAZIM**
(range-azimuth) grid, formed with the **Polar Format Algorithm (PFA)**.

That is a different class from Capella open-data **stripmap** SICDs, which
are typically **RGZERO** / RMA-INCA — the geometry range-Doppler engines
(including isce3's) already speak. A converter that only ingests RGZERO
should **reject** an Umbra open SICD, not silently treat PFA as
zero-Doppler.

umbra-py does not rewrite PFA into range-Doppler. It will not grow a
Capella client. Fetch the NITF here; geometry conversion belongs in the
processor that will use it.

## SICD vs CPHD

**SICD** is a focused complex image in the radar slant plane (NITF).
**CPHD** is compensated phase history *before* image formation. umbra-py
classifies and downloads both. It does not form an image from CPHD, and
`umbra convert` does not read CPHD.

## Recipe: index → search → size → download → stop

Do not crawl S3 for a repeat search. Do not convert.

```bash
# 1. Weekly catalog snapshot (skip the live S3 walk).
umbra index fetch

# 2. Find complex products. --product is repeatable.
#    --place geocodes via Nominatim to a *rectangle* (mutually exclusive
#    with --bbox) and can include nearby ground outside the named place.
#    --area is an Umbra task-directory name (fast for a known site).
umbra sites --local --product SICD --place "Sunny Isles Beach" --top 5
umbra search --local --product SICD --place "Sunny Isles Beach" --limit 5
umbra search --local --product SICD --product CPHD --area Centerfield --limit 5
umbra search --local --product SICD --bbox -80.15,25.90,-80.10,25.95 --limit 5

# 3. Size-check before fetching. Open SICDs are often multi-GB NITFs.
#    There is no `umbra` HEAD verb: take the `url` line from search
#    (the STAC sidecar) and HEAD the asset href, or use the Python
#    sketch below.

# 4. Download the complex product. --asset is repeatable; default is every
#    product present — pass SICD or CPHD explicitly.
umbra download https://example.com/item.stac.v2.json --asset SICD --dest ./sicd

# 5. Stop. Do *not* run `umbra convert` if you need phase.
```

`--local` reads the snapshot. Omit it only if you intend to walk the live
bucket (slow). `--place` and `--bbox` cannot be combined; `--intersects`
is the polygon alternative to the rectangle.

## Python

`CatalogIndex.search` has no `place=` argument — that flag is CLI-only.
Geocode to a bbox, or pass `area=` / `bbox=` directly.

```python
import requests
from umbra_py import CatalogIndex, download_item, geocode_place

bbox, name = geocode_place("Sunny Isles Beach")  # Nominatim rectangle
print(name, bbox)

with CatalogIndex.from_release() as index:
    items = list(index.search(bbox=bbox, product_types=["SICD"], limit=5))

for item in items:
    href = item.asset_href("SICD")
    head = requests.head(href, allow_redirects=True, timeout=60)
    head.raise_for_status()
    nbytes = int(head.headers["Content-Length"])
    print(item.id, f"{nbytes / 1e9:.1f} GB")

# Multi-GB NITFs: download only after the size check.
assert items, "no SICD in this box — try area= or a wider place"
download_item(items[0], dest_dir="./sicd", assets=["SICD"])
```

Swap `"SICD"` for `"CPHD"` when you want phase history. After
`download_item`, umbra-py is done.

## After download

- **[sarpy](https://github.com/ngageoint/sarpy)** — reference SICD / CPHD
  reader.
- **PFA → range-Doppler (not in umbra-py).** Piyush S. Agram, [*Modifying
  Range-Doppler geometry frameworks to process Spotlight SAR imagery in
  Polar Format*](https://arxiv.org/abs/2503.07889) (2025), with public
  proof-of-concept code at
  [piyushrpt/PFA2RDAgeometry](https://github.com/piyushrpt/PFA2RDAgeometry),
  validated on an Umbra SICD.
- **[isce3](https://github.com/isce-framework/isce3)** — no SICD reader
  today; its geometry is range-Doppler, not PFA. Ingest of these files is
  that project's problem, not a convert flag here.

## Related

- [Limitations](limitations.md) — convert is amplitude; this is not an
  InSAR toolbox.
- [Convert](../reference/convert.md) — the amplitude / geocode path, when
  you *do* want a map-ready GeoTIFF.
- [Notebook 07](notebooks.md) — SICD → amplitude GeoTIFF (phase discarded).
- [Used in research](research.md) — ISR / amplitude chips, a different
  product from a complex stack.
