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
what is resident differs.

::: umbra_py.to_xarray

::: umbra_py.to_stack

::: umbra_py.stack_stats

::: umbra_py.to_geotiff

::: umbra_py.stack_to_geotiff
