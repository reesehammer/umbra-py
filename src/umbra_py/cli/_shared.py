"""Helpers and shared Click option groups behind the ``umbra`` commands.

Everything here is used by more than one command module: the geography /
task-name / acquisition-property option groups (one decorator per family, so
a new front door cannot ship with fewer filters than its siblings -- see
``tests/test_cli_option_groups.py``), the search-vs-explicit-URLs gathering
(:func:`_gather_items` / :func:`_search_source`), and the small formatting and
manifest helpers. Command modules import these names; nothing here imports a
command module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from .._http import get_json
from .._spinner import OrbitSpinner
from ..catalog import UmbraCatalog
from ..constants import CANOPY_TOKEN_ENV
from ..exceptions import GeocodeError
from ..geocode import geocode_place
from ..index import (
    BakedPreview,
    CatalogIndex,
    default_index_path,
)
from ..models import UmbraItem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..showcase import FeaturedSite


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise click.BadParameter("bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    return (parts[0], parts[1], parts[2], parts[3])


def _resolve_search_bbox(
    bbox: str | None, place: str | None
) -> tuple[float, float, float, float] | None:
    """Resolve ``--bbox`` / ``--place`` into a single bounding box (or None).

    ``--place`` is geocoded to a bounding box via Nominatim, and the resolved
    place is echoed so the user can confirm the match before the search runs.
    The two options are mutually exclusive.
    """
    if place and bbox:
        raise click.UsageError("Pass --place or --bbox, not both.")
    if place:
        try:
            resolved, label = geocode_place(place)
        except GeocodeError as exc:
            raise click.ClickException(str(exc)) from exc
        # Status line to stderr so it never corrupts a --json manifest on stdout.
        click.echo(f"Resolved '{place}' to {label}.", err=True)
        return resolved
    return _parse_bbox(bbox)


def _resolve_intersects(value: str | None, *, label: str = "--intersects"):
    """Parse ``--intersects`` into a polygon geometry (or None).

    The value is a path to a ``.geojson``/``.json`` file or an inline GeoJSON
    string -- a ``Polygon`` / ``MultiPolygon`` geometry, or a ``Feature`` /
    ``FeatureCollection`` wrapping one. A path that exists is read; otherwise the
    value is treated as inline JSON. Returns the exterior-ring form the search
    functions expect, and raises ``BadParameter`` at the boundary on bad input.
    """
    from .._geometry import parse_geometry

    if not value:
        return None
    text = value
    candidate = Path(value)
    try:
        if len(value) < 4096 and candidate.is_file():
            text = candidate.read_text()
    except OSError:
        text = value
    try:
        return parse_geometry(text)
    except ValueError as exc:
        raise click.BadParameter(f"{label}: {exc}") from exc


def _resolve_aois(values: tuple[str, ...]):
    """Resolve repeated ``--aoi`` values into named :class:`AreaOfInterest`s.

    Each value is ``NAME=SPELLING`` or just a ``SPELLING`` that
    :func:`_resolve_intersects` already understands (a ``.geojson`` path or
    inline GeoJSON). A bare path takes its file stem as the name -- ``--aoi
    watershed.geojson`` is the area called ``watershed`` -- because the name is
    what a model sees and picks by, so it should read like the thing it is.
    Inline GeoJSON has no stem, so it falls back to a positional ``aoi1``.

    Names are unique: a repeated name is a hard error rather than a silent
    shadow, since the plan selects by name and two areas answering to one label
    would make the audited command ambiguous.
    """
    from ..planner import AreaOfInterest

    areas: list[AreaOfInterest] = []
    seen: set[str] = set()
    for position, value in enumerate(values, start=1):
        # Inline GeoJSON is taken whole: it has no stem to name it by, and an
        # '=' inside a property value must not be read as a NAME= prefix.
        inline = value.lstrip().startswith("{")
        name, _, spelling = ("", "", value) if inline else value.partition("=")
        if not spelling:
            name, spelling = "", value
        stem = "" if inline else Path(spelling).stem
        name = (name or stem or f"aoi{position}").strip()
        if name.lower() in seen:
            raise click.UsageError(
                f"--aoi name {name!r} is used twice; give one of them an explicit "
                "NAME=PATH so the plan can tell them apart."
            )
        seen.add(name.lower())
        geometry = _resolve_intersects(spelling, label=f"--aoi {name}")
        if geometry is None:  # pragma: no cover - _resolve_intersects raises first
            raise click.UsageError(f"--aoi {value!r} did not resolve to a polygon.")
        areas.append(AreaOfInterest(name=name, geometry=geometry, source=spelling))
    return areas


def _resolve_geography(bbox: str | None, place: str | None, intersects: str | None):
    """Resolve the shared geography trio into ``(bbox, geometry)`` search kwargs.

    ``--bbox`` / ``--place`` collapse to one rectangle (see
    :func:`_resolve_search_bbox`) and ``--intersects`` to a polygon (see
    :func:`_resolve_intersects`); a rectangle and a polygon are mutually
    exclusive, since two spatial filters at once is almost always a mistake
    rather than an intersection the user meant.

    Every command that gathers acquisitions by search resolves geography here,
    so ``umbra change --intersects aoi.geojson`` and ``umbra search --intersects
    aoi.geojson`` cannot disagree about what the polygon means.
    """
    if intersects is not None and (bbox or place):
        raise click.UsageError("Pass --intersects or --bbox/--place, not both.")
    return _resolve_search_bbox(bbox, place), _resolve_intersects(intersects)


def _index_path(db_path: str | None) -> Path:
    """Resolve the index database path from an explicit ``--db`` or the default."""
    return Path(db_path) if db_path else default_index_path()


def _baked_thumbnails(items: list[UmbraItem], db_path: str | None) -> dict[str, bytes]:
    """Return a ``{id: PNG bytes}`` map of thumbnails already baked into the local
    index (``umbra index bake-thumbnails``) for the given items.

    Lets ``umbra gallery --local`` embed each preview straight from the index
    instead of re-streaming a cloud-optimized overview from S3 -- instant,
    offline, and (when every tile is baked) with no ``rasterio``. Items with no
    baked thumbnail are simply absent from the map, so the gallery streams those
    the usual way. An absent index yields an empty map (the render falls back to
    streaming entirely), matching the "render it instead" contract of
    :meth:`~umbra_py.index.CatalogIndex.get_thumbnail`.
    """
    path = _index_path(db_path)
    if not path.exists():
        return {}
    baked: dict[str, bytes] = {}
    with CatalogIndex(path) as idx:
        for item in items:
            png = idx.get_thumbnail(item.id)
            if png is not None:
                baked[item.id] = png
    return baked


def _baked_previews(db_path: str | None) -> Callable[[str], BakedPreview | None] | None:
    """Return an ``(item_id) -> BakedPreview | None`` reader over the index's baked
    previews, or ``None`` when there is no index file to read.

    The single-item counterpart of :func:`_baked_thumbnails` (which maps a whole
    search result at once), shaped as
    :data:`~umbra_py.describe.BakedPreviews` for ``umbra describe --preview``. It
    reads :meth:`~umbra_py.index.CatalogIndex.get_preview` rather than the bytes
    alone, because the describe path has to decide whether the cached picture is
    of the product it was asked for. The ``None`` return is load-bearing: it is
    how ``--preview baked`` tells "this machine has no index" (fetch one) apart
    from "this scene is not baked in it" (bake it), which are different fixes.
    """
    path = _index_path(db_path)
    if not path.exists():
        return None

    def lookup(item_id: str) -> BakedPreview | None:
        with CatalogIndex(path) as idx:
            return idx.get_preview(item_id)

    return lookup


def _built_note(built_at: object) -> str:
    """Human-readable build date with staleness for ``umbra index info``."""
    if not built_at:
        return "unknown (built before build stamping, or hand-assembled)"
    try:
        age = (date.today() - date.fromisoformat(str(built_at))).days
    except ValueError:
        return str(built_at)
    if age <= 0:
        return f"{built_at} (today)"
    return f"{built_at} ({age} day(s) ago)"


def _search_source(
    local: bool, db_path: str | None, token: str | None = None
) -> tuple[UmbraCatalog | CatalogIndex, bool]:
    """Pick the search backend: the local index (when ``--local``/``--db`` is
    given), the Canopy commercial archive (when a ``token`` is given), or a live
    open-data :class:`UmbraCatalog`. Returns ``(source, is_index)``."""
    if local or db_path is not None:
        path = _index_path(db_path)
        if not path.exists():
            raise click.ClickException(
                f"No index at {path}. Build one first with 'umbra index build'."
            )
        return CatalogIndex(path), True
    if token:
        return UmbraCatalog(token=token), False
    return UmbraCatalog(), False


def _gather_items(
    *,
    local: bool = False,
    db_path: str | None = None,
    token: str | None = None,
    live_label: str = "Searching Umbra archive",
    **search_kwargs: object,
) -> list[UmbraItem]:
    """Run a search and return the results as a list, choosing the backend the
    same way ``umbra search`` does: a prebuilt local index when ``--local`` /
    ``--index-db`` is given, Umbra's authenticated Canopy commercial archive when
    a ``token`` is given, otherwise a live :class:`UmbraCatalog` open-data S3 walk.

    The visual commands (``map``, ``gallery``, ``swipe``, ``change``,
    ``timescan``, ``chips``) used to re-walk S3 on every render; routing them
    through here lets ``--local`` answer from an already-built index
    (``umbra index fetch`` / ``umbra index build``) instead — near-instant, and
    the fast path a demo or repeat-render flow needs — while ``token`` points the
    same verb at the paid Canopy archive, so a commercial customer renders the
    archive they pay for with the identical flags.
    """
    source, is_index = _search_source(local, db_path, token)
    if is_index:
        label = "Searching local index"
    elif token:
        label = "Searching Canopy archive"
    else:
        label = live_label
    try:
        with OrbitSpinner(label):
            return list(source.search(**search_kwargs))  # type: ignore[arg-type]
    finally:
        if isinstance(source, CatalogIndex):
            source.close()


def _item_from_url(url: str) -> UmbraItem:
    """Fetch one acquisition by its STAC item href.

    The explicit-URL half of how a command obtains its items; :func:`_gather_items`
    is the search half. Both live here so a command module reaches its items
    through one module — which is also the single place a test patches to keep
    the CLI offline.
    """
    return UmbraItem.from_dict(get_json(url), href=url)


def _gather_featured_sites(
    *,
    count: int,
    areas: tuple[str, ...],
    pool_limit: int,
    min_passes: int,
    local: bool,
    db_path: str | None,
    search_kwargs: dict[str, Any],
) -> list[FeaturedSite]:
    """Resolve the sites ``umbra showcase --featured`` precomputes an artifact for.

    Two modes, mirroring the flags: explicit ``--featured-area`` names are
    curated one search at a time (the best-covered site per name wins), while a
    bare ``--featured N`` auto-selects the most repeat-imaged sites from a pool
    of at most ``pool_limit`` acquisitions. Either way the selection itself is
    :func:`umbra_py.showcase.select_featured_sites` — deterministic, no render.

    ``min_passes`` is what the chosen ``--featured-view`` needs of a site (a
    change composite wants its ``--featured-frames``, a timescan three, a swipe
    two), so a site the view could not render is dropped before any network work.

    ``search_kwargs`` carries the rest of the showcase's search (bbox, dates,
    product type) as a dict rather than ``**kwargs`` so the two ``live_label`` /
    ``area`` values this function supplies itself can't be shadowed.
    """
    from ..showcase import select_featured_sites  # noqa: PLC0415

    if not areas and count <= 0:
        return []

    if areas:
        sites: list[FeaturedSite] = []
        for area in areas:
            pool = _gather_items(
                local=local,
                db_path=db_path,
                area=area,
                limit=pool_limit,
                live_label=f"Searching {area}",
                **search_kwargs,
            )
            found = select_featured_sites(pool, count=1, min_passes=min_passes)
            if found:
                sites.append(found[0])
            else:
                click.echo(
                    f"No site with {min_passes}+ passes matched --featured-area "
                    f"{area!r}; skipping it.",
                    err=True,
                )
        return sites

    pool = _gather_items(
        local=local,
        db_path=db_path,
        limit=pool_limit,
        live_label="Searching for repeat-imaged sites",
        **search_kwargs,
    )
    return select_featured_sites(pool, count=count, min_passes=min_passes)


def _local_index_options(func):
    """Attach the shared ``--local`` / ``--index-db`` options that point a
    visual command at a prebuilt index instead of a live S3 walk.

    The path option is ``--index-db`` rather than ``--db`` because the render
    commands already use ``--db`` for the decibel stretch; the flag/behaviour
    otherwise mirror ``umbra search``'s ``--local`` / ``--db``.
    """
    func = click.option(
        "--index-db",
        "db_path",
        default=None,
        help="Path to the local index database to read (default: $UMBRA_INDEX_DB "
        "or ~/.cache/umbra-py/catalog.db). Implies --local. Named --index-db "
        "because --db already means the decibel stretch on render commands.",
    )(func)
    func = click.option(
        "--local",
        is_flag=True,
        help="Gather items from a prebuilt local index (see 'umbra index fetch' "
        "/ 'umbra index build') instead of walking S3 live -- near-instant, the "
        "fast path for repeat renders. Only uses acquisitions already indexed.",
    )(func)
    return func


def _place_option(func):
    """Attach the shared ``--place`` option that geocodes a place name into the
    search bounding box (the OpenStreetMap-Nominatim sibling of ``--bbox``).

    Commands whose help text says something more specific about the scope it
    sets (``umbra map``, ``umbra index build``, ...) keep their own wording; this
    is the generic form, so a command that gathers acquisitions never has to go
    without the option just because nobody wrote bespoke help for it.
    """
    return click.option(
        "--place",
        default=None,
        help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding "
        "box and gather within it, via OpenStreetMap Nominatim. Mutually "
        "exclusive with --bbox; the match is rectangular, so it can include "
        "nearby areas outside the named place.",
    )(func)


def _geometry_option(func):
    """Attach the shared ``--intersects`` polygon filter.

    The library, the local index and the ``umbra serve`` STAC API have all
    filtered by polygon since the geometry search shipped, but only ``umbra
    search`` exposed it -- so every render, analysis and index command was
    rectangle-only, and an area of interest that isn't a rectangle (a coastline,
    a border, a catchment) had to be over-approximated by its bounding box and
    the surplus scenes thrown away by hand. This is the one definition; see
    :func:`_resolve_geography` for the shared resolution.
    """
    return click.option(
        "--intersects",
        default=None,
        help="Keep only items whose footprint intersects this GeoJSON polygon -- a "
        "path to a .geojson file or an inline GeoJSON string (Polygon / "
        "MultiPolygon, or a Feature / FeatureCollection wrapping one). A tighter "
        "spatial filter than the rectangular --bbox; the two are mutually exclusive.",
    )(func)


def _area_option(func):
    """Attach the shared ``--area`` task/site-name filter.

    Umbra files every pass of a site under one named task directory, so naming
    the site lists just that directory instead of scanning the archive -- the
    cheapest filter the catalog has, and the one that gathers the co-located
    passes the change/timescan/stack verbs need.

    Like :func:`_place_option`, this is the generic form: commands whose help
    text says something more specific about what the name scopes (``umbra
    search``, ``umbra stack``, ``umbra index build``, ...) keep their own
    wording. It exists so a command that gathers acquisitions never goes without
    the option just because nobody wrote bespoke help for it -- which is exactly
    how ``umbra map`` ended up the one gather command that could not name a site.

    Pair it with :func:`_fuzzy_option`; ``--fuzzy`` without ``--area`` is inert.
    """
    return click.option(
        "--area",
        default=None,
        help="Case-insensitive name of an Umbra task/site to gather (e.g. "
        "'Centerfield'). Faster than a broad scan -- it lists just that area's "
        "directory.",
    )(func)


def _fuzzy_option(func):
    """Attach the shared ``--fuzzy`` flag that widens ``--area`` from a literal
    substring to the deterministic token-wise match in :mod:`umbra_py.fuzzy`
    (word-order- and punctuation-independent, typo-tolerant, no model call)."""
    return click.option(
        "--fuzzy",
        is_flag=True,
        help="Match --area loosely: word-order- and punctuation-independent and "
        "typo-tolerant (so 'utah centerfield' or 'centrfield' still reach "
        "'Centerfield, Utah'). Deterministic, no model call; a strict superset "
        "of the substring match.",
    )(func)


def _acquisition_filter_options(func):
    """Attach the shared SAR acquisition-property filters (``--pol``,
    ``--min-incidence`` / ``--max-incidence``, ``--max-resolution``).

    These are the first-class radar-discovery filters beyond geography and date
    -- the metadata is already on every :class:`~umbra_py.models.UmbraItem`, so
    this closes the "gather everything, then filter client-side" gap. A set
    filter excludes items lacking that property (the STAC Query-extension
    convention); see :meth:`UmbraItem.matches_filters`.
    """
    func = click.option(
        "--max-resolution",
        type=float,
        default=None,
        help="Keep only items at least this fine: both range and azimuth "
        "resolution <= this many metres. Items missing a resolution are excluded.",
    )(func)
    func = click.option(
        "--max-incidence",
        type=float,
        default=None,
        help="Keep only items with view incidence angle <= this many degrees. "
        "Items missing an incidence angle are excluded.",
    )(func)
    func = click.option(
        "--min-incidence",
        type=float,
        default=None,
        help="Keep only items with view incidence angle >= this many degrees. "
        "Items missing an incidence angle are excluded.",
    )(func)
    func = click.option(
        "--pol",
        "polarizations",
        multiple=True,
        help="Keep only items exposing this polarization (e.g. VV, HH; "
        "repeatable, case-insensitive, matches if the item has ANY of them). "
        "The filter that keeps a change comparison like-with-like -- HH and VV "
        "image different physics. Items with no polarization metadata are excluded.",
    )(func)
    return func


def _acquisition_filter_kwargs(
    polarizations, min_incidence, max_incidence, max_resolution
) -> dict[str, Any]:
    """Normalise the ``_acquisition_filter_options`` values into ``search``
    keyword arguments (an empty ``--pol`` tuple becomes ``None``).

    Typed ``dict[str, Any]`` (not ``object``) so the mapping unpacks cleanly with
    ``**`` into ``_gather_items``/``search`` alongside their explicitly-typed
    keyword parameters."""
    return {
        "polarizations": list(polarizations) or None,
        "min_incidence": min_incidence,
        "max_incidence": max_incidence,
        "max_resolution": max_resolution,
    }


def _acquisition_filter_manifest(
    polarizations, min_incidence, max_incidence, max_resolution
) -> dict[str, object]:
    """Return only the *set* acquisition filters, for a render manifest's
    ``parameters`` -- so a rendered artifact records which SAR filters shaped the
    acquisitions it was built from. Unset filters are omitted, so an unfiltered
    render's manifest is byte-for-byte unchanged."""
    out: dict[str, object] = {}
    if polarizations:
        out["polarizations"] = list(polarizations)
    if min_incidence is not None:
        out["min_incidence"] = min_incidence
    if max_incidence is not None:
        out["max_incidence"] = max_incidence
    if max_resolution is not None:
        out["max_resolution"] = max_resolution
    return out


