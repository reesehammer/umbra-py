# Coherent change detection (CCD)

The [change composite](change.md), [time-lapse](change.md) and
[timescan](../README.md#) products all compare how *bright* a scene is between
passes. **Coherent change detection** asks a sharper question: did the ground
itself get physically disturbed?

Two complex SAR images of the same scene, collected from the same geometry,
carry a near-identical speckle *phase* pattern — the random-looking interference
of all the scatterers in each pixel. That pattern is reproducible pass to pass
*unless something at the surface actually moved*. The normalised complex
cross-correlation of the two images, the **coherence** `|γ|` in `[0, 1]`,
measures exactly that:

- `|γ| → 1` — the surface is unchanged (high coherence).
- `|γ| → 0` — the surface decorrelated: a vehicle drove through, earth was
  turned, foliage or water moved — *or* the return was too weak to be coherent
  (radar shadow, smooth water, noise).

The payoff is that decorrelation exposes **sub-resolution** disturbance — tire
tracks, footpaths, freshly dug soil — that leaves *no* signature in amplitude at
all. It is the one SAR product a general-purpose GIS pipeline can't reproduce,
because it needs the preserved phase that only the complex `SICD` product
carries.

CCD reads the `SICD` with `sarpy` and renders with Pillow, so install both
extras:

```bash
pip install "umbra-py[convert,viz]"
```

> **Important — coregistration limitation.** Coherence forms only between two
> images on a *shared pixel grid*. The built-in coregistration is a single
> global sub-pixel **translation**, which suffices only for a coherent collect
> on near-identical geometry. Two *independently focused* Umbra SICDs of the
> same site are each formed on their own slant plane, so a translation cannot
> align them — every pixel decorrelates and the coherence map is just noise.
> The command **warns** when it detects a noise-floor result rather than letting
> it read as "everything changed". Making arbitrary repeat-pass pairs work needs
> full sensor-model coregistration (resampling onto a common grid via the SICD
> geometry and a DEM), which is not implemented. In practice this means CCD here
> is usable on a genuinely coherent pair already on a shared grid; the current
> Umbra open-data catalog does not appear to publish such pairs.

---

## 1. The one-liner

Point `umbra ccd` at two acquisitions of one site. Each argument is either a
local `SICD` (NITF) file or a STAC item JSON URL — given a URL, the command
downloads that item's `SICD` asset first (`sarpy` needs a local file; the
complex NITF can't be streamed like the GEC overviews):

```bash
umbra ccd <ref-stac-url> <sec-stac-url> --out ccd.png
```

The output is a grayscale coherence map: bright where the ground held still,
dark where it changed. Add a colormap, or flip the polarity so *change* is the
bright signal:

```bash
umbra ccd ref.nitf sec.nitf --out ccd.png --colormap magma --invert
```

## 2. From Python

```python
from umbra_py import coherent_change, save_ccd

# A float32 [0, 1] coherence map you can threshold or analyse:
coh = coherent_change("ref.nitf", "sec.nitf", window=5)
print((coh < 0.3).mean(), "fraction of pixels decorrelated")

# Or render straight to an image:
save_ccd("ref.nitf", "sec.nitf", "ccd.png", colormap="viridis")
```

If you already have the two complex arrays in memory (e.g. chips you read
yourself), call `coherence(reference, secondary)` directly — it does the
co-registration and the windowed coherence estimate and returns the `[0, 1]`
map.

## Large scenes: `--crop`

A port SICD is enormous — several **gigabytes** of complex data — and
`coherent_change` reads the whole image into memory by default, which won't fit
on a laptop. `--crop` processes just a sub-window, at full resolution:

```bash
# Print the scene size first (the command echoes "Reference scene: R x C px"),
# then a centered 4096x4096 box:
umbra ccd "$REF" "$SEC" --out berths.png --crop 4096 --dest ./sicd_cache

# Or target a specific area once you know its pixel coordinates
# (COL,ROW,WIDTH,HEIGHT):
umbra ccd "$REF" "$SEC" --out berths.png --crop 8000,3000,3000,2000 --invert
```

The *same* pixel window is read from both files via `sarpy`, so only that
block is loaded — memory and compute scale with the crop, not the scene. This
assumes the pair is co-framed (a same-geometry repeat pass), which is exactly
the pairing CCD needs anyway; the coregistration still removes the residual
sub-pixel shift. Note the **download** is still the whole NITF — `sarpy` needs
the local file — so `--crop` bounds memory, not bytes fetched. A cropped berth
is where ship decorrelation looks most striking.

From Python it's the `crop=` argument:

```python
from umbra_py import coherent_change
coh = coherent_change("ref.nitf", "sec.nitf", crop=4096)          # centered box
coh = coherent_change("ref.nitf", "sec.nitf", crop=(8000, 3000, 3000, 2000))
```

---

## How it works (and what to watch for)

- **Same site, same geometry.** CCD compares an Umbra **repeat-pass** of one
  site. The pair must view the scene from essentially the same collection
  geometry; two very different look angles decorrelate everywhere and the map
  goes uniformly dark.
- **Sub-pixel coregistration is mandatory.** Coherence collapses with even a
  fraction of a pixel of misregistration, so the images are aligned first by
  estimating a single global sub-pixel shift (phase cross-correlation) and
  applying it with an exact, phase-preserving Fourier shift. This runs by
  default; `--upsample` sets its precision (`1/N` of a pixel).
- **The window trades noise for detail.** Coherence is estimated over a
  `--window`×`--window` boxcar (default 5). A larger window suppresses speckle
  noise in the estimate but blurs small changes; a smaller one is sharper but
  noisier.
- **Slant plane, not geocoded.** The map is in the radar image plane, like
  [`sicd_to_amplitude_geotiff`](../README.md#) — fine for inspecting *what*
  changed; geocoding the result is out of scope for v1.
- **Low coherence isn't only change.** Shadow, smooth water and other weak
  returns are incoherent too. Read a dark region as "decorrelated here," then
  bring in the amplitude image to tell *disturbance* from *no signal*.

`--max-size` caps the written image's longer side (coherence is still estimated
at full resolution over the analysed area, then resized for display). Without
`--crop` the whole complex image of each `SICD` is read into memory, so cost
scales with scene size — use `--crop` on large (port-scale) scenes.
