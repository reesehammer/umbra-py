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

`sicd_to_geocoded_cog` also takes `bbox=` (`umbra convert --clip-bbox`) — a
lon/lat rectangle to convert *instead of the whole scene*. A SICD is tens of
square kilometres at 16–25 cm, and every step above is proportional to it, so
keeping a site out of a collect otherwise costs the whole collect. With `bbox=`
the ground rectangle is turned back into the image window that covers it, only
that window is read from the product, and the output is cropped to the request —
the same pixels the whole-scene conversion would have produced there, for a
fraction of the memory, warp and disk. The download stays whole-product: a
slant-plane NITF has no map grid to range-read.

::: umbra_py.sicd_to_geocoded_cog

::: umbra_py.sicd_to_amplitude_geotiff

::: umbra_py.sicd_calibration_types