def _token_option(func):
    """Attach the shared ``--token`` option that points a render/analysis command
    at Umbra's authenticated Canopy commercial archive instead of the open bucket.

    ``umbra search`` already takes a ``--token`` (:mod:`umbra_py.catalog`); this
    threads the *same* commercial-archive backend through ``_gather_items`` so a
    paying Canopy customer renders and analyses the archive they pay for with the
    identical flags (the funnel made literal — the tool learned on the free data
    *is* the tool used on the paid archive). Falls back to
    ``$UMBRA_CANOPY_TOKEN``; mutually exclusive with ``--local`` / ``--index-db``.
    """
    return click.option(
        "--token",
        default=None,
        envvar=CANOPY_TOKEN_ENV,
        help="Canopy API token. When given, gather items from Umbra's "
        "authenticated COMMERCIAL archive (a real STAC API) instead of the open "
        "bucket — the same flags, over the paid catalog. Falls back to "
        f"${CANOPY_TOKEN_ENV}. Mutually exclusive with --local / --index-db.",
    )(func)


def _check_token_not_local(token, local, db_path) -> None:
    """Guard: the Canopy ``--token`` backend queries the live commercial archive,
    so it cannot be combined with a local open-data index (``--local`` /
    ``--index-db``). Mirrors the check ``umbra search`` makes for ``--token``."""
    if token and (local or db_path is not None):
        raise click.ClickException(
            "--token renders from the live Canopy commercial archive and cannot "
            "be combined with --local / --index-db (which read a local open-data "
            "index)."
        )


