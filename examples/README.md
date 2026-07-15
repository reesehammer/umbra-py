# Examples

Runnable end-to-end examples live here — a gallery of Jupyter notebooks plus
task-focused Markdown guides.

## Notebooks

Self-contained, self-checking Jupyter notebooks. Each one uses a small,
deterministic search and ends its code cells with `assert`s, so running it
top-to-bottom is both a tutorial and a live smoke test of the documented flow.
They stream from Umbra's public bucket, so run them with network access.

- [`01_hello_umbra.ipynb`](01_hello_umbra.ipynb) — the three-line tour: search
  the catalog, summarize an item, and render it as a SAR quicklook. Also shows
  the zero-glue geopandas (`__geo_interface__`) and model-ready
  (`to_llm_context`) paths (`viz` extra).
- [`02_download_and_open_gec.ipynb`](02_download_and_open_gec.ipynb) — stream a
  GEC into an analysis-ready `xarray.DataArray` (no full download), analyze it,
  and round-trip the CRS with `rioxarray` (`load` extra).
- [`03_change_detection.ipynb`](03_change_detection.ipynb) — find a site imaged
  more than once, pick two passes, and composite the change into one color
  image (`viz` extra).

The committed notebooks ship with **cleared outputs**. `tests/test_examples.py`
validates them offline on every CI run (well-formed, code cells parse, every
`umbra_py` symbol they reference is public, CC-BY attribution present) and can
execute them end-to-end under `pytest -m network` when `nbclient` and the render
extras are installed.

Still planned (good first contributions):

- `04_sicd_amplitude.ipynb` — convert a SICD to an amplitude GeoTIFF for
  inspection (uses the `convert` extra); tracked with the SICD → geocoded COG
  work in `docs/STRATEGY.md` 5.5.

## Guides

- [`gallery.md`](gallery.md) — browse a search as a contact sheet of streamed
  SAR thumbnails in one self-contained HTML page, with copy-paste asset/STAC
  URLs and place-name (`--place "California"`) search (CLI + Python; uses the
  `viz` extra).
- [`load.md`](load.md) — load an acquisition into a georeferenced `xarray`
  DataArray for analysis (Python; uses the `load` extra).
- [`maps.md`](maps.md) — render search results as interactive maps or GeoJSON
  (incl. searching by `--place` name).
- [`quicklook.md`](quicklook.md) — turn a single acquisition into a standalone
  SAR image (CLI + Python), with decibel scaling and matplotlib pseudo-color.
- [`change.md`](change.md) — composite 2–3 acquisitions of the same site into
  a color change-detection image (CLI + Python).
- [`swipe.md`](swipe.md) — compare two passes of a site with an interactive
  before/after swipe map (CLI + Python).

## The minimal Python flow

```python
from umbra_py import UmbraCatalog, download_item

catalog = UmbraCatalog()
items = list(catalog.search(start="2024-02-08", end="2024-02-08", limit=1))
print(items[0].summary())
download_item(items[0], dest_dir="downloads", assets=["GEC"])
```
