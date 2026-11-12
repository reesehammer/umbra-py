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

SICD and CPHD carry the sensor trajectory. See
[State vectors](#state-vectors).

## State vectors

SICD and CPHD carry an ECEF trajectory. The GEC `*.stac.v2.json` sidecar
carries scene-centre angles. `read_sicd_xml` reads a SICD's XML over HTTP
range requests and returns the document. umbra-py does not parse a CPHD,
and reading either product's metadata does not form an image.

Checked on 2026-11-12 against five public collects (the stems in the table
below) and the CPHDs of two of them. All five SICDs are SICD 1.3.0
(`urn:SICD:1.3.0`). Both CPHDs start `CPHD/1.1.0`.

### SICD

The trajectory is inside the NITF.

- `Position/ARPPoly` is an X, Y, Z polynomial in time. On these five files
  each axis is degree 5 (`order1="5"`, `Coef` with `exponent1` 0 through
  5). At `SCPCOA/SCPTime`, the polynomial and its first two derivatives
  reproduce `ARPPos`, `ARPVel`, and `ARPAcc` (residuals under
  3×10⁻⁸ m and 5×10⁻⁶ m/s).
- `SCPCOA` is the aperture-centre state: `ARPPos`, `ARPVel`, `ARPAcc`,
  plus `DopplerConeAng`, `GrazeAng`, `IncidenceAng`, `AzimAng`, and
  `SlantRange`.
- The same `Position` block also has `GRPPoly`, `TxAPCPoly`, and
  `RcvAPC/RcvAPCPoly`.
- `CollectionInfo` parameter `collect_id` equals the sidecar property
  `umbra:collect_id`. On these five, `ephemeris_source` is
  `POST_PASS_SMOOTHED`.

### CPHD

On `2025-07-15-06-10-12_UMBRA-08` and `2024-09-07-17-26-59_UMBRA-08`:

- The XML `PVP` block declares per-vector `TxPos`, `TxVel`, `RcvPos`,
  `RcvVel`, and `SRPPos`, each `X=F8;Y=F8;Z=F8`. The samples are in the
  binary PVP block.
- `ReferenceGeometry/Monostatic` has one `ARPPos` and one `ARPVel`. On the
  2025-07-15 collect those values match the SICD `SCPCOA` vectors.

`download_item(item, dest_dir="./cphd", assets=["CPHD"])` and
`umbra download <sidecar-url> --asset CPHD --dest ./cphd` fetch the file.
Nothing in umbra-py reads the PVP block.

### GEC sidecar

On these five collects the JSON next to the GeoTIFF is the
`*.stac.v2.json` sidecar. `view:azimuth`, `view:incidence_angle`,
`umbra:grazing_angle_degrees`, and `umbra:slant_range_meters` equal
`SCPCOA` `AzimAng`, `IncidenceAng`, `GrazeAng`, and `SlantRange`.
`sat:orbit_state` is `ascending` or `descending`. A walk of every key
found no position polynomial and no velocity vector. A sibling
`*_METADATA.json` and `*_STAC-METADATA.json` returned HTTP 404.

The three squint properties are a separate field, covered below.

### Reading the SICD XML

`read_sicd_xml(src)` takes the NITF URL or a local path. It returns
`(xml, bytes_read, product_bytes)`. `session` is optional and
keyword-only. No `umbra` command prints this XML.

`item.asset_href("SICD")` is the public NITF. The sidecar may also list a
`*_SICD_MM.xml`, which becomes asset `sicd_2`. On the 2025-07-15 collect
that href stayed on `s3://prod-prod-processed-sar-data/...`, and the open
bucket returned 404 for the filename and for `<stem>_SICD.xml`. Pass the
NITF to `read_sicd_xml`.

`CatalogIndex.search` and `UmbraCatalog.search` take `product_types=`
(the CLI flag is `--product`), `area=` (`--area`, the task-directory
name; for a `task-data` collect that is `umbra:task_id`), and `limit=`.
They have no `collect_id` argument, and `umbra search` has no
`--collect-id` flag. The id is `properties["umbra:collect_id"]`.
`canonical=True` is a keyword of those two `search` methods.
`umbra search` has no `--canonical` flag. `place=` is not a `search`
argument; `--place` is CLI-only, as in the recipe below.

```python
from xml.etree import ElementTree

from umbra_py import CatalogIndex, read_sicd_xml

with CatalogIndex.from_release() as index:
    hits = [
        item
        for item in index.search(area=task_id, product_types=["SICD"], limit=20)
        if item.properties.get("umbra:collect_id") == collect_id
    ]

xml, nbytes, product_bytes = read_sicd_xml(hits[0].asset_href("SICD"))
root = ElementTree.fromstring(xml)
ns = {"s": "urn:SICD:1.3.0"}
arp_x = root.findtext("s:SCPCOA/s:ARPPos/s:X", namespaces=ns)
```

On these five NITFs the call transferred 34,589–35,955 bytes. The
products are 1.02–11.34 GB. `umbra download --asset SICD` and
`download_item(..., assets=["SICD"])` fetch the whole NITF.
`download_url(..., max_bytes=)` and `umbra download --max-bytes` cap a
transfer that starts at byte 0. `read_sicd_xml` is the call that locates
the XML from the NITF header. `umbra preflight` reads the same XML and
reports calibration, noise level, and SCPCOA look geometry. It does not
return `ARPPoly`.

### Sidecar squint

Each sidecar has:

- `umbra:squint_angle_exploitation_degrees`, signed
- `umbra:squint_angle_degrees_off_broadside`, its absolute value
- `umbra:squint_angle_engineering_degrees`

On these five sidecars, and on 400 SICD items from five date windows of
the community search API the same day, the off-broadside value is the
absolute value of the exploitation angle, with no exception. The
engineering angle is `90 + exploitation` when `sar:observation_direction`
is `right`, and `exploitation - 90` when it is `left`. On right-looking
`2023-11-09-05-12-02_UMBRA-05`, exploitation is -57.465° and engineering
is 32.535°. On left-looking `2024-09-07-17-26-59_UMBRA-08`, exploitation
is +57.512° and engineering is -32.488°.

Two angles computed from the matching SICD:

- **Slant plane.** `|DopplerConeAng - 90°|`. On these files
  `DopplerConeAng` equals the angle between `ARPVel` and the vector from
  `ARPPos` to `GeoData/SCP/ECF`.
- **Ground plane.** In the WGS 84 geodetic east-north plane at that
  scene-centre point, the clockwise angle from the horizontal part of
  `ARPVel` to the horizontal part of `(scene-centre ECF - ARPPos)`,
  wrapped to ±180°. The table is the absolute difference between that
  angle and the nearer of +90° and -90°.

| Collect | Look | Slant ° | Ground ° | Sidecar off-broadside ° |
| --- | --- | ---: | ---: | ---: |
| `2024-02-09-05-34-49_UMBRA-07` | left | 0.000017 | 0.037 | 0.063 |
| `2024-12-27-16-42-10_UMBRA-09` | right | 0.000005 | 0.010 | 0.105 |
| `2025-07-15-06-10-12_UMBRA-08` | right | 1.630 | 3.509 | 3.867 |
| `2024-09-07-17-26-59_UMBRA-08` | left | 15.185 | 57.333 | 57.512 |
| `2023-11-09-05-12-02_UMBRA-05` | right | 23.936 | 57.549 | 57.465 |

The sidecar value and the slant-plane angle differ on every row. On the
two largest squints the slant-plane angle is 15.185° and 23.936° where
the sidecar says 57.512° and 57.465°. The ground-plane angle is 0.179°
and 0.084° from the sidecar on those two rows, and 0.358° from it on the
3.867° row. On the two near-broadside rows both computed angles are under
0.04° and the sidecar value is 0.063° and 0.105°. These five collects do
not identify the sidecar field with either angle.

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
downloads the two assets. FastSAR forms the image.

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
  downloads that pair; FastSAR forms the image.
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
