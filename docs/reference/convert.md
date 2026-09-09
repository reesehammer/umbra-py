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

That refusal — and the matching one for `noise_model="measured"` on a product
that states no floor — is a
[`UnsupportedMeasurementError`](exceptions.md), the family of errors that are
facts about a *product* rather than about the request, and it is raised off the
metadata **before** any pixels are read. A scene that could never have been
calibrated therefore costs its header rather than a multi-gigabyte complex read
followed by an amplitude detection. A malformed request — an unknown
calibration name, an even filter window — stays a plain `ValueError`, because
the caller can fix that one.

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

## Speckle

Every correction above targets something the sensor added. Speckle is not one of
them: coherent illumination of a rough surface interferes with itself, so a
single-look pixel's power scatters about the surface's true backscatter with a
standard deviation equal to its mean. That is the dominant uncertainty in a
calibrated number, the reason a pixel-by-pixel difference between two passes is
mostly speckle, and it cannot be subtracted — averaging is the only correction.

`speckle_filter=` (`umbra convert --speckle-filter`) does the averaging in the
power domain, last in image space: `"boxcar"` averages the `speckle_window`
window unconditionally — the multilook, maximum variance reduction, blind to
edges — and `"lee"` averages only where the window is no more variable than
speckle alone would explain, keeping edges and points (Lee 1980). No filter is
the default because what it spends is resolution: a window that averages *N*
pixels reports ground *N* pixels across, and 25 cm is the reason to use this
archive.

So the raster records both what it did (`UMBRA_SPECKLE_FILTER`,
`UMBRA_SPECKLE_WINDOW` — both refused-on-mix by
[`to_stack`](load.md), since averaging one pass and not another shows up as
change) and what it achieved: the equivalent number of looks before and after
(`UMBRA_SPECKLE_ENL_BEFORE` / `_AFTER`). That pair is the honest answer to "how
much speckle did that remove?", and on a product sampled finer than it resolves
it lands below the window's pixel count — which is a fact about the product, not
a fault in the filter.

::: umbra_py.SpeckleFiltering

::: umbra_py.sicd_noise_level

::: umbra_py.compare_noise_models

::: umbra_py.NoiseModelComparison

::: umbra_py.NoiseModelAgreement

## Preflight: ask before downloading

`sicd_calibration_types()` and `sicd_noise_level()` answer "can this product
support that?" from a file you already have. The expensive half was getting the
file: a SICD's metadata lives inside the NITF, so learning that a pass cannot be
calibrated meant downloading the pass, and learning it about a site's twenty
passes meant downloading twenty.

`sicd_capabilities()` (`umbra preflight`) asks the same question over the wire. A
NITF states its own layout in a fixed-width file header, so the SICD XML — a data
extension segment near the end of the file — is located by arithmetic on the
segment table and fetched with two HTTP range requests: tens of kilobytes of a
multi-gigabyte product. It reports what the product declares (which calibrations,
which noise level, the scene's identity), what the answer cost, and the product
size it did not download.

The verdict is not a second opinion. `SicdCapabilities.refusal()` hands the
parsed metadata to the conversion's own support check, calling the same
coefficient readers, so a preflight that clears a product and a conversion that
then refuses it cannot disagree — only where the metadata came from differs.
`preflight_items()` asks it of a whole search result, recording an unreadable
acquisition as its own verdict rather than ending the walk, and reads several
products at once (`workers=`, default `DEFAULT_PREFLIGHT_WORKERS`) — because once
the answer costs kilobytes, the round trip is the only part left that scales with
the number of passes. The concurrency is a schedule and not an answer: the reads
are independent, the verdicts come back in the order they were asked in (the chip
run pairs them against its own selection positionally), and `progress` is called
from the calling thread in that same order.

The NITF walk and the XML parse are stdlib, so this needs no extra at all —
including no `[convert]`. Only confirming a *positive* answer on a product that
does carry the scale factors reads their coefficients, which needs `numpy`; every
refusal, which is what most of Umbra's open archive returns, is answerable from a
core install.

::: umbra_py.sicd_capabilities

::: umbra_py.preflight_items

::: umbra_py.SicdCapabilities

::: umbra_py.PreflightResult

::: umbra_py.PreflightReport

::: umbra_py.read_sicd_xml
