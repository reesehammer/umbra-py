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

## Noise floor

A measured pixel is the ground's echo plus the receiver's own thermal noise, so
over a dark surface a calibrated value reports the sensor rather than the scene.
`noise_subtract=True` (`umbra convert --subtract-noise`) removes it in the power
domain, before anything scales it, and `noise_model=` says where the floor comes
from: `"measured"` reads the product's own `Radiometric.NoiseLevel` polynomial,
`"estimated"` infers one constant from the scene's own darkest pixels, and
`"estimated-range"` infers one per range line and fits it against range so the
inferred floor follows the swath. The inferred models need no metadata, which is
the point — most of Umbra's open products carry no `Radiometric` block at all, so
`"measured"` refuses on exactly the archive this library exists for. Ask
`sicd_noise_level()` which kind a file declares.

`compare_noise_models()` (`umbra convert --noise-check`) is how those inferences
get checked. On a product that *does* state an absolute floor there is a truth to
score against, so it runs the estimators over the product's pixels and differences
each result against the product's own `NoisePoly` — reporting the offset the
estimate reads low by and, once that offset is granted, how well it follows the
real floor across the image. It writes nothing and converts nothing.

::: umbra_py.sicd_noise_level

::: umbra_py.compare_noise_models

::: umbra_py.NoiseModelComparison

::: umbra_py.NoiseModelAgreement
