# Loading SAR as analysis-ready data

The guides in [`quicklook.md`](quicklook.md) and [`maps.md`](maps.md) answer
"*what does this scene look like?*". This one answers "*give me the pixels so I
can analyze them.*" `to_xarray` turns an Umbra acquisition into a georeferenced
[`xarray.DataArray`](https://docs.xarray.dev/) — the same on-ramp you'd use for
Sentinel-1 or Landsat — so the data drops straight into the scientific Python
stack.

Because the source is a cloud-optimized GeoTIFF read through GDAL's `/vsicurl/`
driver, only the window and resolution you ask for stream over HTTP range
requests. You can pull a small area out of a multi-GB scene without downloading
the whole file.

Loading needs the `load` extra (xarray + rasterio + numpy):

```bash
pip install "umbra-py[load]"
```

---

## 1. The bare minimum

```python
from umbra_py import UmbraCatalog, to_xarray

item = next(iter(UmbraCatalog().search(start="2024-02-08", end="2024-02-08", limit=1)))

da = to_xarray(item, max_size=2048)   # cap the longest side; decimates via overviews
print(da)                             # a 2D DataArray, dims ("y", "x")
```

`max_size` is worth setting for a first look: a full-resolution Umbra scene can
be tens of thousands of pixels on a side. Leaving it `None` reads native
resolution (pair that with a `bbox`, below).

---

## 2. What you get back

The array carries its georeferencing with it:

```python
da.dims              # ("y", "x")
da.attrs["crs"]      # e.g. "EPSG:32618" — the raster's native CRS
da.attrs["transform"]  # affine transform, 6-tuple
da.attrs["bounds"]   # (left, bottom, right, top) in that CRS
da.attrs["units"]    # "amplitude" (or "dB" when db=True)
da.attrs["item_id"], da.attrs["datetime"], da.attrs["platform"]
da.attrs["attribution"]  # "Contains Umbra open data, licensed under CC BY 4.0."
```

The `y` axis descends (north-up) and `x` ascends, both as cell-center
coordinates — so slicing by coordinate works the way you'd expect:

```python
da.sel(x=slice(x0, x1), y=slice(y0, y1))   # note y is descending
```

If you have [`rioxarray`](https://corteva.github.io/rioxarray/) installed, attach
the CRS for its `.rio` accessor (reproject, clip, write GeoTIFF):

```python
da = da.rio.write_crs(da.attrs["crs"])
da.rio.to_raster("scene.tif")
```

---

## 3. Just an area of interest

Pass a lon/lat `bbox` (EPSG:4326) and only that window is read — reprojected to
the raster's CRS first, so you give coordinates in degrees regardless of the
scene's projection:

```python
aoi = to_xarray(item, bbox=(-68.05, 10.45, -68.00, 10.50))
```

A `bbox` that doesn't overlap the scene raises `ValueError` rather than
returning an empty array.

---

## 4. Straight to a GeoTIFF (no array, or no Python)

If you just want a clipped/decimated file on disk — for QGIS, GDAL, or a
colleague who doesn't use Python — `to_geotiff` writes the same data without you
touching the array. It takes the same `bbox` / `max_size` / `db` options and
writes a single-band float32 GeoTIFF in the source CRS (nodata as `NaN`):

```python
from umbra_py import to_geotiff

to_geotiff(item, "aoi.tif", bbox=(-68.05, 10.45, -68.00, 10.50), max_size=4096)
```

There's a CLI for it too — point it at the item's `.stac.v2.json` URL:

```bash
umbra load <item-json-url> --out aoi.tif --bbox -68.05,10.45,-68.0,10.5 --max-size 4096
```

Both stream only the requested window of the cloud-optimized GeoTIFF — no full
download. (`to_xarray` is the in-memory path; `to_geotiff` / `umbra load` is the
file path. Internally the second just calls the first and writes the result.)

---

## 5. Decibels and masking

SAR amplitude has enormous dynamic range. `db=True` returns
`20*log10(amplitude)`, the radiometrically meaningful scale, and masks the
non-positive pixels it can't represent:

```python
db = to_xarray(item, max_size=2048, db=True)
db.plot.imshow(cmap="gray")          # xarray's matplotlib accessor
```

By default (`masked=True`) nodata and non-positive pixels become `NaN` so they
don't skew statistics; `da.mean()`, histograms, and percentile stretches just
work. Pass `masked=False` to get the raw values (zeros and all).

---

## 6. From here

Once it's a DataArray, the rest of the ecosystem is open: `dask` for
out-of-core, `scikit-image` for filtering, `matplotlib`/`hvplot` for custom
visualizations, or stacking several dates with `xarray.concat` for your own
time-series analysis. The [`change.md`](change.md) guide shows the built-in
multi-temporal composites if you'd rather not roll your own.
