# Convert: SICD → geocoded COG

Turn a complex SICD product into a north-up, map-ready cloud-optimized GeoTIFF
using SICD's own image-projection model. Optional DEM terrain
orthorectification, geoid correction, and radiometric terrain flattening are
documented in [Terrain](terrain.md). Requires the `[convert]` extra.

Both converters also take `calibration=` — one of `sigma0`, `beta0`, `gamma0`
or `rcs` — which scales pixel power by the SICD's own `Radiometric` scale
factors so the output is a physical backscatter coefficient (or an absolute
radar cross-section) rather than relative brightness. It composes with the
terrain flattening: `rtc_model="facet"` with `calibration="gamma0"` is a
terrain-flattened gamma-nought product. Ask `sicd_calibration_types()` what a
given file supports — products that carry no scale factors, which includes most
of Umbra's open data, raise rather than returning an uncalibrated number that
looks calibrated.

::: umbra_py.sicd_to_geocoded_cog

::: umbra_py.sicd_to_amplitude_geotiff

::: umbra_py.sicd_calibration_types
