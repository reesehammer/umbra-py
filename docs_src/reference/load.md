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
returns over MCP / LangChain / LlamaIndex.

::: umbra_py.to_xarray

::: umbra_py.to_stack

::: umbra_py.stack_stats

::: umbra_py.to_geotiff

::: umbra_py.stack_to_geotiff
