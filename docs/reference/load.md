# Load & xarray

Read a clipped or decimated SAR scene straight into an `xarray.DataArray`, or
write a GeoTIFF. Requires the `[load]` extra.

`to_stack` is the multi-date companion: it co-registers several acquisitions
onto one shared grid and returns a `(time, y, x)` datacube — the step
`stackstac` / `odc-stac` play elsewhere in the STAC ecosystem, which can't be
pointed at Umbra because successive passes over a site arrive in whatever UTM
zone and extent each acquisition used. The shared grid is lon/lat by default;
`crs="utm"` (or any CRS) builds it in projected units instead, so its cells are
equal-area and a cell count is a measurement.

`stack_stats` reduces such a cube to the answer most multi-date searches are
after: one record per pass (its distribution and the signed decibel change
against the pass before it) plus a net first-to-last record, all plain JSON. It
is what `umbra stack --stats` prints and what the `stack_stats` agent tool
returns over MCP / LangChain / LlamaIndex. Its `blocks=N` argument adds the
spatial half of the answer — the scene cut into an N×N grid, each block
reporting its own net change, a compass label and lon/lat centre, and the pair
of passes it moved most between — so a change confined to one corner, which the
scene-wide mean dilutes, reads as *where* and *when*. `umbra stack --blocks N`
prints the same breakdown. Adding `block_series=True` (`umbra stack
--block-series`) keeps each block's *whole* pass-to-pass sequence rather than
only the interval it moved most in, which is what distinguishes a steady drift
from a single step.

A cube costs `max_size²` × the number of passes in memory, which is what
caps how much series can be stacked sharp. `to_stack(lazy=True)` (`umbra
stack --lazy`, the `[dask]` extra) defers each pass's read into one `dask`
chunk, and the consumers that reduce a cube — `stack_stats` and
`stack_to_geotiff` — walk it a slice at a time, so peak memory follows the
grid rather than the length of the series. The numbers are identical; only
what is resident differs. `chunk_size=N` (`umbra stack --lazy --chunk-size N`)
takes the same step *within* a pass: each slice is cut into N-square windows
read and written independently, so `max_size` stops being bounded by how much of
one scene fits in memory.

`stack_stats(windowed=True)` (`umbra stack --stats-windowed`) measures those
windows rather than whole passes, so a cube stacked sharper than a slice you can
hold is measurable and not only writable. Every count, mean, standard deviation
and change number stays exact — each is a sum, so a window folds in — while each
pass's `median` / `p5` / `p95` become histogram estimates good to about 0.05 dB,
because a percentile is the one statistic that needs the whole pass at once. The
summary says which it is (`quantile_method` / `quantile_bin_db`, plus a caveat),
so the two kinds of number are never confused.

`to_stack(speckle_filter=...)` (`umbra stack --speckle-filter`) averages
**speckle** down in every pass before the series is assembled — the one
uncertainty in those numbers that no correction in the conversion pipeline
touches, and the largest: a single look's power scatters about its surface's
true backscatter as widely as its own mean, so an unfiltered cell-to-cell
difference is mostly interference rather than change. `"boxcar"` averages the
window unconditionally (the multilook); `"lee"` averages only where a window is
no more variable than speckle alone explains, so edges and points survive. It is
the only surface that reaches Umbra's *published* GEC rasters —
[`umbra convert --speckle-filter`](convert.md) filters complex products in the
radar's own image space, before geocoding. Opt-in, because what it spends is
resolution, and both halves are recorded in the cube's `provenance` (the same
`speckle_filter` / `speckle_window` keys a converted raster carries), so
`stack_stats` states the trade and a later stack refuses to difference a
filtered cube against an unfiltered pass.

::: umbra_py.to_xarray

::: umbra_py.to_stack

::: umbra_py.stack_stats

::: umbra_py.to_geotiff

::: umbra_py.stack_to_geotiff