def _manifest_option(func):
    """Attach the shared ``--json`` flag that turns a render command's human
    "Wrote ... to ..." output into a machine-readable manifest on stdout.

    The manifest is ``{output, items_used, parameters}`` (see
    ``docs/schemas/render-manifest.schema.json``) so an agent knows exactly what
    file was produced, from which acquisitions, and with which settings -- the
    success-side counterpart to the machine-readable error contract. Progress and
    warnings still go to stderr, so stdout carries the JSON object alone.
    """
    return click.option(
        "--json",
        "as_json",
        is_flag=True,
        help="Emit a machine-readable {output, items_used, parameters} manifest "
        "on stdout instead of the human 'Wrote ...' line. Progress and warnings "
        "stay on stderr, so stdout is the JSON object alone.",
    )(func)


def _emit_render_manifest(out_path, items, parameters, sidecars=None, stats=None) -> None:
    """Print a render command's success manifest as JSON to stdout.

    ``items`` are the acquisitions the artifact was built from (their ids become
    ``items_used``); ``parameters`` is the command-specific settings dict;
    ``sidecars`` maps a name to any auxiliary file the command also wrote (e.g. a
    change narration JSON); and ``stats`` is any measurement of the artifact the
    command also computed (``umbra stack --stats``), carried inline rather than
    as a second document so stdout stays one object. Matches
    ``docs/schemas/render-manifest.schema.json``.
    """
    manifest: dict = {
        "output": str(out_path),
        "items_used": [it.id for it in items],
        "parameters": parameters,
    }
    if sidecars:
        manifest["sidecars"] = {name: str(path) for name, path in sidecars.items()}
    if stats is not None:
        manifest["stats"] = stats
    click.echo(json.dumps(manifest, indent=2))


def _sha256_file(path: Path) -> str:
    """Stream a file through SHA-256, bounding memory to one chunk."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _progress_printer(label: str):
    def cb(done: int, total: int | None) -> None:
        if total:
            pct = 100 * done / total
            click.echo(
                f"\r  {label}: {done / 1e6:.1f}/{total / 1e6:.1f} MB ({pct:4.1f}%)", nl=False
            )
        else:
            click.echo(f"\r  {label}: {done / 1e6:.1f} MB", nl=False)

    return cb


def _parse_percentile(value: str) -> tuple[float, float]:
    parts = value.split(",")
    if len(parts) != 2:
        raise click.BadParameter("percentile must be 'low,high' (e.g. '2,98')")
    try:
        lo, hi = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise click.BadParameter("percentile values must be numbers") from exc
    return (lo, hi)


def _search_subtitle(area, bbox, start, end) -> str | None:
    """A short, human-readable description of a search, for a page header."""
    parts: list[str] = []
    if area:
        parts.append(area)
    elif bbox:
        parts.append(f"bbox {bbox}")
    if start or end:
        parts.append(f"{start or '…'} → {end or '…'}")
    return " · ".join(parts) or None
