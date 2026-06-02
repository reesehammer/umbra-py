# SAR quicklooks

`umbra quicklook` is the lowest-friction way to *see* an Umbra acquisition: it
streams a downsampled preview of the cloud-optimized GeoTIFF via HTTP range
requests (no multi-gigabyte download, no Folium map, no GIS) and writes a plain
image file. The maps in [`maps.md`](maps.md) answer "*where* did this search
land?"; a quicklook answers "*what does this scene actually look like?*".

Quicklooks need the `viz` extra (rasterio + numpy + Pillow + matplotlib):

```bash
pip install "umbra-py[viz]"
```

---

## 1. The bare minimum

```bash
umbra quicklook "<item-json-url>" --out scene.png
```

```python
from umbra_py import UmbraCatalog, save_quicklook

item = next(UmbraCatalog().search(start="2024-02-08", end="2024-02-08",
                                  product_types=["GEC"], limit=1))
save_quicklook(item, "scene.png")
```

Both read a downsampled overview of the item's GEC image in its native
(already geocoded, north-up) projection, apply a SAR-tuned contrast stretch,
and write the result. `quicklook(item, ...)` returns a `PIL.Image` if you'd
rather display or post-process it in a notebook.

---

## 2. Getting an `<item-json-url>`

The URL is the item's `.stac.v2.json` sidecar. The easiest source is
`umbra search`, which prints a `url:` line per result:

```bash
umbra search --start 2024-01-01 --end 2024-12-31 --product GEC --limit 5
```

Copy any `url:` value and hand it to `quicklook`. **Quote it** — Umbra's
named-task directories contain spaces (`%20`) and commas. `umbra info <url>`
is a good dry run to confirm the URL parses before rendering.

---

## 3. Every option

| Flag | Domain | Default | Notes |
| ---- | ------ | ------- | ----- |
| `ITEM_URL` (positional) | any STAC `.stac.v2.json` URL | — | required |
| `--out` | any path | — | required; **extension picks the format** (`.png`, `.jpg`, …) |
| `--asset` | `GEC` \| `CSI` \| `SIDD` \| `SICD` \| `CPHD` | `GEC` | `GEC`/`CSI` are the sensible targets (see §6) |
| `--max-size` | positive integer (pixels) | `2048` | larger = sharper but more bytes (~quadratic) and more speckle |
| `--db` | flag | off | decibel (log-amplitude) stretch |
| `--colormap` | any matplotlib colormap name | _(grayscale)_ | e.g. `viridis`, `magma`, `inferno` |
| `--percentile` | `"low,high"` | `"2,98"` | contrast cut percentiles |

The Python `quicklook` / `save_quicklook` functions take the same options as
keyword arguments (`asset=`, `max_size=`, `db=`, `colormap=`,
`percentile=(2.0, 98.0)`).

---

## 4. Rendering modes (`--db` × `--colormap`)

These two options are the only discrete axes — together they give four
distinct looks:

| Combination | Result |
| ----------- | ------ |
| _(neither)_ | Linear-amplitude **grayscale** — faithful but dark |
| `--db` | **Decibel** grayscale — reveals texture/structure the linear stretch crushes to black |
| `--colormap magma` | Linear-amplitude **pseudo-color** |
| `--db --colormap magma` | Decibel **pseudo-color** — the most legible SAR look |

SAR amplitude spans an enormous dynamic range, so a straight linear scaling
looks almost black. `--db` converts amplitude to decibels (`20·log10`) before
stretching — the radiometrically-correct view that brings out terrain texture
and urban structure. `--colormap` then maps the stretched values through any
matplotlib colormap for a pseudo-colored quicklook.

```bash
# Decibel pseudo-color, JPEG output
umbra quicklook "<url>" --out scene.jpg --db --colormap magma
```

---

## 5. The scalar knobs

These are orthogonal to the rendering mode above.

### Output format (`--out` extension)

Pillow infers the format from the extension: `.png` (lossless, supports the
transparent invalid-pixel mask), `.jpg` / `.jpeg` (smaller; the transparency is
flattened onto black automatically), `.tif`, etc.

### Resolution (`--max-size`)

The longest image dimension, in pixels. `512` for a fast thumbnail; `4096` to
zoom in on a single scene. Remember SAR is inherently speckled — higher
resolutions reveal more detail *and* more noise. Only the bytes for the chosen
overview level are fetched, so smaller is also faster.

### Contrast (`--percentile`)

The low/high percentile cut for the stretch, computed on valid pixels only.
`"2,98"` (default) is a good balance; `"0,100"` disables clipping (washed out);
`"5,95"` is punchier.

### Product (`--asset`)

`GEC` (default) and `CSI` are detected amplitude/quick-look rasters and render
correctly. The flag also accepts `SIDD`/`SICD`/`CPHD`, but those aren't
single-band amplitude images — the stretch will error or produce nonsense. Use
GEC unless you specifically know the CSI product is what you want.

---

## 6. Recipe gallery

Complete, copy-pasteable patterns.

### The legible default

Decibel pseudo-color is the look most people want from SAR:

```bash
umbra quicklook "<url>" --out scene.png --db --colormap magma
```

### Fast contact-sheet thumbnail

```bash
umbra quicklook "<url>" --out thumb.png --max-size 512 --percentile 5,95
```

### High-resolution, no clipping

```bash
umbra quicklook "<url>" --out big.png \
    --max-size 4096 --percentile 0,100 --colormap viridis
```

### Search → render in one Python flow

```python
from umbra_py import UmbraCatalog, save_quicklook

catalog = UmbraCatalog()
for i, item in enumerate(catalog.search(
    bbox=(-118.42, 33.90, -118.36, 33.96),   # LAX
    start="2024-01-01", end="2024-12-31",
    product_types=["GEC"], max_per_task=1, limit=10,
)):
    save_quicklook(item, f"lax_{i:02d}.png", db=True, colormap="magma")
```

### Post-process the image in a notebook

```python
from umbra_py import quicklook

img = quicklook(item, db=True, max_size=1024)   # a PIL.Image
img.rotate(90).save("rotated.png")
```

---

## 7. Troubleshooting

- **`MissingDependencyError: 'rasterio' is required`** — install the extra:
  `pip install "umbra-py[viz]"`.
- **`Image has no valid pixels to stretch`** — the fetched overview was all
  nodata / non-positive (e.g. a scene-edge tile, or a non-amplitude `--asset`
  like SICD/CPHD). Use `--asset GEC`.
- **`Protocol "s3" not supported`** — you're on an old build. `asset_href`
  now rewrites Umbra's private-bucket `s3://` hrefs to the public sibling URL;
  pull the latest and retry.
- **`CURL error: SSL certificate problem`** — your network sits behind a proxy
  whose CA GDAL's curl doesn't trust. Point GDAL at the right bundle
  (`CURL_CA_BUNDLE=/path/to/ca.pem`) or, as a last resort on a trusted network,
  `GDAL_HTTP_UNSAFESSL=YES`.
- **Output is too dark** — add `--db`, and/or widen the stretch with a tighter
  `--percentile` like `2,98` → `5,95`.
- **File is bigger/slower than expected** — `--max-size` is the lever; it's
  quadratic in filesize and fetch cost.
