# Examples

Runnable end-to-end examples live here. v0.1 ships the building blocks; the
notebooks below are planned for v0.2 and are great first contributions.

Planned:

- `01_hello_umbra.ipynb` — search the catalog, summarize and visualize an item.
- `02_download_and_open_gec.ipynb` — download a GEC GeoTIFF and open it with
  `rioxarray` / `rasterio`.
- `03_change_detection.ipynb` — basic multi-temporal change detection on a
  fixed site.
- `04_sicd_amplitude.ipynb` — convert a SICD to an amplitude GeoTIFF for
  inspection (uses the `convert` extra).

Until then, here's the minimal Python flow:

```python
from umbra_py import UmbraCatalog, download_item

catalog = UmbraCatalog()
items = list(catalog.search(start="2024-02-08", end="2024-02-08", limit=1))
print(items[0].summary())
download_item(items[0], dest_dir="downloads", assets=["GEC"])
```
