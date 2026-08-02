# umbra-py Strategy — Maximally Valuable to Umbra and the SAR Ecosystem

> **How this file fits with the rest of the repo.** This is the single home for
> the project's enduring *context*: why it exists, where it sits in the SAR
> ecosystem, the design principles it holds to, and the remaining critical
> path. It is deliberately **not** a status log.
>
> - **What has shipped** lives in [`CHANGELOG.md`](../CHANGELOG.md) (history,
>   newest first) — the authoritative record. Do not re-narrate shipped work
>   here.
> - **Fine-grained open follow-ons** live in [`TODO.md`](TODO.md) (the
>   per-PR ledger of items intentionally scoped out of merged PRs).
> - **This file** carries the durable "why" and the short list of genuinely
>   open workstreams (§8).
>
> The three companion planning docs — `CODEBASE_ANALYSIS.md`,
> `DEMO_APP_GAPS.md`, and `AI_INTEGRATION_IDEAS.md` — were analysis snapshots
> whose plans are now executed. They have been consolidated into this file and
> removed. Their historical item IDs (`C1`, `G6`, `P2 #11`, workstream `5.x`, …)
> still appear in commit messages and CHANGELOG entries; the detail behind each
> lives in git history and the CHANGELOG.

---

## 1. The thesis

Umbra's [Open Data Program](https://umbra.space/open-data/) is a marketing
funnel: its job is to turn curious analysts into
[Canopy](https://docs.canopy.umbra.space/) (commercial tasking/archive API)
customers. umbra-py becomes valuable to Umbra to the exact degree it:

1. **widens that funnel** — more people successfully using the open data;
2. **shortens the path from free data to paid tasking**; and
3. **does work Umbra would otherwise have to do themselves.**

For the wider ecosystem, the goal is simpler: make Umbra's 16–25 cm SAR feel
as approachable as Sentinel-1 or Landsat — searchable, previewable, and
analysis-ready from the scientific Python stack in a few lines.

These goals reinforce each other. The honest pitch to Umbra is not "no one
can do this without us"; it's *"everyone who does this without us writes the
same 500 lines of glue first, and many give up."*

## 2. The landscape: life without umbra-py

Every existing path to the open data is workable but not easy, for one
structural reason: **Umbra publishes a static STAC catalog with no search
API**, which breaks the standard tooling that makes other missions feel easy.

- **Official surfaces.** A public 40+ TB S3 bucket
  ([AWS Open Data registry](https://registry.opendata.aws/umbra-open-data/))
  listable with `aws s3 ls --no-sign-request`, and a hosted
  [STAC Browser](https://open-data.umbra.space/browse/) for clicking around.
  That's *browsing*, not *searching* — there is no "GEC scenes in this bbox
  for these dates" primitive. Canopy runs a real authenticated STAC API, but
  it serves the commercial archive, not the open data.
- **Generic STAC tooling falls flat.** The elegant answer elsewhere is
  `pystac-client` + `stackstac`/`odc-stac`, but that stack assumes a STAC
  *API*. Against a static catalog you're reduced to crawling thousands of
  nested `catalog.json` files with plain `pystac` and filtering client-side.
  The QGIS STAC plugin and leafmap search hit the same wall. (This is the wall
  `umbra serve`'s read-only STAC API façade removes — see §5.2.) The *load*
  half of that stack fails for a second, independent reason: `stackstac` /
  `odc-stac` want a common projected grid, and successive Umbra passes over one
  site arrive in whatever UTM zone and extent each acquisition used. That is
  what `to_stack` / `umbra stack` do themselves — see §5.5.
- **Google Earth Engine.** The
  [community catalog](https://gee-community-catalog.org/projects/umbra_opendata/)
  mirrors GEC products as an ImageCollection — genuinely elegant if you live
  in GEE, but GEC-only, community-maintained, and platform-locked away from
  xarray / rasterio / PyTorch.
- **The DIY route.** The best-documented workflow is
  [Mark Litwintschik's blog series](https://tech.marksblogg.com/umbra-open-data-free-satellite-imagery.html)
  (`aws s3 sync` + jq + DuckDB + GDAL + sarpy) — strong evidence the gap is
  real: the state of the art is a multi-page tutorial, not a `pip install`.
- **Scattered pieces.** [sarpy](https://github.com/ngageoint/sarpy)
  (SICD/CPHD, low-level), [MultiRTC](https://github.com/MultiSAR/MultiRTC)
  (RTC processing), one-off downloader scripts. No cohesive toolkit; EODAG
  has no Umbra provider.

## 3. Novelty, honestly assessed

The individual techniques here are standard — STAC crawling, COG range
reads, SQLite indexing, xarray loading. The *packaging* is the novelty:
nothing else goes search → footprint map → quicklook → analysis-ready array
in a few lines against Umbra's catalog.

Two consequences to keep in mind:

- **Discovery is the moat; loading is convenience.** GEC products are
  cloud-optimized GeoTIFFs, so once someone has a URL, plain
  rasterio/rioxarray/QGIS can stream them. The part with no substitute is
  search over a catalog that has no search API (`UmbraCatalog`,
  `CatalogIndex`, the published geoparquet snapshot, and now the `umbra serve`
  STAC API).
- **The moat is leased, not owned.** Umbra could publish a stac-geoparquet
  index or a public STAC API tomorrow, obsoleting the crawler layer. That's
  fine — it would be a *win* for the mission, and the viz / quicklook /
  xarray / workflow layers survive and get better. Design so that outcome is
  graceful (see workstream 5.2's "offer it upstream").

## 4. Why Umbra should care (and the risks)

- The Open Data Program exists for adoption ("experiment with SAR's
  capabilities", CC BY 4.0, no sign-up), and its best-documented complaint
  is exactly the friction this library removes.
- Umbra's own engineering targets authenticated commercial customers
  (Canopy), so an open-data toolkit doesn't compete with anything they sell
  — it widens the funnel toward it. Precedent: Capella ships an official
  `capella-console-client`; Umbra has no equivalent.
- The AWS registry entry has a "Tools & Applications" section with very
  little in it; community tooling is the kind of thing companies link from
  their docs.

**Risks:** (1) upstream obsolescence of the crawler layer (acceptable, see
§3); (2) the name — `umbra-py` trades on their trademark, and an unrelated
[`Umbra` package](https://pypi.org/project/Umbra/) already exists on PyPI.
Raise the naming question with Umbra proactively; the existing "not
affiliated" disclaimer plus asking first makes the project easy to say yes to.

## 5. Workstreams, ranked by leverage

Status here is a one-line marker, not a log — the shipped detail is in the
CHANGELOG, the open follow-ons in `TODO.md`. Workstream numbers (`5.1`…`5.6`)
are stable identifiers cited from source docstrings; keep them.

### 5.1 Canopy backend behind the same `search()` interface — **shipped**

Pass a Canopy token (`UmbraCatalog(token=…)`, `umbra search --token …`, or
`$UMBRA_CANOPY_TOKEN`) and the *same* `search()` interface queries the
commercial archive's real STAC API instead of the open bucket — the funnel
made literal. Reachable across the whole CLI (`map`/`gallery`/`change`/…),
plus a keyed `get_item` lookup and the MCP server. **Open:** push
`product_types` down as a STAC filter extension once the exact Canopy field
names are confirmed, and verify request/response shapes against the live API
(needs a real token — see `TODO.md`).

### 5.2 Continuously rebuilt, published catalog index — **shipped**

`export_geoparquet()` / `umbra index export` write a
[stac-geoparquet](https://stac-geoparquet.org/) snapshot; the weekly
`publish-index.yml` workflow rebuilds the full index and publishes
`catalog.db`, `umbra-open-data.parquet`, a whole-catalog `catalog.pmtiles`
basemap, and (opt-in) a `catalog.embed.db` similarity sidecar on the rolling
`catalog-index` release; the consume side (`umbra index fetch`,
`umbra tiles --fetch`, `umbra embed fetch`) pulls them so a fresh install gets
instant whole-catalog `--local` search, a zoomable basemap, and visual
similarity search with no crawl. **Open (maintainer):** *offer it upstream* —
"host the parquet, the `.pmtiles` basemap, and the similarity vectors next to
`catalog.json` and the whole ecosystem gets a search API, a whole-catalog map,
and scene-similarity search for free." If Umbra adopts it, this project is
part of their data program's infrastructure.

### 5.3 Make adoption visible where Umbra looks — **partial**

`CITATION.cff`, `SECURITY.md`, and a Contributor Covenant `CODE_OF_CONDUCT.md`
ship, completing GitHub's community profile. **Open (mostly maintainer
actions):** a PR to
[awslabs/open-data-registry](https://github.com/awslabs/open-data-registry/blob/main/datasets/umbra-open-data.yaml)
adding umbra-py under the Umbra entry's "Tools & Applications"; a listing on
the [STAC Index](https://stacindex.org/) ecosystem page; registering
`umbra-mcp` in the MCP registries and Anthropic's directory; and minting the
Zenodo DOI on the first release.

### 5.4 Demo notebooks that create SAR converts — **shipped**

The full `examples/` notebook gallery (`01`–`08`: hello → download/open GEC →
change detection → amplitude time series → detection chips → site monitoring →
SICD amplitude → time-series datacube) exists and doubles as a live eval — each
notebook is self-checking and guarded offline by `tests/test_examples.py`. `08`
is the on-ramp for the stacking chain of workstream 5.5: `to_stack` onto a
shared equal-area grid, then `stack_stats` (net change, `blocks`, `block_series`)
reducing the cube to a measured answer.

### 5.5 Close the format gaps that generate support burden — **partial**

ML dataset prep (`umbra chips`), time-series datacubes (`umbra_py.to_stack` /
`stack_to_geotiff` / `umbra stack` — the co-registered `(time, y, x)` xarray
cube or multi-band GeoTIFF that `stackstac`/`odc-stac` can't produce here, see
§2 — reduced to a JSON answer by `stack_stats` / `umbra stack --stats` / the
`stack_stats` agent tool / `POST /artifacts/stats` on `umbra serve` / the
`umbra demo` explorer's **Quantify** button, and to a
*located* answer by its `blocks=N` spatial breakdown, which says which part of a
site moved and between which two passes — and, with `block_series=True`, each
block's whole pass-to-pass sequence rather than only the interval it moved most
in, which is what tells a steady drift apart from a single step, now *drawn* as
sparklines of the site's and its peak block's history in the explorer's Quantify
readout — and, since the cube used to cost `max_size²` × the number of passes in
memory, `to_stack(lazy=True)` / `umbra stack --lazy` now defer each pass into one
`dask` chunk and have both consumers, `stack_to_geotiff` and `stack_stats`, walk
the series a slice at a time, and `chunk_size=N` / `--chunk-size N` takes the
same step *within* a pass — windows read and written independently — so neither
the length of the series nor the size of one scene sets how much archive can be
stacked sharp)
and SICD → geocoded COG (`umbra convert`, including DEM/`--dem auto`
orthorectification, geoid handling, and four RTC flattening models:
`cosine`/`area`/`gamma`/`facet`) all ship. The fourth is the image-space
illuminated-area facet integration (Small 2011): it projects every terrain facet
into the scene's own `(slant_range, azimuth)` geometry, accumulates the
illuminated area landing in each radar cell, and normalises by that total — so
it is the only one of the four that measures **layover**, where several ground
facets image into one cell and their returns sum. ~~**Open:** calibration
itself~~ **shipped** — `umbra convert --calibrate {sigma0,beta0,gamma0,rcs}` /
`sicd_to_geocoded_cog(calibration=…)` scales pixel power by the SICD's own
`Radiometric` scale-factor polynomial, in image space where those polynomials
are defined, so the output is a physical backscatter coefficient rather than
relative amplitude. It composes with the flattening (both are power-domain
factors), which makes `--rtc-model facet --calibrate gamma0` a terrain-flattened
**gamma-nought** product. The caveat did not disappear so much as become
*detected*: Umbra's open products generally carry no `Radiometric` block, and
asking for a calibration one cannot support is a self-describing error naming
what it does carry (`sicd_calibration_types` answers the same question ahead of
time) rather than a calibrated-looking number. And a calibrated product now
*says so*: every raster `umbra convert` writes carries `UMBRA_*` GeoTIFF
metadata naming the calibration, the RTC model and its resolved reference angle,
the DEM/geoid, the projection, the scale and the CC-BY licence
(`read_conversion_tags` / `umbra convert --provenance` / `gdalinfo`), because a
physical measurement nobody can attribute to a calibration is not one.
~~**Open:** calibration scales the ground's echo and the receiver's own thermal
noise alike, so over a dark surface it reports the sensor rather than the
scene.~~ **shipped** — `umbra convert --subtract-noise` /
`sicd_to_geocoded_cog(noise_subtract=True)` takes the product's own
`Radiometric.NoiseLevel` floor off the detected power *first*, because noise adds
where the flattening and the calibration multiply, and because over calm water,
radar shadow or dry sand that floor is most of the value — and, varying across
the swath as it does, put a gradient in the answer that tracked the geometry
rather than the ground. Only an `ABSOLUTE` noise level can be subtracted: a
`RELATIVE` one describes how the floor varies without saying what it *is*, so it
is a self-describing refusal rather than an invented offset (`sicd_noise_level`
answers ahead of time, as `sicd_calibration_types` does for the scale factors).
It is recorded (`UMBRA_NOISE_SUBTRACTION`) and *consumed*: `to_stack` refuses a
series that subtracted the floor from some passes and not others, because over a
dark cell that mix is the difference between the two passes.
~~**Open:** that floor is read from metadata Umbra's open products do not carry,
so the correction refused on exactly the archive it was built for.~~ **shipped** —
`umbra convert --noise-model estimated` /
`sicd_to_geocoded_cog(noise_model="estimated")` infers the floor from the scene
instead of reading it: a SAR image's darkest surfaces return essentially nothing,
so the low tail of its own power distribution *is* the receiver, and its 5th
percentile is a robust read of it. It needs no metadata, which is the point. What
it costs is named rather than smoothed over — one scalar cannot follow the swath
the way a `NoisePoly` does, and a scene that is bright everywhere has no dark
ground to read — and so the inference is never allowed to wear a measurement's
clothes: `UMBRA_NOISE_SUBTRACTION` records `"estimated"` rather than `"absolute"`
(with the level it inferred in a new `UMBRA_NOISE_FLOOR_DB`), which is precisely
what makes `to_stack` refuse to difference an inferred floor against a measured
one, `stack_stats` name the estimate's two limits, and a `--work-dir` chip cache
keep the two products apart. Those limits are now *measured* on each scene rather
than only named: `UMBRA_NOISE_FLOORED_FRACTION` says how much of the image the
floor drove to the sensor's limit, and `UMBRA_NOISE_FLOOR_MARGIN_DB` how far the
scene's median sat above an inferred floor — the estimator's own assumption
turned into a number, since the model works exactly to the degree that a scene's
dark surfaces are a different population from its backscatter.
~~**Open:** the estimate is one constant, so it cannot follow the swath — which
is the artefact the subtraction exists to remove, put back by the model that made
it usable on the open archive.~~ **shipped** — `umbra convert --noise-model
estimated-range` takes the same low-tail read *per range line* (SICD stores range
along the image rows) and **fits** those floors against range, so an inferred
floor follows the swath the way the measured one does with no metadata at all.
The fit is what makes a per-line read survive a real scene: it interpolates over
the lines that had no dark ground, and drops the lines whose tail sits more than
3 dB *above* it — a one-sided trim, because bright ground can only push a line's
low tail up. The claim is narrow and stated as such: what it adds is the *shape*,
since a percentile of speckled noise sits conservatively below that population's
mean by nearly the same decibel offset on every line, so the bias lowers the
curve without bending it — and it is the gradient, not the offset, that a scalar
floor leaves in a scene. It is a *third* provenance value, not a better
`"estimated"`, so `to_stack` refuses to difference a fitted profile against a
constant guess, `stack_stats` says which limit went away and which did not, and
`UMBRA_NOISE_FLOOR_SPREAD_DB` reports the swing found — the number that says
whether the constant model was missing anything at all.
~~**Open:** every one of those inferred models was justified by an argument the
open archive could not check, since a product that states no floor states no
truth either.~~ **shipped** — `compare_noise_models` / `umbra convert
--noise-check` scores an inferred floor against a product's own `NoisePoly` where
one exists, splitting the error into the offset the estimate reads low by and how
well it follows the real floor once that offset is granted; against a synthetic
10 dB ramp the fitted profile scores 0.2 dB of shape error where the constant
estimate scores the ramp's full deviation, 2.9 dB. Which is the difference
between a model that is argued and one that is measured.
~~**Open:** every correction above targets something the *sensor* added, and what
is left after all of them is larger than any of them — speckle, whose standard
deviation equals its mean on a single look, so the pipeline's calibrated numbers
carried an undocumented dominant uncertainty and nothing averaged it down.~~
**shipped** — `umbra convert --speckle-filter {boxcar,lee}` averages in the power
domain, last in image space: `boxcar` is the multilook (most variance removed for
a window, blind to the edge it averages across) and `lee` averages only where a
window is no more variable than speckle alone would explain, so edges and points
survive — with its speckle parameter read off the scene and clamped at single-look,
since no product has fewer looks than one and believing a lower read is licence to
smooth structure away. It is opt-in because what it spends is the reason to use
this archive: a window that averages N pixels reports ground N pixels across. So
both sides of that trade are recorded — `UMBRA_SPECKLE_FILTER` /
`UMBRA_SPECKLE_WINDOW` join `MEASUREMENT_PROVENANCE_KEYS`, so `to_stack` refuses
to difference a filtered pass against an unfiltered one (the smoothing would read
as change, strongest where the ground has the most structure) and `stack_stats`
states both halves — and what the filter *achieved* is measured rather than
claimed: `UMBRA_SPECKLE_ENL_BEFORE` / `_AFTER`, the equivalent number of looks
either side, which on a product sampled finer than it resolves lands **below** the
pixels averaged. That estimator is itself calibrated against synthetic single-look
imagery (1.0 unfiltered, within 10 % of N² after an N-pixel boxcar), and structure
biases it down, so it is a floor on the looks present rather than a claim about
them. `umbra chips --speckle-filter` carries it to a training set, where every
record says which window it was cut with — a chip's resolution, as opposed to its
pixel size, being what decides what a model can learn to see.
~~**Open:** that averaging happens in the radar's own image space, so it reaches
the complex archive and nothing else — and the products this library is mostly
used with, Umbra's *published* GEC rasters, arrive already geocoded and never go
through the pipeline at all, so the archive it exists for could not average
speckle by any route.~~ **shipped** — `umbra stack --speckle-filter {boxcar,lee}`
/ `to_stack(speckle_filter=…)` runs the same two filters, the same arithmetic and
the same power domain one step down the chain: on each pass of a datacube, on the
shared grid, which is the first point a source exists on the cube's own
coordinates. That placement is the claim rather than a convenience — the window
averages the cells the cube *reports*, so the resolution it spends is the
resolution a `stack_stats` number quotes. It records itself in `umbra convert`'s
own two keys rather than a second vocabulary, because a cube whose cells were
averaged over an N-cell window *is* an N-window-filtered raster: which means the
caveat, the written GeoTIFF's tags and the refusal to difference a filtered pass
against an unfiltered one all apply with no new machinery. Filtering a series
that already carries a filter is refused rather than composed (two averagings
leave a resolution neither window names), while the combination that once could
not be made exact — a filter window straddling the independently-read windows
`chunk_size` cuts, with `lee`'s scene-read looks parameter differing per window —
is now made exact rather than refused (a halo per window, the looks read once per
pass; see below). And the filter now reaches the surfaces that
*answer* rather than compute: `POST /artifacts/stats` takes `"speckle_filter"` as
a request field (in the artifact cache key, since it moves the numbers — the rule
`"windowed"` established) and the `stack_stats` agent tools take the same pair, so
a hosted or model-driven measurement stops being the noisiest one the chain can
produce. It turned out to be `"windowed"`'s exact complement at the time —
filtering needed a pass whole, windowed measurement needed it chunked — so each
was a `400` on the instance the other needs, and the two were refused together at
the request.
~~**Open:** which of the two a given instance honours was discoverable only by
sending a request and reading the `400`.~~ **shipped** — the landing page's
`stats` link carries `umbra:options`, where each option reports whether this
server supports it and an unsupported one carries the reason its refusal would
have given, so a client that installed nothing locally picks the option that
works before spending a request. Advertisement and refusal are one function, and
the suite drives the renderer against the page rather than trusting the pair to
stay in step.
~~**Open:** advertising the choice is not the same as not having to make it — the
sharpest cube the chain could build was still the noisiest one it could measure,
and a hosted client that wanted a filtered measurement of a large series had
nowhere to send it.~~ **shipped** — `to_stack(speckle_filter=…, chunk_size=N)`
composes, so the exclusivity is gone rather than better documented. The two
obstacles are answered the way `umbra chips` answered them tile by tile, which is
what the old refusal named as what it would take: each window is read with a
half-window **halo** and cropped after filtering, so a filtered chunked cube is
the whole-pass filter's own answer (to a `float32` ulp — a summed-area table adds
in a different order over 12 cells than over a pass) rather than one carrying a
seam `stack_stats` would report as change; and `lee`'s speckle parameter, being a
property of the product's processing rather than of the cells one window covers,
is resolved **once per pass** as a single deferred task every window depends on,
from a fixed sample of it, since a chunked build is by definition the case where
the pass does not fit. `boxcar` needs no such parameter, so a chunked `boxcar`
cube is cell-for-cell the unchunked one at no extra read. The payoff is one
request: `POST /artifacts/stats` takes `"windowed"` **and** `"speckle_filter"`
together on a chunked instance, so a hosted measurement of a series too large to
hold is also the one with the interference averaged out of it — and the refusal
is gone from all three places it was stated (`to_stack`, the request-level pair
check, and the per-instance one the landing page advertises, which now reports
`supported: true` everywhere).
~~**Open:** it reached every surface that renders a picture or returns a number,
and stopped at the one whose output is a *dataset* — `umbra chips` took
`--speckle-filter` only on the complex path, so a training set cut from the
published GEC rasters could not be smoothed at all.~~ **shipped** — `umbra chips
--speckle-filter` now applies to any asset, running where it is most correct for
each: on `GEC`/`CSI` the **tiles** are averaged, on `SICD` the request is routed
into the conversion and the scene is filtered in image space before geocoding.
This is the place the correction matters most, because unlike a caveat on a
measurement nothing downstream can put back what a model was trained without.
The two things that made a tile loop the hard place to put it are answered rather
than approximated — the exact pair `to_stack`'s `chunk_size` refusal named. A
window straddling a tile boundary: every tile is read with a half-window **halo**
and cropped after filtering, so a filtered tile is *bit-for-bit* the same as that
region of the scene filtered whole, which is what makes two overlapping tiles
agree about shared ground rather than carry a seam a model would learn. And
`lee`'s speckle parameter: it is read **once per acquisition** from a fixed grid
of sample windows, pooled at the block level before the percentile, since it is a
property of the product's processing — per tile it would smooth one over water
differently from the one beside it over a city. What the window spent and what it
bought both reach the manifest: `speckle_enl_before` / `_after` / `_looks` join
`speckle_filter` / `speckle_window` in every record on **either** path, and
`ChipDataset.speckle` rolls them up per acquisition the way the noise summary
does. **This closes the speckle group.**
And the conversion pipeline now *feeds the ML on-ramp*: `umbra chips --asset
SICD` geocodes each complex product through `sicd_to_geocoded_cog` and cuts the
identical tiles from the result — and, with `--clip-bbox`, geocodes only the area
of interest rather than the whole collect — so a training set can come from the
full-resolution archive — and, with `--rtc-model facet --calibrate gamma0`, from
a terrain-flattened gamma-nought coefficient rather than relative brightness,
which is the difference between a model that transfers between scenes and one
that memorises brightness. The chipper composes with the pipeline rather than
reimplementing it (same window loop, conversion settings passed straight
through, provenance read back from the raster's own `UMBRA_*` tags into both the
manifest and every chip GeoTIFF — including the noise estimate's two per-scene
diagnostics, which a batch reports as a count of the scenes that had too little
dark ground to read rather than as a line each), and states its cost: a SICD has no map grid to
range-read, so the product is downloaded whole — opt-in, one scene resident at a
time, and `--work-dir` making the expensive step resumable.
~~**Open:** the conversion is all-or-nothing, so a run whose subject is a *site*
converts every scene whole to keep a fraction of each.~~ **shipped** — `umbra
convert --clip-bbox` / `sicd_to_geocoded_cog(bbox=…)` turns the ground rectangle
back into the image window that covers it, reads only that window from the
product, sizes every downstream step to it (control points projected at the scene
coordinates the window occupies but labelled with the array's own rows and
columns, the radiometric scale-factor polynomials evaluated at those same image
coordinates, `--dem auto` fetching the window's tiles rather than the scene's) and
crops the geocoded output to the request. The pixel size still comes from the
whole input, so a clip chooses *which* ground is written rather than how finely it
is sampled — the pixels are the ones the whole-scene conversion would have put
there. `umbra chips --clip-bbox` is the same decision one level up: it tiles only
that window for every asset, and on `--asset SICD` it is the conversion's clip
too, so the expensive step costs what the site costs rather than what the collect
does.
~~**Open:** the two corrections that depend on a product describing itself
(`--calibrate`, `--noise-model measured`) refused as a bare `ValueError` raised
after the scene had been read, so a chip batch over a mixed archive lost every
pass already cut to the first product whose metadata came up short.~~
**shipped** — `UnsupportedMeasurementError` names that family (an `UmbraError`
*and* a `ValueError`, so nothing that caught one before changed), which does two
things a nameless refusal could not: `umbra chips --skip-unsupported` catches
exactly it, records the pass on `ChipDataset.skipped` and carries on, so the
dataset **states** its hole rather than having one — while a download failure or
a corrupt product still ends the run, because a batch that swallows unknown
errors is one nobody can trust; and the refusal now reaches the `--json` error
envelope with a stable name and an actionable hint, which is what an agent
branches on. It is also asked *before* the read now, off the metadata, the way
the speckle window already was: a scene that could never have answered costs its
header rather than a multi-gigabyte complex read.
~~**Open:** that made the refusal cheap per scene but not cheap to *discover* — a
SICD's metadata lives inside the NITF, so a batch still learned which of twenty
passes could be calibrated by downloading twenty passes.~~ **shipped** — `umbra
preflight` / `sicd_capabilities` read the metadata over the wire: a NITF states
its own layout in a fixed-width file header, so the SICD XML data extension
segment is located by arithmetic on ~30 bytes of segment table and fetched with
two HTTP range requests — tens of kilobytes of a multi-gigabyte product, and the
command reports both numbers, because the download it did not do is the whole
claim. What makes it a preflight rather than a lookalike is that the verdict is
the conversion's own: the parsed XML is presented through an attribute view
shaped like a `sarpy` SICD (a polynomial's exponent-addressed coefficients
densified into the grid the conversion reads), so
`_check_measurement_support` — the same function, calling the same coefficient
readers — produces the answer, and a preflight that says yes cannot be
contradicted by the conversion it cleared. The whole path is stdlib plus the
already-core `requests`, so "can this archive answer my question?" is answerable
from a core install, and every way a product can fail to answer (not a NITF,
NITF 2.0's different header layout, a truncated field, no XML segment) is
refused by name rather than misread.
~~**Open:** it was a command you ran *beside* a chip run rather than part of one,
so the batch it exists to save still discovered a refusal by downloading the
product that would have refused.~~ **shipped** — `umbra chips --preflight` reads
every pass's metadata over the wire first and drops the ones that cannot answer
before a single product is fetched, asking the run's *own* conversion settings so
the question is the conversion's by construction. The design point is that a
cheaply-found hole is still a hole: a dropped pass is the same
`SkippedAcquisition` a survived refusal produces, distinguished only by a `stage`
field, and `ChipDataset.preflight` reports what asking cost against the download
it removed. An acquisition whose metadata cannot be *read* is kept rather than
dropped — a failed read is not a product declaring it cannot answer.
~~**Open:** all of that machinery covered two of the three corrections that
depend on a product describing itself — `--rtc` reads the collection geometry out
of the same file and refused with a bare `ValueError`, so a batch could not skip
it, an agent could not branch on it, and no preflight could see it coming.~~
**shipped** — `_scene_look_geometry` raises `UnsupportedMeasurementError`, which
is not a new mechanism but the *existing* one becoming applicable: the skip, the
JSON envelope and the hint all work unchanged the moment the refusal has the
right name. The question also moved twice — off the metadata rather than after
the warp (`_check_measurement_support(rtc=True)`), and then over the wire
(`umbra preflight --rtc`, `SicdCapabilities.look_geometry`, and
`umbra chips --preflight` asking it from the run's own settings) — which matters
most for exactly this correction, since flattening is the one whose refusal
otherwise arrives after a complex read, a DEM fetch *and* a reprojection. Most
products state the geometry, which is why it was worth asking rather than
assuming: the absent case is the one nobody plans for.
~~**Open:** every one of those reports — the summary, the `--json` payload, the
console lines — is a report to somebody *watching the run*, and the thing a chip
run produces is a directory somebody opens later. `manifest.jsonl` described the
tiles that existed and nothing in the directory said which ones were meant
to.~~ **shipped** — a run that could not include every acquisition it was
offered writes a `skipped.jsonl` sidecar beside the manifest, one line per
left-out pass with the product's own words, its `hint` and the `stage` the
refusal was found at, so a dataset that dropped nine of twenty-two passes stops
being indistinguishable on disk from one that was only ever offered thirteen —
which is the difference between a gap in a time series being the archive's or
the pipeline's. A sidecar rather than manifest rows (a skipped acquisition has
no chip, so it would be a record with no path, bbox or transform in a
one-row-per-chip schema), always `.jsonl` whatever the manifest's format, and
written only when there is something to record — so a clean run leaves exactly
the files it left before and the file's presence is itself the statement.
**This closes the batch-survivability group**: a refusal is named, survivable,
askable ahead of the download, read several at a time, and now *recorded in the
artifact* rather than only in the run.
**Open:** MultiRTC interop, which stays deferred.

### 5.6 Then actually talk to Umbra — **not started** (maintainer/relationship)

Sequenced after 5.2–5.3 so the pitch is concrete, not a favor: "unofficial
toolkit, N downloads/month, here's a hosted search index you can adopt,
here's the notebook gallery — link us from the open data page, and tell us if
the `umbra-py` name is a problem." Good outcomes, any of which locks in the
niche: a docs link, a registry listing, co-marketing, or upstreaming the index.

## 6. Guardrails

- **Don't** build a hosted service on Umbra's data or brand without talking
  to them first.
- **Keep the crawl polite:** scheduled (weekly), rate-limited, incremental.
  The fastest way to become *negatively* valuable is to be the reason their
  S3 bill spikes.
- **Don't position against Canopy.** This is the on-ramp to their
  commercial product, not a competitor to it.

## 7. Design principles to hold onto

These are the durable rules the AI-integration and demo work were built on;
they apply to every future change (consolidated from the former
AI-integration plan's design-principles section).

1. **Deterministic core, AI at the edges.** Models plan, describe, and
   narrate; the library searches, downloads, and renders. Never let a model
   output become a coordinate, a URL, or a filter without passing through the
   deterministic layer. (This is the `§A4`/`§6.1` determinism boundary cited
   from `planner.py`, `describe.py`, `narrate.py`, and `mcp_server.py`.)
2. **Images are the API.** The library's superpower for AI is that its
   outputs are pictures with provenance. Prefer returning renderable artifacts
   (MCP image blocks, PNGs with JSON sidecars) over prose.
3. **Context is a product surface.** `llms.txt`, context cards, tool
   descriptions, and packaged prompts deserve the same review bar as code —
   they are what the agent "reads" instead of the README.
4. **License propagation is non-negotiable.** CC-BY attribution must survive
   every AI transformation, including model-generated text about the data.
5. **Agents are users; users are agents.** Every improvement for one (JSON
   errors, stable schemas, runnable examples, resumable operations) compounds
   for the other. Build once, serve both.

## 8. Current status & remaining critical path

The three original planning docs are essentially executed — the S3 pagination
fix (PR #29), the prebuilt/published index, the MCP server, the `umbra serve`
STAC API, natural-language search (`umbra ask`), the `umbra demo` self-serve
explorer, `umbra tiles` PMTiles, `umbra describe`/`watch`/`chips`/`embed`, and
the `umbra convert` SICD/DEM/RTC pipeline have all shipped (see the CHANGELOG).
What remains, grouped by the kind of work rather than by the old doc it came
from:

**Structural code debt (schedule, don't rush)**

- Extract the shared search-vs-URLs gathering + common Click option groups out
  of the CLI commands that still duplicate them (was the P3 #18 codebase-analysis
  item).
  **Partly shipped:** the gathering half (`_gather_items` / `_search_source`) and
  now the *geography* option group — `--bbox` / `--place` / `--intersects` are one
  shared pair of decorators plus one shared resolver across all fourteen gather
  commands. That extraction was worth doing because of what the duplication had
  cost: the options had drifted, so the polygon filter the library, the index and
  the STAC API all support reached exactly one command (`umbra search`) and
  `--place` was missing from three more. Every front door now takes an area of
  interest as a shape rather than a bounding rectangle — including the
  natural-language one: `umbra ask --aoi` lets the planner *select* one of the
  polygons the user supplied, by name, rather than authoring coordinates a
  hallucination could silently move (the §7.1 boundary applied to geometry).
  ~~**Open:** the date / task-name / limit options are still per-command (their
  help text is genuinely command-specific), and `umbra map` still lacks
  `--area`.~~ The **task-name group is shipped too**, and with it the gap it had
  left: `umbra map` now takes `--area` / `--fuzzy` like every sibling, so the one
  verb whose job is showing where the archive has imagery no longer makes you
  find a site's bounding box first (`umbra index build` / `update` gained the
  matching `--fuzzy`). `--area` is one shared `_area_option` beside
  `_fuzzy_option`, and — the part that outlasts this PR — **both** shared groups
  are now checked against one roster of gather commands
  (`conftest.GATHER_COMMANDS`): `tests/test_cli_option_groups.py` asserts every
  command on it exposes the group *and* forwards it to the search backend, so the
  drift that cost the polygon filter thirteen commands, `--place` three and
  `--area` one now fails a test rather than shipping. See the CHANGELOG.
  **Open:** the date / limit options are still per-command, and deliberately so —
  unlike geography and task name, `--limit`'s *default* is command-specific
  (20 / 24 / 100 / 500 / 2000) as well as its wording, so a shared decorator would
  have to override both and would buy nothing but indirection. Extract them only
  if that stops being true — see `TODO.md`.
- ~~Split `viz.py` into a `viz/` package (geojson / maps / raster / composites /
  gallery) with re-exports preserved (was P3 #19).~~ **shipped** — the
  2 023-line module is now `geojson.py` / `raster.py` / `composites.py` /
  `contact_sheet.py` (the gallery, renamed so the submodule is not shadowed by
  the `gallery` function beside it) / `maps.py`, plus `_deps.py` for the single
  `viz`-extra gate. `viz/__init__.py` re-exports every name the module had —
  public functions *and* the private helpers other package modules import from
  `umbra_py.viz` — and every definition is AST-identical to its pre-split form
  bar six relative-import levels, so no caller changed. The one behavioural
  difference is the test seam: an internal helper is now patched on the module
  that *calls* it. See the CHANGELOG.
- ~~Split `cli.py` — the outlier the `viz` split left behind — the same way.~~
  **shipped** — the 5 522-line module is now a `cli/` package of nine modules
  grouped by what the verb does: `_root.py` (the Click group, the JSON error
  envelope, `main()`), `_shared.py` (the option groups and how a command obtains
  its items), then `discover` / `scenes` / `process` / `composites` / `atlas` /
  `explore` / `indexes`. The claim is checked rather than asserted: the whole
  `--help` surface (group, 28 commands, 3 sub-groups, every option and help
  string) is byte-identical to the pre-split output, and all 74 definitions are
  AST-identical bar three mechanical rewrites — import depth, the shared helpers
  qualified as `_shared.<name>` (one patch target, which is what lets the
  option-group parity suite keep iterating over all fourteen gather commands),
  and thirteen copies of one item-fetch expression collapsed into
  `_shared._item_from_url`. See the CHANGELOG. **With this the structural-debt
  group is closed bar the conditional R\*Tree upgrade below**; the date/limit
  option groups stay deliberately per-command (see above).
- ~~Wire `pytest --cov` + a Codecov badge into CI (was P2 #16).~~ **shipped** —
  the `test-all-extras` job (the one job where every module actually runs)
  measures branch coverage behind a `--cov-fail-under` floor and uploads to
  Codecov (non-blocking); README carries the CI + coverage badges. See the
  CHANGELOG.
- SQLite R\*Tree upgrade *iff* the index grows to hundreds of thousands of
  items (the schema-version marker already makes this a migration, not a break).

**Demo / hosting polish (was the demo-gap analysis's G7 + Path A polish)**

- Packaging/hosting: ~~a Dockerfile + compose for one-command self-hosting of
  `umbra serve`~~ **shipped** (`Dockerfile` + `docker-compose.yml` +
  `docker-entrypoint.sh`, a first-boot index fetch, a `/healthz` probe, and a
  `docker.yml` CI smoke test — see the CHANGELOG). ~~A **GitHub Pages
  deployment of the static `umbra demo` / `catalog.pmtiles` showcase**~~
  **shipped** (`umbra showcase` composes the whole-catalog map + interactive
  explorer + a landing page into a static `site/showcase/`, and the `docs.yml`
  Pages job publishes it beside the docs — non-blocking and main-only; see the
  CHANGELOG).
- ~~Precompute showcase change artifacts for ~6–10 curated sites (R4 for the
  static path).~~ **shipped** — `umbra showcase --featured N` renders a change
  composite for the most repeat-imaged sites (or the ones `--featured-area`
  names) into `featured/` and puts them on the landing page as a captioned
  gallery, and the `docs.yml` Pages job passes `--featured 6`; see the CHANGELOG.
  ~~Still open under R4: the *swipe* and *timescan* variants of the same idea.~~
  **shipped too** — `umbra showcase --featured-view {change,timescan,swipe}`
  renders the same marquee selection as a whole-series *timescan* composite
  (mean/peak/variability as RGB, 3+ passes) or as a self-contained before/after
  *swipe* page, which the gallery shows as a link card since an interactive page
  has no still to preview. **R4 is closed.**
- ~~Let the interactive explorer scale to the whole catalog (Path A's last
  structural cap) and collapse the showcase's map/explorer pair into one page.~~
  **shipped** — `umbra demo --pmtiles` draws every acquisition from a
  whole-catalog `.pmtiles` archive read by range request, with the sidebar filters
  compiled to MapLibre expressions evaluated inside the tiles, and `umbra showcase
  --unified` (what `docs.yml` now deploys) builds the showcase as a single
  explorer over that archive instead of a click-only map plus a sliced explorer.
  See the CHANGELOG. ~~Still open: tiling footprint *polygons* rather than
  centroids, which is what the embedded-slice mode still has over it.~~
  **shipped** — the archive now carries a `footprints` polygon layer (clipped per
  tile, from zoom 6 up) beside the centroids, and both the explorer and the
  minimal viewer draw coverage shape as you zoom in. ~~The embedded-slice mode's
  one remaining extra is the on-click "Get SAR image" COG overlay.~~ **shipped
  too** — every tiled feature now references its GEC cloud-optimized GeoTIFF (a
  bare filename resolved against the `stac_href` the tiles already carry) plus the
  bounds to place it, and the shared geotiff.js driver gained a MapLibre
  `image`-source placement beside its Leaflet `imageOverlay` one, so the
  whole-archive explorer streams the radar picture on click like the slice one.
  ~~The two modes now differ only in the two fields vector tiles do not encode
  (polarizations and the per-product asset list).~~ **Shipped, and Path A is
  closed** — those two fields (`pol`, `assets`) are tiled comma-joined, which
  both finished the detail panel and bought a facet *neither* explorer had: a
  **polarization filter**, the one that decides whether a change measurement is
  valid rather than what it shows (`POST /artifacts/stats` refuses a
  mixed-polarization selection; the page had no control to narrow one). Chips in
  both modes, compiled to a MapLibre `index-of` test inside the tiles for the
  whole-archive one. The whole-archive front end is now a strict superset of the
  embedded-slice one. See the CHANGELOG.
- ~~Bake place labels into the *published* weekly snapshot.~~ **shipped** —
  `umbra index bake --by-site` geocodes once per site rather than once per
  acquisition (a task's passes share their ground), which brings a whole-catalog
  bake inside Nominatim's ~1 req/s policy, and `publish-index.yml` now runs it
  (bounded, non-blocking) before the derived artifacts, so the fetched
  `catalog.db`, the parquet export and `catalog.pmtiles` all arrive pre-labelled.
  `umbra tiles` was also taught to tile the baked label rather than the task
  codename. See the CHANGELOG. ~~Still open: baking per-item **thumbnails** into
  the published snapshot, which is gated on egress (a COG overview streamed per
  acquisition) rather than a rate limit.~~ **shipped** — the pictures are
  published as a separate `catalog.thumbs.db` sidecar (`umbra index
  fetch-thumbnails` / `export-thumbnails`), not as a column of `catalog.db`, so
  the metadata download that promises "instant local search, no crawl" stays
  small and the pixels are opt-in. The egress gate is answered by making the
  bake *incremental* — the weekly run re-imports the previous sidecar and then
  streams only the acquisitions added since, bounded by `--limit` and spent
  newest-first — so the archive is re-listed weekly but never re-streamed. See
  the CHANGELOG. **This closes the demo / hosting polish group.** Those published
  pixels have since gained a consumer beyond the pictures: `umbra describe
  --preview {baked,auto}` reads one as the picture a vision model is *shown*, so
  the C2 reading costs no S3 overview stream per call and needs no `viz` extra at
  all — a description from an `[ai]`-only install. It is opt-in and self-describing
  rather than automatic, because a 128 px preview is different evidence from a
  1024 px render: every description records the picture it read
  (`SceneDescription.image`), a smaller one carries a caveat naming what it could
  not have seen, and a request the bake cannot answer is refused by the one
  function that also explains it rather than silently substituted. See the
  CHANGELOG. ~~**Open:** what the bake *was* had to be assumed, since the index
  stored a preview's bytes and nothing else — so the refusal covered every asset
  but `GEC`, including a `--asset CSI` bake made deliberately, and a sidecar merge
  kept whichever preview arrived first rather than the better one.~~ **shipped** —
  schema v4 records the asset and the size each preview was baked at
  (`CatalogIndex.get_preview` / `BakedPreview` beside the unchanged bytes-only
  `get_thumbnail`), which turns both assumptions into reads: a `CSI` bake now
  answers a `CSI` reading and a `GEC` one is refused *naming what it is*, and
  `umbra index fetch-thumbnails` keeps a local bake unless the incoming one is a
  larger preview of the same product — because the published sidecar is 128 px
  where a local bake defaults to 256, which had made a scene's preview resolution
  a fact about the order two commands were run in. Absence stays absence: a
  preview from an older index reads as *unknown* and falls back to the assumed
  default rather than being treated as a claim.

**SAR-processing depth (was workstream 5.5)**

- ~~Gamma-nought RTC by facet integration in image space.~~ **shipped** —
  `umbra convert --rtc --rtc-model facet` accumulates each terrain facet's
  illuminated area into the radar cell it images into and normalises by that
  total, so folded ground (layover) is suppressed together rather than corrected
  pixel-by-pixel; flat terrain is unchanged and a planar slope reduces to the
  `area` × `gamma` closed form. See the CHANGELOG.
- ~~Radiometric *calibration* (every RTC model normalises detected amplitude, so
  the output was a relative image).~~ **shipped** — `umbra convert --calibrate
  {sigma0,beta0,gamma0,rcs}` applies the SICD's own `Radiometric` scale-factor
  polynomial to pixel power, in image space where the polynomial is defined
  (metres from the SCP, chip origin included), so the result is a physical
  backscatter coefficient — and because it is a power-domain factor like the
  flattening, the two compose into a terrain-flattened gamma-nought product.
  Where the metadata cannot support it — Umbra's open products generally carry no
  `Radiometric` block — the refusal is explicit and names what the product does
  carry, and `sicd_calibration_types` reports it without trying. See the
  CHANGELOG.
- ~~Record what a converted raster *is* (every setting above left no trace in the
  file, so two scenes converted differently were indistinguishable after the
  fact).~~ **shipped** — `conversion_tags` writes namespaced `UMBRA_*` GeoTIFF
  metadata into every raster the module emits: the calibration, the RTC model and
  the reference incidence angle it resolved to, the DEM/geoid used, the
  projection, the resampling kernel, the amplitude scale, what a pixel value is,
  the umbra-py version, and the CC-BY licence + attribution (design principle 4
  applied to a derivative). Steps that did not run report `"none"` rather than
  vanishing, and only the source *file name* is recorded. Read it with
  `read_conversion_tags`, `umbra convert --provenance`, or `gdalinfo`. See the
  CHANGELOG.
- ~~Radiometric calibration made a pixel physical without making it the
  *ground*: a measured value is the echo plus the receiver's own thermal noise,
  so over a low-backscatter surface a calibrated number reported the sensor's
  sensitivity.~~ **shipped** — `umbra convert --subtract-noise` /
  `sicd_to_geocoded_cog(noise_subtract=True)` evaluates the SICD's own
  `Radiometric.NoiseLevel.NoisePoly` per pixel, in the image coordinates the
  pixels came from (so a `--clip-bbox` window gets its own part of the swath),
  and subtracts it from detected power **before** anything scales it — the one
  correction in the module that is not a multiplicative factor, which is exactly
  why its position in the chain is the design. Where the floor meets the
  measurement the residual is floored rather than driven negative: that pixel is
  at the sensor's limit, which is a fact about the radar. `--rtc-model facet
  --calibrate gamma0 --subtract-noise` is therefore a terrain-flattened
  gamma-nought coefficient with the receiver removed, and `umbra chips
  --subtract-noise` carries the same to a training set. A `RELATIVE` noise level,
  a missing one, and a product with no `Radiometric` block each raise rather than
  subtract a guess. See the CHANGELOG.
- ~~The floor was *read*, never inferred, so the correction refused on Umbra's
  open products — which generally carry no `Radiometric` block at all. A shipped
  correction that could not be applied to the archive this library exists for.~~
  **shipped** — `umbra convert --noise-model {measured,estimated}` /
  `sicd_to_geocoded_cog(noise_model=…)` names where the floor comes from, the
  same shape `--rtc` / `--rtc-model` already has. `measured` is the previous
  behaviour exactly; `estimated` takes the 5th percentile of the scene's own
  detected power, on the argument that a SAR image's water, shadow and smooth
  ground return essentially nothing, so what is recorded there is the receiver.
  Everything downstream is untouched — the same subtraction, in the same
  position, first and on raw power — because what differs between the two is the
  provenance of the number, not the physics. That difference is carried
  end-to-end rather than asserted: a distinct `UMBRA_NOISE_SUBTRACTION` value
  (`"estimated"`), the inferred level in `UMBRA_NOISE_FLOOR_DB`, a `to_stack`
  refusal on a series that mixes the two floors, a `stack_stats` caveat naming
  the estimate's two limits (one constant cannot follow the swath; a uniformly
  bright scene has no dark ground to read, so the subtraction takes real
  backscatter off), and a `SicdConversion.cache_key` that keeps an estimated
  conversion out of a measured one's `--work-dir` slot. See the CHANGELOG.
- ~~Those two limits were *stated*, never *measured*, so on any particular scene
  there was no way to tell whether either had bitten.~~ **shipped** — every
  subtraction now records what it did to the image it ran on:
  `UMBRA_NOISE_FLOORED_FRACTION`, how much of the raster the floor drove to the
  sensor's sensitivity limit (either model), and `UMBRA_NOISE_FLOOR_MARGIN_DB`,
  how far the scene's own median power sat above an *inferred* floor. Both were
  already being computed and discarded — `_subtract_noise` makes the comparison
  that defines the first, and `_estimate_noise_power` holds the distribution the
  second is read from — and the second is the estimator's assumption made
  checkable rather than merely documented: the model works because a scene's dark
  surfaces are a different population from its backscatter, so the distance
  between the fifth percentile and the median is the evidence that they were.
  `umbra convert` prints both and, under `NOISE_MARGIN_WARN_DB`, says the scene
  had little dark ground to read and points at `--noise-model measured` — an
  advisory, never a refusal, because a uniform scene is legitimate and the honest
  fix there is a measured floor rather than a tuned guess. They are diagnostics
  of a scene rather than claims about a pixel, so they stay out of
  `MEASUREMENT_PROVENANCE_KEYS` by design: no two real passes agree on them, and
  refusing over them would have ended every series. See the CHANGELOG.
  ~~**Open:** both numbers reached `umbra convert`'s one raster and stopped, so a
  *training set* built from twenty passes could carry a handful of scenes whose
  dark tail was ground rather than receiver with the evidence unread inside the
  files.~~ **shipped** — `umbra chips` now carries them into every manifest
  record (`ChipRecord.noise_floored_fraction` / `.noise_floor_margin_db`, so a
  loader drops the affected scenes with a filter rather than a raster read) and
  rolls them up across the run (`ChipDataset.noise` / `NoiseSummary`, in the
  `--json` payload and on the way out). The roll-up is counted per *acquisition*,
  because the diagnostics describe the scene a chip was cut from; it is derived
  from the records rather than accumulated, so it cannot disagree with the
  manifest beside it; and it is absent from a run where no floor came off, so a
  `GEC` dataset's output is unchanged.
- ~~The inferred floor was one constant for a whole scene, so it could not follow
  the across-swath variation the measured polynomial exists to describe — leaving
  a gradient that tracks the geometry rather than the ground, which is the
  artefact the subtraction exists to remove.~~ **shipped** — `umbra convert
  --noise-model estimated-range` reads the same low tail *per range line* (SICD
  stores range along the image rows) and fits those per-line floors against range,
  so the subtracted floor follows the swath while still needing no metadata. The
  profile is a degree-2 fit rather than a lookup precisely so a real scene cannot
  defeat it: lines with no dark ground are interpolated over, and lines whose tail
  sits more than 3 dB *above* the curve are dropped and the fit redone — one-sided
  because ground contamination can only raise a line's low tail, so a line far
  below the curve is noise-only and is exactly what to believe. What it adds is
  the *shape*, and only that: a percentile of speckled noise sits conservatively
  below that population's mean by nearly the same offset on every line, so the
  bias moves the whole curve down without bending it, and under-subtraction is the
  safe direction. Because it is a different estimator with its own failure mode it
  is recorded as a third value (`"estimated-range"`) rather than quietly changing
  what `"estimated"` means — which is what makes `to_stack` refuse a series that
  mixes a fitted profile with a constant guess, gives `stack_stats` its own caveat
  (one limit gone, the other not), and makes the new
  `UMBRA_NOISE_FLOOR_SPREAD_DB` — the swing of the fitted floor, i.e. what the
  constant model was missing — meaningful by its presence. `umbra chips
  --noise-model estimated-range` carries it to a training set, where a flat floor
  showed up as an offset between chips cut from opposite edges of one swath. See
  the CHANGELOG.
- ~~Every one of the inferred models above rested on an *argument* — the estimate
  reads low but consistently, so the per-line fit recovers the shape a scalar
  cannot — and the archive they exist for could not check it, because a product
  that states no floor states no truth either.~~ **shipped** —
  `compare_noise_models` / `umbra convert --noise-check` scores the inferred
  floors against a product's own `ABSOLUTE` `NoisePoly` where one exists, split
  into the two parts the claims are about: the offset the estimate reads low by
  (`bias_db`) and, once that offset is granted, how well it follows the real floor
  across the image (`shape_error_db`), beside how much swing there was to find at
  all (`measured_spread_db`). On a synthetic SICD whose stated floor *is* the
  floor its pixels were built from, the fitted profile recovers a 10 dB ramp to
  0.1 dB and scores 0.2 dB of shape error where the constant estimate scores
  2.9 dB — exactly the ramp's own deviation about its midpoint, i.e. everything a
  scalar had to leave behind — and both models read low by about 5.5 dB, within a
  decibel of each other, as a fifth percentile of a speckled noise-only population
  should. The measurement also found two things the argument had not: a constant
  estimate is biased low on a *varying* floor even with no speckle at all (a
  pooled percentile lands near the near-range end of a ramp, not its middle), and
  where backscatter sinks toward the floor the fitted profile reads the swing
  ~30% flat. Both are recorded rather than smoothed over — the second is a caveat
  on quoting `UMBRA_NOISE_FLOOR_SPREAD_DB`, and it is the kind of finding that
  only a measurement produces. **Open:** running it on a *real* product, which
  needs a Canopy scene or any SICD carrying the metadata Umbra's open archive
  does not — see `TODO.md`.
- ~~Nothing *consumed* those tags (they were written and read back, but no code
  acted on them, so a stack could still mix two conversions and report the
  difference between them as change).~~ **shipped** — `to_stack` reads every
  source's record while it opens them and refuses, before any warping, a series
  that disagrees on `MEASUREMENT_PROVENANCE_KEYS` (`calibration`, `rtc_model`,
  `scale`, `units`), naming the key, both values and an acquisition on each side;
  a raster with no tags is its own value, so a converted product mixed with a
  published GEC is caught too (`noise_subtraction` has since joined that key set;
  a record that predates a key reads as "that step did not run" rather than as
  the sentinel, so a new measurement key cannot retroactively split a series that
  agrees), while a series of published GECs agrees and is
  unaffected. The keys that legitimately vary per pass (`source`,
  `rtc_reference_deg`) are excluded by design. It is the polarization refusal of
  `POST /artifacts/stats` applied to what the pixel values *are*, and it reaches
  that endpoint as a `400` with no change to `serve.py`. What the sources agree
  on is carried rather than dropped: `to_xarray` / `to_stack` expose it as
  `attrs["provenance"]`, `to_geotiff` and the datacube writer stamp it back into
  the derivative's own `UMBRA_*` tags, and `stack_stats` both reports it and lets
  it correct the two caveats that are claims about the pixel values — a
  calibrated cube stops being told its decibels are relative. See the CHANGELOG.
  ~~**Still open:** the *composite* path made no such check, on the argument that
  a mixed picture is only confusing to look at.~~ **shipped** — one caller on
  that path does not make a picture. `umbra change --narrate` quotes a signed
  **decibel** delta per grid block, ships it as an auditable JSON sidecar and
  hands it to a vision model as ground truth, so `render_change_png` now applies
  the same `MEASUREMENT_PROVENANCE_KEYS` refusal to the pair it measures between
  — before a number is computed and before the model call that would quote it.
  `_coregister_bands` collects each source's record while the datasets are open
  (the only place it is free), the picture commands take it and ignore it — the
  line `POST /artifacts/stats` already draws for polarization — and what the
  passes agree on rides out on `ChangeStats.provenance`, into the sidecar and
  into the model's ground-truth block, so a quoted dB delta can be attributed.
  **Still open:** MultiRTC interop — heavy, research-oriented,
  deferred. **This closes the SAR-processing-depth group bar that interop.**
- ~~Nothing removed **speckle**, the one uncertainty in a calibrated pixel that is
  not the sensor's fault and is bigger than everything that is: a single look's
  power scatters about its surface's true backscatter as widely as its own mean, so
  a pixel-by-pixel difference between two passes was mostly interference rather
  than change.~~ **shipped** — `umbra convert --speckle-filter {boxcar,lee}`
  averages it down in the power domain (`boxcar` unconditionally, `lee` only where
  a window is no more variable than speckle alone explains), records what it did
  in two keys `to_stack` refuses to mix, and measures what it achieved as the
  scene's equivalent number of looks before and after — the number a window size
  cannot supply, since Umbra samples finer than it resolves, so 25 averaged pixels
  buy fewer than 25 independent looks. Not a default: what it spends is
  resolution. `umbra chips --speckle-filter` carries it to a training set with the
  window in every manifest record. See the CHANGELOG.
  ~~**Open:** it filters in the radar's image space, so it reaches only the
  complex archive — and Umbra's *published* GEC rasters, which arrive geocoded and
  never enter that pipeline, had no route to averaging at all. The correction that
  matters most for change detection could not be applied to the products change
  detection is done on.~~ **shipped** — `umbra stack --speckle-filter` /
  `to_stack(speckle_filter=…)` averages each pass down on the cube's shared grid,
  which is the first place a GEC exists on coordinates the library owns. The
  filters are `convert`'s own functions rather than a second implementation, and
  the record is `convert`'s own two keys rather than a second vocabulary — so the
  `stack_stats` caveat, the written cube's `UMBRA_*` tags and the refusal to
  difference a filtered pass against an unfiltered one all work unchanged on a
  cube that filtered itself. Filtering an already-filtered series is refused (two
  averagings leave a resolution neither window names). Pairing the filter with
  `--chunk-size` was refused too, and is not any more: each window is read with a
  half-window halo and cropped after filtering, and `lee`'s looks parameter is
  read once per pass, so the sharpest cube the library can build is also the
  least noisy one it can measure. See the CHANGELOG.
  ~~**Open:** it reached the library and the CLI and stopped there, so every
  measurement made through `umbra serve` or an agent tool — the front doors built
  so nobody has to install anything — was unfiltered with no way to ask
  otherwise.~~ **shipped** — `"speckle_filter": "boxcar" | "lee"` is a request
  field on `POST /artifacts/stats` and a parameter on the `stack_stats` MCP /
  LangChain / LlamaIndex tools, passed straight to `to_stack` so the caveats, the
  provenance keys and the refusals all work unchanged. It is in the artifact cache
  key rather than in the instance policy for the reason `"windowed"` set: it moves
  the numbers, and a cached artifact whose values depend on an invisible server
  flag is the failure mode. What the implementation *found* is that the two are
  complementary — filtering needs each pass whole, windowed measurement needs the
  cube chunked — so each was refused on the instance the other requires, which
  made the pair unsatisfiable everywhere and refusable at the request itself.
  ~~**Open:** which meant the front doors could advertise the choice but never
  remove it, so a hosted measurement of a series too large to hold was
  necessarily the noisiest one.~~ **shipped** — the pair composes now: each
  window is read with a half-window halo (so a filtered chunked cube is the
  whole-pass filter's own answer to a `float32` ulp, not one carrying a seam
  `stack_stats` would report as change) and `lee`'s looks parameter is resolved
  once per pass instead of per window, exactly as `umbra chips` resolves it once
  per acquisition. So `"windowed": true` and `"speckle_filter"` are one request
  on a chunked instance, the request-level refusal is gone, and the landing page
  reports `speckle_filter` as supported everywhere.
  ~~**Open:** the chip-side loader, the one surface whose output is a *dataset*
  rather than a picture or a number — `umbra chips` took `--speckle-filter` only
  on the complex path, so a training set cut from the published GEC rasters could
  not be smoothed at all.~~ **shipped** — `umbra chips --speckle-filter` applies
  to any asset now, averaging the **tiles** on `GEC`/`CSI` and routing into the
  conversion on `SICD`. It is the place the correction matters most, because
  unlike a caveat on a measurement nothing downstream can put back what a model
  was trained without. The two obstacles a tile loop has are answered rather than
  approximated — the pair `to_stack`'s `chunk_size` refusal named as what it would
  take: a half-window **halo** per tile, which makes a filtered tile *bit-for-bit*
  the region of the whole-scene filter (so overlapping tiles agree rather than
  carrying a seam a model would learn), and `lee`'s speckle parameter read **once
  per acquisition** from a fixed grid of sample windows, pooled at block level,
  since it is a property of the product's processing rather than of the 512 pixels
  a tile covers. Both halves of the trade reach the manifest — `speckle_enl_before`
  / `_after` / `_looks` beside `speckle_filter` / `speckle_window` on either path,
  and a `ChipDataset.speckle` roll-up counted per acquisition like the noise one.
  **This closes the speckle group.**
- ~~A product's own metadata could refuse a measurement, but the refusal had no
  name — so a chip batch could only die on the first scene that could not answer,
  and the most common failure a complex conversion has never reached the
  machine-readable error envelope.~~ **shipped** — the five product-metadata
  refusals raise `UnsupportedMeasurementError`, which `umbra chips
  --skip-unsupported` catches (and only it) to keep the run going while recording
  every left-out pass on `ChipDataset.skipped`, and which `cli.main` renders into
  the `--json` envelope with a hint. The same change moved the check ahead of the
  pixel read, so an uncalibratable scene costs its header rather than a whole
  complex read and an amplitude detection. See the CHANGELOG.
  ~~**Open:** the header it cost was the header of a product already downloaded,
  so *discovering* which of a site's twenty passes could be calibrated still cost
  twenty whole-product downloads — the refusal was survivable and cheap per
  scene, and its discovery was neither.~~ **shipped** — `umbra preflight` /
  `sicd_capabilities` / `preflight_items` read the metadata by HTTP range
  request: the NITF's own fixed-width file header states every segment's length,
  so the SICD XML data extension segment is located without reading anything
  between, and two range requests answer for a multi-gigabyte product. The
  answer is `_check_measurement_support` run against an attribute view over the
  parsed XML — the conversion's own check, not a restatement of it — so the
  survivors of a preflight are exactly the passes `umbra chips --asset SICD
  --calibrate gamma0` will not refuse. It needs no extra (stdlib NITF walk,
  stdlib XML), and an acquisition whose metadata cannot be read is its own
  verdict rather than the end of the walk. See the CHANGELOG.
  ~~**Open:** the survivors had to be carried across by hand — the preflight was
  a separate command, so the batch it exists to save still learned which of
  twenty passes could be calibrated by downloading twenty passes.~~ **shipped** —
  `umbra chips --preflight` / `write_chips(preflight=True)` makes the check part
  of the run: each pass's metadata is read by range request, the conversion's own
  support check is applied with the run's *own* settings, and the passes that
  cannot answer never reach a download. A cheaply-found hole is still a hole, so
  a dropped pass is the same `SkippedAcquisition` `--skip-unsupported` produces
  (plus a `stage` field, the one thing the two routes do not share), and
  `ChipDataset.preflight` reports what asking cost against the download it
  removed — counting only the *dropped* products as saved, since a supported pass
  is downloaded anyway. A pass whose metadata cannot be read is kept, because a
  failed read is not a product declaring it cannot answer. See the CHANGELOG.
  ~~**Open:** the walk was serial, so the check that runs *in front of* a batch
  was itself a stall that grew with the number of passes — 40 sequential round
  trips before the first chip, on the one command whose argument is that you
  should not pay twice.~~ **shipped** — `preflight_items(workers=…)` /
  `umbra preflight --workers` / `umbra chips --preflight-workers` read the
  selection through a small pool (8 by default, the catalog walk's own sidecar
  fan-out), which is the whole of what was left: once a verdict costs kilobytes,
  the round trip is the only part that still scales with the selection. The
  widening is a *schedule* and not an answer, and the design is what keeps it
  one: the reads are independent, the verdicts are consumed in the order they
  were asked in — the chip run pairs them against its own selection positionally,
  since two passes of a task can share an id — and `progress` is still called
  once per acquisition, in that order, from the calling thread, so a CLI callback
  never becomes thread-safety-sensitive. `workers=1` runs no pool at all and the
  lane count is capped by the selection, so a one-scene preflight is byte-for-byte
  the path that shipped before. See the CHANGELOG.
  ~~**Open:** every bit of that — the name, the skip, the JSON envelope, the
  metadata-first ordering, the preflight — covered two of the *three* corrections
  that depend on a product describing itself. `--rtc` reads the collection
  geometry out of the same SICD and refused with a bare `ValueError` raised after
  the warp, so a batch died on it, an agent could not branch on it, and no
  preflight could see it coming.~~ **shipped** — `_scene_look_geometry` raises
  `UnsupportedMeasurementError` (still a `ValueError`, so nothing that caught one
  changed), which is what makes the whole existing machinery apply with no new
  vocabulary; `_check_measurement_support(rtc=True)` asks it off the metadata
  before the complex read, the DEM fetch and the reprojection; and `umbra
  preflight --rtc` / `preflight_items(rtc=True)` asks it over the wire, with
  `umbra chips --preflight` taking it from the run's own `SicdConversion.rtc` so
  the preflight's question stays the conversion's by construction. What a product
  *does* state is reported beside the calibrations and the noise level
  (`SicdCapabilities.look_geometry`). **This closes the preflight group**: every
  correction whose answer is in the file is now named, skippable and askable
  ahead of the download. See the CHANGELOG.
- ~~The ML on-ramp reached only the *derived* products (`umbra chips` cut tiles
  from GEC/CSI, so a model trained on Umbra data was never trained on the
  full-resolution complex archive).~~ **shipped** — `umbra chips --asset SICD`
  geocodes each complex product through the pipeline above and cuts the identical
  tiles from the result, so the two heaviest subsystems compose: the same window
  loop, the same manifest, and every conversion setting (`--dem`, `--geoid`,
  `--rtc`, `--rtc-model`, `--calibrate`, …) applying to what a training loader
  reads. `--rtc-model facet --calibrate gamma0` makes a chip a terrain-flattened
  gamma-nought measurement rather than relative brightness — the property that
  lets a model transfer between scenes taken at different angles. Provenance is
  read *back* from the converted raster's `UMBRA_*` tags into both the manifest
  and every chip GeoTIFF, so a record reports the processing that ran rather than
  the one requested. The whole-product download a SICD requires is stated rather
  than hidden: opt-in, one scene resident at a time, and `--work-dir` caching the
  geocoded COG under a digest of its settings so a re-run reuses it. See the
  CHANGELOG.
  ~~**Open:** the conversion under it is whole-scene, so chipping a *site* out of
  a series converts every pass entirely to keep a fraction of each.~~
  **shipped** — `umbra chips --clip-bbox` (and `umbra convert --clip-bbox` /
  `sicd_to_geocoded_cog(bbox=…)` beneath it) turns the ground rectangle back into
  the image window that covers it, reads only that window from the product, sizes
  the control points, the radiometric polynomials and the `--dem auto` fetch to
  it, and crops the output to the request — so *both* halves of a
  geocode-then-chip run cost what the area of interest costs rather than what the
  collect does, and the memory a full-resolution complex scene needs stops being
  the ceiling on working with one. The pixel size still comes from the whole
  input, so a clip chooses which ground is written rather than how finely; the
  download stays whole-product, which a slant-plane NITF's lack of a map grid
  makes unavoidable. See the CHANGELOG.
- ~~The datacube's memory ceiling (every pass read eagerly, so a long series had
  to be traded against resolution).~~ **shipped** — `to_stack(lazy=True)` /
  `umbra stack --lazy` (the new `[dask]` extra) make each acquisition one chunk
  fetched on demand, while the shared grid stays eagerly resolved so an
  impossible stack still fails at the call. `stack_to_geotiff` writes band by
  band and `stack_stats` streams the series (first / previous / current pass
  resident), so the whole chain's memory follows the grid rather than the number
  of passes — the numbers are identical either way. See the CHANGELOG.
  ~~**Open:** chunking *within* a slice~~ **shipped** — `to_stack(chunk_size=N)`
  / `umbra stack --lazy --chunk-size N` cuts each pass into `N`-square windows
  read (and written) independently, so the unit of work stops being a whole
  `max_size²` slab and the achievable sharpness stops depending on how much of
  *one scene* fits in memory. A window is the shared grid restricted to its own
  rows and columns, so it is pixel-identical to that region of the whole-slab
  read; the cost is one range-read per window rather than per pass, which is why
  it is opt-in. `_write_stack_geotiff` follows the cube's chunks, so the written
  file is byte-identical however it was read. See the CHANGELOG.
  ~~**Open:** letting `umbra serve`'s `POST /artifacts/stats` opt into the lazy
  path.~~ **shipped** — `umbra serve --stack-lazy [--stack-chunk-size N]
  [--stack-scheduler {synchronous,threads}]` gives the one endpoint whose cost
  grows with the *number* of acquisitions the same ceiling-lift the CLI has.
  It is an instance-wide policy (`serve.StackExecution`), not a request field,
  because it needs the `dask` extra on the server and a decision about the
  threads one request may spend — `synchronous` by default, since a request
  handler that quietly starts a thread pool per render is a worse surprise than
  a slower one. The scheduler is set as a context manager around the single
  render (so it cannot leak), the eager default never imports `dask`, and
  because the numbers are identical either way the policy is deliberately *not*
  in the artifact cache key: an operator flips it without invalidating a cached
  artifact. See the CHANGELOG.
  ~~**Open (not a blocker, in `TODO.md`):** `stack_stats` still materialises one
  slice per pass — its medians and percentiles need the pass whole, so streaming
  it would mean approximate quantiles, i.e. changing the numbers.~~ **shipped** —
  `stack_stats(windowed=True)` / `umbra stack --stats-windowed` walks the cube's
  own windows instead of whole passes, so three windows are resident rather than
  three slices and a cube stacked sharper than a slice you can hold is
  *measurable* and not only writable. The decision this entry deferred was made
  rather than dodged: every count, mean, standard deviation and change number is
  still exact (each is a sum, so a window folds into an accumulator), and the
  percentiles — the one statistic that needs the pass whole — become estimates
  from a mergeable 0.05 dB histogram, good to about a bin. What makes that
  acceptable is that the summary *says which numbers are which*
  (`quantile_method` / `quantile_bin_db` + a caveat), and that a default summary
  is byte-identical to before, so nobody gets an estimate they didn't ask for.
  Blocks are cut from the shared grid, not from a window, so a misaligned window
  edge leaves the spatial breakdown identical. **This closes the datacube's
  memory ceiling end to end — build, write and measure.** See the CHANGELOG.
  ~~**Open (not a blocker, in `TODO.md`):** the mode is library + CLI only; `umbra
  serve`'s `POST /artifacts/stats` and the agent tools do not expose it, because
  there it would have to enter the artifact cache key (unlike `--stack-lazy`,
  this one *does* move numbers).~~ **shipped for the server** — `"windowed":
  true` on `POST /artifacts/stats`, and the cache-key question was answered by
  making it a **request option** rather than an instance policy: because it
  moves the percentiles it belongs in the key, and putting it in the request
  body puts it there for free, so a cached artifact can never depend on an
  invisible server flag. It is refused (`400`) on an instance without
  `--stack-chunk-size`, where there are no windows to walk and it could only
  estimate percentiles for the same memory. The exact numbers are pinned
  identical through the endpoint's own renderer. The agent tools are left out on
  purpose: they build an eager 512-pixel cube, so there is no ceiling there to
  lift and `windowed` would only make a model's percentiles approximate. See the
  CHANGELOG.

**Agent-session hardening (was `STRATEGY` §7 follow-on)**

- ~~A SessionStart hook / permission allowlist for remote coding-agent sessions.~~
  **shipped** — `.claude/hooks/session-start.sh` (registered in
  `.claude/settings.json`) installs the package editable with every extra on a
  Claude-Code-on-the-web container so `ruff` / `mypy` / `pytest` and the `umbra`
  CLI work from the first turn (mirroring CI's `test-all-extras` job), and the
  same `settings.json` pre-approves the documented dev-loop + read-only commands.
  Gated on `$CLAUDE_CODE_REMOTE`, idempotent, synchronous, dev-tooling only (no
  runtime code, nothing on the published package). See the CHANGELOG.

**Getting the published artifacts to actually exist**

- ~~The rolling `catalog-index` release had never been produced, so every
  artifact this project publishes was a 404.~~ **Fixed** — the weekly
  `publish-index` workflow had died in the same step on both of the only two
  runs it has ever had: `umbra tiles --local --db catalog.db`, where the gather
  commands spell that option `--index-db` (`--db` is the decibel stretch on the
  render commands). Because the tiling step ran *before* the release step, a
  completed crawl, 2 725 baked place labels and a good parquet export went with
  it and the release was never created — so `umbra index fetch`, the parquet,
  `catalog.pmtiles`, the thumbnail sidecar and the Pages showcase built from
  them were all dark. The invocation is corrected, each artifact is now uploaded
  by the step that builds it (the crawl publishes before anything is derived
  from it, so a failure deriving the basemap costs the basemap and not the
  snapshot), and `tests/test_workflows.py` parses every `umbra …` invocation in
  `.github/workflows/*.yml` against the real Click command tree so the same
  drift fails a pull request rather than a Monday morning. See the CHANGELOG.
  ~~**Open, and operational rather than code:** the first good snapshot needs a
  maintainer to dispatch a run.~~ **Done** — run 3 of `Publish catalog index`
  (dispatched 2026-07-27) succeeded, and the rolling `catalog-index` release now
  carries all five artifacts: `catalog.db`, `umbra-open-data.parquet`,
  `catalog.pmtiles`, `catalog.thumbs.db` and `catalog.html`. `umbra index fetch`,
  `umbra tiles --fetch`, the thumbnail sidecar and the Pages showcase built from
  them resolve for the first time. **This group is closed**; the weekly cron
  keeps it current.

**Maintainer / relationship actions (no code)**

- Register the PyPI Trusted Publisher and cut the `v0.1.0` GitHub Release to
  claim the name (release plumbing already ships).
- The ecosystem-visibility actions in §5.3, the "offer it upstream" move in
  §5.2, and the "talk to Umbra" conversation in §5.6.

Fine-grained follow-ons for individual shipped features are tracked in
[`TODO.md`](TODO.md); the record of everything already delivered is in
[`CHANGELOG.md`](../CHANGELOG.md).
