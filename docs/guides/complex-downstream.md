# Complex SICD/CPHD for downstream processors

This page is for people who need **phase** — a SICD or CPHD as input to
sarpy, isce3, a GPU backprojector, a custom former, or any other
processor outside umbra-py.

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

## CPHD for GPU backprojection (e.g. MatX `sarbp`)

CPHD is compensated phase history **before** image formation. GPU / research
formers (NVIDIA [MatX `examples/sarbp`](https://github.com/NVIDIA/MatX/tree/main/examples/sarbp)
is one) need that file, not a geocoded amplitude GeoTIFF.

umbra-py's job ends when a `.cphd` is on disk. It does **not** form the
image, run backprojection, convert CPHD to `.sarbp`, or choose pulses /
aperture / precision. `umbra convert` does not read CPHD.

Open CPHD is often **10–30+ GiB**. Size-check before pulling. `--place`
geocodes via Nominatim to a **rectangle**, so it can include nearby ground
outside the named site; use `--bbox` / `--intersects` for a tight AOI.
Do not treat one STAC id or S3 key as the forever demo file — collects
move, the weekly index lags, and several CPHDs can sit near the same
airport.

The open bucket has two trees: named campaigns under `sar-data/tasks/`
and UUID collects under `sar-data/task-data/`. Place / bbox search covers
both. `--area` matches a task-directory name (a label or a UUID), not a
geocoded place.

```bash
# Weekly snapshot (skip the live S3 walk — task-data/ is thousands of
# UUID directories).
umbra index fetch

# Illustration place only. --place is a Nominatim rectangle.
umbra search --local --product CPHD \
  --place "Hartsfield-Jackson Atlanta International Airport" --limit 5
# or:
# umbra search --local --product CPHD --bbox -84.47,33.61,-84.39,33.67 --limit 5

# HEAD the CPHD asset href from search (no `umbra head` verb) before
# fetching tens of GiB.

# Download only CPHD. Do NOT run `umbra convert`.
umbra download <stac-item-url> --asset CPHD --dest ./cphd
```

Prefer `--local` after `index fetch`. If a known open collect is missing
from the snapshot, omit `--local` (a live walk of `task-data/` is slow)
or refresh the snapshot.

```python
import requests
from umbra_py import CatalogIndex, download_item, geocode_place

bbox, name = geocode_place("Hartsfield-Jackson Atlanta International Airport")
print(name, bbox)

with CatalogIndex.from_release() as index:
    items = list(index.search(bbox=bbox, product_types=["CPHD"], limit=5))

assert items, "no CPHD in this box — try a wider place, --area, or omit --local"
href = items[0].asset_href("CPHD")
head = requests.head(href, allow_redirects=True, timeout=60)
head.raise_for_status()
nbytes = int(head.headers["Content-Length"])
print(items[0].id, f"{nbytes / (1024**3):.1f} GiB")

# Multi-tens-of-GiB: download only after the size check.
download_item(items[0], dest_dir="./cphd", assets=["CPHD"])
# stop — hand the .cphd to the former (MatX: cphd_to_sarbp_input.py → sarbp)
```

MCP is the same verbs: `search_catalog(place=…, products=["CPHD"])` then
`download_asset(…, asset="CPHD", confirm=False)` to size-check,
`confirm=True` to pull.

Then the former, unchanged. For MatX `sarbp`:

```bash
python cphd_to_sarbp_input.py ./cphd/*.cphd --image-size 8192 --aperture-angle 1.0 -o input_8k.sarbp
./examples/sarbp input_8k.sarbp -o output_image.raw
```

## CPHD + SICD pair for image formers (e.g. FastSAR)

Some formers need both files from **one** collect: the CPHD phase history
and the vendor SICD grid.
[FastSAR](https://github.com/saulpingerman/FastSAR) reads that pair with
`fastsar.io.read_cphd(..., sicd=)`. umbra-py searches the catalog and
downloads the two assets. FastSAR forms the image. FastSAR 0.2.0 can also
form from the CPHD alone — see the next section.

Search by place or by bbox, then check sizes. Open CPHD is large (often
multi-GiB, sometimes tens of GiB), and the SICD next to it is large too.
`--place` is a Nominatim rectangle and cannot be combined with `--bbox`.
`--product CPHD` keeps collects that expose phase history; the `assets`
line on a hit should also list `SICD` before you download the pair.
`--product` is "any of these", so repeating it does not require both.

```bash
umbra index fetch

# --place is a Nominatim rectangle. --bbox is min_lon,min_lat,max_lon,max_lat.
umbra search --local --product CPHD --place "Panama Canal" --limit 5
# umbra search --local --product CPHD --bbox -79.61,8.95,-79.55,9.00 --limit 5

# No `umbra head` verb. `umbra info <url> --json` lists each product `url`;
# HEAD those (Content-Length) before fetching.

# One item, both products. <item-url> is the `url` line from search:
# a *.stac.v2.json sidecar or a legacy *_METADATA.json.
# --asset is repeatable; name both so the default does not pull every product.
umbra download <item-url> --asset CPHD --asset SICD --dest ./pair
```

```python
import fastsar

col = fastsar.io.read_cphd("pair/..._CPHD.cphd", sicd="pair/..._SICD.nitf")
# FastSAR forms the image from col. umbra-py finished at the download.
```

The library call for the same pair is
`download_item(item, dest_dir="./pair", assets=["CPHD", "SICD"])`.

## CPHD to a north-up COG (FastSAR 0.2.0, then umbra convert)

[FastSAR 0.2.0](https://pypi.org/project/fastsar/0.2.0/)
([source](https://github.com/saulpingerman/FastSAR)) forms an image from a
frequency-domain CPHD and writes a SICD or a SIDD. `process_cphd` is that
one call. It does not need sarpy. Umbra spotlight CPHD is one of the
collections it is checked against. umbra-py does not form the image, run
backprojection, or preserve phase through the step below.

```python
import fastsar

fastsar.process_cphd("scene_CPHD.cphd", "scene_SICD.nitf")
```

The default product is a complex **slant-plane** SICD (`Grid.Type` PLANE,
`ImagePlane` SLANT), not Umbra's published RGAZIM / PFA SICD. Options
(area, plane, autofocus, weighting) are FastSAR's; see its
[product options](https://github.com/saulpingerman/FastSAR/blob/v0.2.0/docs/product-options.md).
Install FastSAR beside umbra-py (`pip install fastsar`). It is not an
umbra-py extra.

`umbra convert` does not read the CPHD. It reads the SICD FastSAR just
wrote. The default command calls `sicd_to_geocoded_cog`: amplitude only,
phase discarded, north-up EPSG:4326 cloud-optimized GeoTIFF, flat-earth HAE.
That is not an interferogram. `--calibrate` and `--noise-model measured`
still need a `Radiometric` block; `sicd_calibration_types()` says whether
the file has one (the SICD formed in the check below had none).

```bash
umbra convert scene_SICD.nitf scene_geocoded.tif
```

Checked with FastSAR 0.2.0 on one public Umbra spotlight CPHD (the search
below). `process_cphd` wrote a slant-plane SICD, and the default
`umbra convert` wrote an EPSG:4326 COG whose footprint covers that collect.
Pixels outside the slant image are nodata. The GeoTIFF carries
`Contains Umbra open data, licensed under CC BY 4.0.` A figure made from it
still needs the year notice in [Citing](../citing.md)
(`SAR-derived data © [YEAR] Umbra Lab, Inc.`).

Open CPHD ranges from about a tenth of a gigabyte to tens of gigabytes.
`--place` is a Nominatim rectangle and cannot be combined with `--bbox`.
`--start` / `--end` prune by acquisition date. Size-check the CPHD href
before downloading; there is no `umbra head` verb.

```bash
umbra index fetch

# Illustration only. This day returned two CPHDs (about 140 MB and 540 MB).
umbra search --local --product CPHD \
  --place "Tanna Island, Vanuatu" --start 2023-09-11 --end 2023-09-11 --limit 5

# HEAD the CPHD url from `umbra info <item-url> --json` (Content-Length).

# A missing ./cphd is written as a file. Create the directory first.
mkdir -p ./cphd
umbra download <item-url> --asset CPHD --dest ./cphd

# FastSAR forms the image. umbra-py does not.
python -c 'import fastsar; from pathlib import Path; fastsar.process_cphd(next(Path("./cphd").glob("*.cphd")), "scene_SICD.nitf")'

# Amplitude COG. Phase is discarded.
umbra convert scene_SICD.nitf scene_geocoded.tif
```

```python
import fastsar
from pathlib import Path

from umbra_py import CatalogIndex, download_item, geocode_place, sicd_to_geocoded_cog

bbox, name = geocode_place("Tanna Island, Vanuatu")
print(name, bbox)

with CatalogIndex.from_release() as index:
    items = list(
        index.search(
            bbox=bbox,
            product_types=["CPHD"],
            start="2023-09-11",
            end="2023-09-11",
            limit=5,
        )
    )

assert items, "no CPHD in this box — widen the date or the place"
Path("./cphd").mkdir(parents=True, exist_ok=True)
downloaded = download_item(items[0], dest_dir="./cphd", assets=["CPHD"])
fastsar.process_cphd(downloaded[0], "scene_SICD.nitf")  # FastSAR writes the SICD
sicd_to_geocoded_cog("scene_SICD.nitf", "scene_geocoded.tif")  # amplitude only
```

## After download

- **[sarpy](https://github.com/ngageoint/sarpy)** — reference SICD / CPHD
  reader.
- **GPU backprojection (not in umbra-py).** NVIDIA
  [MatX `sar_bp` / `sarbp`](https://github.com/NVIDIA/MatX/tree/main/examples/sarbp)
  forms an image from CPHD on the GPU after a sarpy-based
  `cphd_to_sarbp_input.py` conversion. umbra-py only gets the `.cphd` onto
  disk.
- **CPHD + SICD pair (not in umbra-py).**
  [FastSAR](https://github.com/saulpingerman/FastSAR)
  `read_cphd(..., sicd=)` reads both files from one collect. umbra-py
  downloads that pair; FastSAR forms the image. FastSAR 0.2.0
  `process_cphd` forms from the CPHD alone and writes a SICD;
  `umbra convert` can then geocode amplitude (phase discarded).
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
