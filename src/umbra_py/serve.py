"""``umbra serve`` -- a read-only STAC API façade over the local catalog index.

Umbra publishes a *static* STAC catalog (a tree of ``catalog.json`` files) and
**no STAC API**, which is exactly what breaks the standard geospatial tooling:
``pystac-client``, the QGIS STAC plugin, ``stac-browser`` and leafmap all speak
the STAC API *search* protocol, and there is nothing here for them to talk to.
:class:`umbra_py.CatalogIndex` already mirrors the search semantics in SQL, so
putting a small read-only STAC API in front of it turns this library into the
bridge: point any STAC API client at ``http://localhost:8000`` and Umbra's open
archive becomes searchable like Sentinel-1 or Landsat.

This buys two ecosystems from one component:

- **The geo ecosystem** -- every tool above consumes ``/search``,
  ``/collections`` and ``/collections/{id}/items`` without custom glue.
- **The AI ecosystem** -- STAC API is a well-documented, schema'd REST surface
  that OpenAPI-driven agents (and everything that isn't MCP) consume from the
  generated OpenAPI document alone. It is the browser-facing sibling of the
  ``umbra-mcp`` server: same index underneath, a different front door. That
  document describes the *artifact* routes too: the committed contracts they
  emit (``docs/schemas/stack-stats``, ``stack-provenance``, ``render-job``) are
  merged into it as components by :func:`openapi_components`, so a generated
  client reads the same shape the CLI and the agent tools do rather than a bare
  object.

On top of *discovery* the server also renders *artifacts on demand*, so a
front end (or an agent) can trigger the library's visual products over **any**
site straight from HTTP, not just a curated set baked at build time
(the R4 self-serve gap the demo-gap analysis named):

- ``GET  /artifacts/quicklook/{item_id}.png`` -- one acquisition's SAR quicklook;
- ``GET  /artifacts/thumbnail/{item_id}.png`` -- its baked quicklook thumbnail,
  served straight from the index with no render (``umbra index bake-thumbnails``);
- ``POST /artifacts/change``   -- a 2--3 date change composite over a query;
- ``POST /artifacts/timescan`` -- a temporal-statistics composite over a series;
- ``POST /artifacts/swipe``    -- an interactive before/after swipe map (HTML);
- ``POST /artifacts/stats``    -- the same change question answered in *numbers*;
- ``POST /artifacts/provenance`` -- whether that selection is one measurement at
  all, asked (and answered with the largest subset that is) before the numbers
  are spent on it.

The stats endpoint is the odd one out on purpose. Every other artifact is a picture,
which a human reads and a program cannot; ``/artifacts/stats`` runs the
:func:`~umbra_py.load.to_stack` + :func:`~umbra_py.load.stack_stats` reduction
behind the same request shape and returns JSON -- per-pass decibel statistics,
the signed change against the previous pass, how much ground moved past a
threshold (in km², because the grid defaults to the site's UTM zone), and with
``"blocks": N`` which part of the site moved and between which two passes (and
with ``"block_series": true``, each block's whole pass-to-pass sequence). It is
the reduction the CLI (``umbra stack --stats``) and the agent front doors
(``stack_stats`` on MCP / LangChain / LlamaIndex) already expose, finally
reachable over HTTP -- so a QGIS user, a browser front end or an OpenAPI-driven
agent can *measure* a site without installing the ``load`` extra locally.

``/artifacts/provenance`` is the preflight for it, and the one endpoint that
neither renders nor caches. ``/artifacts/stats`` refuses a selection whose
rasters were made by different ``umbra convert`` settings, because differencing
two conversions puts their difference on the time axis; that refusal used to be
discoverable only by spending the request, and named a subset it could not
identify. This endpoint asks the same question first
(:func:`~umbra_py.load.stack_provenance`, one COG header per pass and no
pixels), reports a mix as a ``200`` rather than a ``400`` -- the mix *is* the
answer -- and returns the largest agreeing subset with the hrefs to re-run on.
It is the move ``umbra preflight`` makes for a chip run and ``umbra stack
--provenance`` for a local one, on the front door that answers for people who
installed nothing.

These wrap the existing :mod:`umbra_py.viz` and :mod:`umbra_py.load` functions
unchanged and cache every result to disk keyed by its inputs, so a repeat
request is a file read (closing the "no artifact caching" gap for these
endpoints). Two properties keep them in the package's grain: the renderers are
**injectable** (``build_app(..., renderers=...)``), so the routes are
unit-testable in the core install with no network and no ``viz``/``load``
extra; and they are opt-out (``--no-artifacts``) for a public instance that
wants to bound COG-streaming egress.

``/artifacts/stats`` is also the only endpoint whose cost grows with the
*number* of acquisitions rather than with one render, so how it builds its cube
is an instance-wide setting rather than a request field: ``umbra serve
--stack-lazy [--stack-chunk-size N] [--stack-scheduler ...]``
(:class:`StackExecution`) hands the server the same ``to_stack(lazy=…)`` /
``chunk_size=`` ceiling-lift the CLI has, so a hosted instance can measure a
long series without holding every pass at once. It is operator-configured
because it needs the ``dask`` extra *on the server* and a decision about the
threads one request may spend -- and because the numbers do not move, only the
memory, it is not part of the artifact cache key.

The *measurement* side of that ceiling is the request's call, for the opposite
reason: ``"windowed": true`` reduces the cube window by window
(``stack_stats(windowed=True)``) instead of a slice per pass, which is what
stops a chunked build from being re-materialised a whole slice at a time -- but
it turns each pass's median/p5/p95 into histogram estimates. A number that moves
belongs in the cache key, so it is a request option rather than a policy: two
clients asking different questions of the same passes get different cache
entries, and no cached artifact's quantiles depend on an invisible server flag.
It needs a chunked instance to lower anything, so on one without
``--stack-chunk-size`` it is refused (``400``) rather than silently answered
with worse percentiles and identical memory.

``"speckle_filter": "boxcar" | "lee"`` is a request option for the same reason
and a larger one. Speckle -- the interference pattern coherent illumination
makes on rough ground, whose standard deviation equals its mean on a single look
-- is the dominant uncertainty in a per-cell decibel delta, so an unfiltered
hosted measurement quotes mostly interference wherever the change is small.
``to_stack(speckle_filter=…)`` averages it down on the shared grid before
anything is measured, the cube records what it did in ``umbra convert``'s own
``speckle_filter`` / ``speckle_window`` keys, and ``stack_stats`` turns that
record into the caveat that states the trade: the window that removed the
variance also spent the resolution, so a cell reports ground several cells
across. That is a number *and* a caveat moving, so it is in the cache key.
It used to be the exact complement of ``windowed`` -- filtering needed each pass
whole, so it was refused on exactly the chunked instance ``windowed`` requires,
which made the pair unsatisfiable everywhere. ``to_stack`` now reads each window
with a half-window halo and resolves ``"lee"``'s speckle parameter once per pass,
so the two compose: a chunked instance answers both, and the sharpest cube this
server can build is also the least noisy one it can measure. ``speckle_filter``
is honoured on every instance.

*Which* options an instance takes is still a fact a client needs before it asks:
``windowed`` remains a property of how the server builds its cube. The landing
page's ``stats`` link carries it (:data:`STATS_CAPABILITY_FIELD` ->
:func:`stats_capabilities`): each option reports whether this server supports it
and, when it does not, the reason it would be refused with -- the same string
:func:`stats_option_refusal` gives the renderer, so the advertisement cannot
drift from the ``400`` it predicts.

Renders are synchronous by default -- a single composite streams a downsampled
overview per pass and returns in seconds -- but a composite request can opt in
to ``"async": true`` for a small job queue: it gets a ``202 Accepted`` + a job
id back immediately, polls ``GET /jobs/{id}`` for status, and fetches the
finished artifact from ``GET /jobs/{id}/result``. There is no separate result
store -- the render still writes the same content-addressed disk cache, so a
completed job's result *is* a cache entry (and an async request whose key is
already cached returns an already-``succeeded`` job with no work). The queue's
executor is injectable too, so it stays offline-testable without wall-clock
timing.

Two design commitments carry over from the rest of the package:

- **Deterministic core, thin edge.** The STAC documents are built by plain,
  offline functions (:func:`landing_page`, :func:`collection`,
  :func:`item_to_stac`, :func:`search_result`) with no web-framework
  dependency, so they are unit-testable in the core install. :func:`build_app`
  only wires those functions onto FastAPI routes.
- **Index-first, fast on the first request.** Backed by the prebuilt
  ``catalog.db`` (``umbra index fetch``), every query is a local SQL read, so
  the server answers in milliseconds instead of re-walking S3. A live-catalog
  fallback exists for convenience but is intentionally slow.

Run it with ``umbra serve`` (needs the ``serve`` extra:
``pip install 'umbra-py[serve]'``). ``umbra serve --public`` is the hosted
community instance: STAC search and Streamable HTTP MCP on one process
(``POST /mcp``), artifacts off so this host does not proxy Umbra COGs, a
per-client request cap, CC-BY license headers, and a refuse of Canopy / model
keys and of a live S3 walk. Railway's start command is that flag.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ._geometry import Geometry, parse_geometry
from .catalog import DateLike, _coerce_date
from .constants import (
    ATTRIBUTION,
    CANOPY_TOKEN_ENV,
    DATA_LICENSE,
    PRODUCT_ASSETS,
    PRODUCT_TYPE_EXPLANATIONS,
)
from .convert import SPECKLE_FILTERS, SPECKLE_WINDOW_DEFAULT
from .coverage import site_query_echo
from .exceptions import MissingDependencyError
from .index import CatalogIndex, default_index_path
from .load import STACK_AUTO_CRS, STACK_EXTENTS, stack_provenance
from .models import BBox, UmbraItem
from .schemas import load_schema

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

    from .coverage import SiteCoverage

# --------------------------------------------------------------------------
# STAC API constants
# --------------------------------------------------------------------------

#: STAC (and STAC API) version this façade advertises.
STAC_VERSION = "1.0.0"

#: The single collection every Umbra open-data acquisition belongs to. Umbra
#: files all products under one flat archive, so one collection is honest.
COLLECTION_ID = "umbra-open-data"

#: Conformance classes we implement: STAC API core / collections / item-search
#: plus the OGC API - Features classes their clients check for.
CONFORMANCE_CLASSES = (
    "https://api.stacspec.org/v1.0.0/core",
    "https://api.stacspec.org/v1.0.0/collections",
    "https://api.stacspec.org/v1.0.0/ogcapi-features",
    "https://api.stacspec.org/v1.0.0/item-search",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
    # The Query extension, mapped to the index's Umbra-specific filters
    # (``product_types``, free-text ``area``) and the SAR acquisition
    # properties (``sar:polarizations``, ``view:incidence_angle``,
    # ``sar:resolution``); see :func:`parse_query`.
    "https://api.stacspec.org/v1.0.0/item-search#query",
)

#: Default page size, and the ceiling a client can request via ``limit``.
DEFAULT_LIMIT = 10
MAX_LIMIT = 10_000

#: Per-client request cap for ``umbra serve --public``. Search is a local SQL
#: read; the cap exists so a tiny shared host cannot be scraped into a DoS, and
#: so MCP render tools that *do* stream Umbra COGs cannot be burst without
#: bound. ``0`` disables it. Overridable with ``--rate-limit``.
PUBLIC_RATE_LIMIT = 120
PUBLIC_RATE_WINDOW_S = 60.0

#: Paths a probe or a human hits before any query; never count them against
#: the cap (a health check that 429s is worse than a scraper).
RATE_LIMIT_EXEMPT_PATHS = frozenset({"/healthz", "/docs", "/openapi.json", "/redoc"})

#: Env vars a public instance must not hold. Canopy is the commercial archive
#: (STRATEGY.md §6: don't position against it); model keys would turn MCP's
#: opt-in describe/narrate tools into an open wallet.
PUBLIC_SECRET_ENV = (
    CANOPY_TOKEN_ENV,
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
)

#: Umbra's open-data program page; sent as ``Link: rel=license`` so a client
#: that never reads the collection document still gets the attribution rule.
LICENSE_URL = "https://umbra.space/open-data/"

#: Appended to the landing-page description when ``public=True``.
COMMUNITY_INSTANCE_NOTE = (
    "Unofficial community instance, not an Umbra product. Asset hrefs point "
    f"at Umbra's public bucket -- stream them directly. {ATTRIBUTION}"
)

#: ``GET /sites`` defaults: the live-backend pool size (``limit``, bounded by
#: :data:`MAX_LIMIT`; ignored on an index, which ranks the whole archive), how
#: many sites are returned (``top``, bounded by :data:`SITES_MAX_TOP`), and how
#: many dated passes a site needs to qualify (``min_passes``; 2 is the minimum a
#: change composite can use).
SITES_POOL_LIMIT = 500
SITES_DEFAULT_TOP = 20
SITES_MAX_TOP = 100
SITES_MIN_PASSES = 2


def _require_serve():
    """Import FastAPI, or raise a helpful install hint.

    Kept lazy (like the ``viz``/``mcp`` requires elsewhere) so importing this
    module -- and the deterministic document builders below -- never needs the
    web stack; only :func:`build_app`/:func:`serve` do.
    """
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        raise MissingDependencyError(
            "The STAC API server needs the 'serve' extra. Install it with:\n"
            "    pip install 'umbra-py[serve]'",
            hint="pip install 'umbra-py[serve]'",
        ) from exc
    import fastapi

    return fastapi


# --------------------------------------------------------------------------
# Request-parameter parsing (deterministic; no framework dependency)
# --------------------------------------------------------------------------


def parse_bbox(value: str | list[float] | None) -> BBox | None:
    """Parse a STAC ``bbox`` (``"minlon,minlat,maxlon,maxlat"`` or a list).

    Accepts the 6-element 3D form and drops the elevation components, matching
    the 2D footprint bbox the index stores. Returns ``None`` for empty input.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        parts = [float(p) for p in value.split(",")]
    else:
        parts = [float(p) for p in value]
    if len(parts) == 4:
        return (parts[0], parts[1], parts[2], parts[3])
    if len(parts) == 6:
        # 3D bbox: [minx, miny, minz, maxx, maxy, maxz] -> drop z.
        return (parts[0], parts[1], parts[3], parts[4])
    raise ValueError("bbox must have 4 or 6 comma-separated numbers")


def parse_intersects(value: dict | str | None) -> Geometry | None:
    """Parse a STAC ``intersects`` GeoJSON geometry into polygon rings.

    Accepts a GeoJSON ``dict`` (as in a POST body) or a JSON string (as in a GET
    query). Returns ``None`` for empty input. Raises :class:`ValueError` on a
    non-polygon geometry so the handler can answer 400.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_geometry(value)


def _date_part(token: str) -> date | None:
    """Parse one side of a STAC datetime into a date (open sides -> ``None``)."""
    token = token.strip()
    if token in ("", ".."):
        return None
    # Accept full RFC3339 datetimes and plain dates; the index prunes on date.
    head = token.replace("Z", "").split("T", 1)[0]
    try:
        return date.fromisoformat(head)
    except ValueError as exc:
        raise ValueError(f"invalid datetime {token!r}") from exc


def parse_datetime(value: str | None) -> tuple[date | None, date | None]:
    """Parse a STAC ``datetime`` filter into ``(start, end)`` dates.

    Handles a single instant (``2024-01-01`` -> both bounds that day), a closed
    interval (``2024-01-01/2024-02-01``) and half-open intervals with ``..``.
    """
    if not value:
        return (None, None)
    if "/" in value:
        start_s, end_s = value.split("/", 1)
        return (_date_part(start_s), _date_part(end_s))
    d = _date_part(value)
    return (d, d)


def parse_product_types(value: str | list[str] | None) -> list[str] | None:
    """Parse a ``product_types`` filter into a canonical, validated list.

    Accepts a comma-separated string (``"GEC,SICD"``) or a list; matching is
    case-insensitive (uppercased to the canonical :data:`PRODUCT_ASSETS` keys).
    An unknown product type is a :class:`ValueError` rather than a silent empty
    result, so a typo surfaces as a ``400`` instead of "no items match".
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(p) for p in value]
    else:
        raise ValueError("product_types must be a string or list")
    wanted = [p.strip().upper() for p in parts if str(p).strip()]
    if not wanted:
        return None
    unknown = sorted({p for p in wanted if p not in PRODUCT_ASSETS})
    if unknown:
        raise ValueError(f"unknown product_types {unknown}; valid types are {list(PRODUCT_ASSETS)}")
    return wanted


def parse_polarizations(value: str | list[str] | None) -> list[str] | None:
    """Parse a ``polarizations`` filter into an upper-cased list.

    Accepts a comma-separated string (``"VV,VH"``) or a list; the match is
    case-insensitive (an item is kept if it exposes *at least one* of the
    requested polarizations, per :meth:`UmbraItem.matches_filters`). Unlike
    :func:`parse_product_types` there is no fixed vocabulary to validate
    against -- SAR polarizations are a small open set (``VV``/``VH``/``HH``/
    ``HV``) and a value the archive never carries simply matches nothing.
    Returns ``None`` for empty input.
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(p) for p in value]
    else:
        raise ValueError("polarizations must be a string or list")
    wanted = [p.strip().upper() for p in parts if str(p).strip()]
    return wanted or None


def _as_float(prop: str, value: Any) -> float:
    """Coerce a query value to a float, or raise a 400-worthy ``ValueError``."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"query.{prop} value must be a number, got {value!r}") from exc


def _opt_float(value: Any, field: str) -> float | None:
    """Coerce an optional top-level POST body field to a float (``None`` stays)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number, got {value!r}") from exc


def _opt_int(value: Any, field: str) -> int | None:
    """Coerce an optional top-level POST body field to an int (``None`` stays).

    Rejects a bool (``True`` is not a ``limit``) and a non-integral float, so a
    malformed paging/ranking field is a ``400`` rather than a silent truncation.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer, got {value!r}") from exc


class QueryFilters(NamedTuple):
    """The Umbra-specific filters :func:`parse_query` extracts from a STAC query.

    Every field defaults to ``None`` so an empty query is ``QueryFilters()`` and
    a caller can read only the fields it threads through.
    """

    product_types: list[str] | None = None
    area: str | None = None
    polarizations: list[str] | None = None
    min_incidence: float | None = None
    max_incidence: float | None = None
    max_resolution: float | None = None


def parse_query(query: Any) -> QueryFilters:
    """Parse a STAC *Query* extension object into the index's filters.

    The Umbra index filters by fields that aren't STAC core properties. This
    maps the Query extension onto them, matching the property names the
    ``item_to_stac`` output already carries:

    - ``{"product_types": {"in": ["GEC", "SICD"]}}`` (or a bare list/string)
    - ``{"area": {"like": "Beet Piler"}}`` (``eq`` is treated the same; the
      index match is already a case-insensitive substring), or a bare string.
    - ``{"sar:polarizations": {"in": ["VV"]}}`` (or ``eq``, or a bare
      list/string) -- keep items exposing at least one of the polarizations.
    - ``{"view:incidence_angle": {"gte": 20, "lte": 40}}`` -- inclusive bounds
      on the view incidence angle (either or both operators).
    - ``{"sar:resolution": {"lte": 0.5}}`` (or a bare number) -- keep items
      whose range *and* azimuth resolution are both at most this fine (metres).

    Any other property, or an unsupported operator, is a :class:`ValueError` so
    a client's filter is never silently dropped (which would return items the
    client asked to exclude). Returns an empty :class:`QueryFilters` for an
    empty query.
    """
    if query is None:
        return QueryFilters()
    if not isinstance(query, dict):
        raise ValueError("query must be an object")

    supported = {
        "product_types",
        "area",
        "sar:polarizations",
        "view:incidence_angle",
        "sar:resolution",
    }
    unknown = sorted(set(query) - supported)
    if unknown:
        raise ValueError(
            f"unsupported query properties {unknown}; this API's query extension "
            f"covers {sorted(supported)}"
        )

    def _scalar(prop: str, spec: Any, ops: tuple[str, ...]) -> Any:
        # A bare value is shorthand for the natural operator; an object must use
        # exactly one of the operators we implement for that property.
        if isinstance(spec, dict):
            keys = list(spec)
            if len(keys) != 1 or keys[0] not in ops:
                raise ValueError(f"query.{prop} supports operators {list(ops)}, got {keys}")
            return spec[keys[0]]
        return spec

    def _numeric_range(prop: str, spec: Any) -> tuple[float | None, float | None]:
        # A range property accepts ``gte`` and/or ``lte`` in one object (a bare
        # value would be ``eq``, which the index has no exact-match filter for).
        if not isinstance(spec, dict):
            raise ValueError(f"query.{prop} needs a {{'gte': …, 'lte': …}} object")
        extra = sorted(set(spec) - {"gte", "lte"})
        if extra or not spec:
            raise ValueError(f"query.{prop} supports operators ['gte', 'lte'], got {sorted(spec)}")
        lo = _as_float(prop, spec["gte"]) if "gte" in spec else None
        hi = _as_float(prop, spec["lte"]) if "lte" in spec else None
        return lo, hi

    product_types = None
    if "product_types" in query:
        spec = _scalar("product_types", query["product_types"], ("in", "eq"))
        product_types = parse_product_types(spec)

    area = None
    if "area" in query:
        raw = _scalar("area", query["area"], ("like", "eq"))
        area = str(raw).strip() or None

    polarizations = None
    if "sar:polarizations" in query:
        spec = _scalar("sar:polarizations", query["sar:polarizations"], ("in", "eq"))
        polarizations = parse_polarizations(spec)

    min_incidence = max_incidence = None
    if "view:incidence_angle" in query:
        min_incidence, max_incidence = _numeric_range(
            "view:incidence_angle", query["view:incidence_angle"]
        )

    max_resolution = None
    if "sar:resolution" in query:
        spec = _scalar("sar:resolution", query["sar:resolution"], ("lte",))
        max_resolution = _as_float("sar:resolution", spec)

    return QueryFilters(
        product_types=product_types,
        area=area,
        polarizations=polarizations,
        min_incidence=min_incidence,
        max_incidence=max_incidence,
        max_resolution=max_resolution,
    )


# --------------------------------------------------------------------------
# STAC document builders (deterministic; unit-testable without a server)
# --------------------------------------------------------------------------


def _link(rel: str, href: str, *, type: str = "application/json", **extra: Any) -> dict[str, Any]:
    link = {"rel": rel, "href": href, "type": type}
    link.update(extra)
    return link


def landing_page(
    base_url: str,
    *,
    artifacts: bool = False,
    stack_execution: StackExecution | None = None,
    narrate: bool = False,
    narrate_policy: Mapping[str, Any] | None = None,
    mcp: bool = False,
    public: bool = False,
) -> dict[str, Any]:
    """The STAC API landing page (a STAC ``Catalog`` with conformance + links).

    When ``artifacts`` is true the returned links also advertise the on-demand
    render endpoints (``/artifacts/...``) so a client can discover them without
    reading the OpenAPI document.

    The ``stats`` link carries one thing the others cannot: which of its request
    options *this* instance can honour. ``stack_execution`` is the instance's
    :class:`StackExecution` (the eager default when omitted), and the link's
    :data:`STATS_CAPABILITY_FIELD` reports :func:`stats_capabilities` for it, so
    a client learns whether ``windowed`` (and, historically, ``speckle_filter``)
    is available by reading the landing page rather than by sending a request and
    parsing the ``400``.

    The ``narrate`` link does the same for the one endpoint that spends money:
    when ``narrate_policy`` is given (:func:`narrate_capabilities`) it rides under
    the same :data:`STATS_CAPABILITY_FIELD`, so a client reads an instance's spend
    caps and area bound before a ``403``/``429`` teaches them the hard way.
    """
    base = base_url.rstrip("/")
    geojson = "application/geo+json"
    links = [
        _link("self", f"{base}/"),
        _link("root", f"{base}/"),
        _link("conformance", f"{base}/conformance"),
        _link("data", f"{base}/collections"),
        _link("search", f"{base}/search", type=geojson, method="GET", title="STAC search"),
        _link("search", f"{base}/search", type=geojson, method="POST", title="STAC search"),
    ]
    if mcp:
        links.append(
            _link(
                "mcp",
                f"{base}/mcp",
                type="application/json",
                method="POST",
                title="Streamable HTTP MCP (same catalog, agent front door)",
            )
        )
    links += [
        _link(
            "sites",
            f"{base}/sites",
            type="application/json",
            method="GET",
            title="Rank the archive's most repeat-imaged sites (discovery before analysis)",
        ),
        _link(
            "sites",
            f"{base}/sites",
            type="application/json",
            method="POST",
            title="Rank the archive's most repeat-imaged sites (GeoJSON body form)",
        ),
        _link(
            "service-desc",
            f"{base}/openapi.json",
            type="application/vnd.oai.openapi+json;version=3.0",
        ),
        _link("service-doc", f"{base}/docs", type="text/html"),
        _link(
            "child",
            f"{base}/collections/{COLLECTION_ID}",
            title="Umbra open data",
        ),
    ]
    if artifacts:
        png = "image/png"
        links += [
            _link(
                "quicklook",
                f"{base}/artifacts/quicklook/{{item_id}}.png",
                type=png,
                title="On-demand SAR quicklook (templated by item id)",
                templated=True,
            ),
            _link(
                "thumbnail",
                f"{base}/artifacts/thumbnail/{{item_id}}.png",
                type=png,
                title="Baked SAR quicklook thumbnail (templated by item id)",
                templated=True,
            ),
            _link(
                "change",
                f"{base}/artifacts/change",
                type=png,
                method="POST",
                title="On-demand 2-3 date change composite",
            ),
            _link(
                "timescan",
                f"{base}/artifacts/timescan",
                type=png,
                method="POST",
                title="On-demand temporal-statistics composite",
            ),
            _link(
                "swipe",
                f"{base}/artifacts/swipe",
                type="text/html",
                method="POST",
                title="On-demand before/after swipe map (interactive HTML)",
            ),
            _link(
                "stats",
                f"{base}/artifacts/stats",
                type="application/json",
                method="POST",
                title="On-demand change statistics over a site's passes (JSON)",
                **{STATS_CAPABILITY_FIELD: stats_capabilities(stack_execution)},
            ),
            _link(
                "provenance",
                f"{base}/artifacts/provenance",
                type="application/json",
                method="POST",
                title="Whether a selection is one measurement, before /artifacts/stats",
            ),
        ]
        # Advertised only when the instance opted in (a model key was configured):
        # the endpoint answers 501 otherwise, so a client should not find a link
        # to a door that is shut. A visitor discovers "this instance can explain
        # its changes" from the landing page rather than by spending a request.
        if narrate:
            narrate_extra: dict[str, Any] = {}
            if narrate_policy is not None:
                narrate_extra[STATS_CAPABILITY_FIELD] = dict(narrate_policy)
            links.append(
                _link(
                    "narrate",
                    f"{base}/artifacts/narrate",
                    type="application/json",
                    method="POST",
                    title=(
                        "Vision-language narration of the change between two passes "
                        "(a longer series is scanned for the pair worth reading)"
                    ),
                    **narrate_extra,
                )
            )
    description = (
        "A read-only STAC API over Umbra's open SAR archive, served by "
        "umbra-py from a local catalog index. Umbra publishes a static STAC "
        "catalog and no search API; this façade restores /search for the "
        f"standard STAC tooling. Data is {DATA_LICENSE}: {ATTRIBUTION}"
    )
    if public:
        description = f"{description} {COMMUNITY_INSTANCE_NOTE}"
    return {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": COLLECTION_ID,
        "title": "Umbra Open Data STAC API",
        "description": description,
        "conformsTo": list(CONFORMANCE_CLASSES),
        "links": links,
    }


def conformance() -> dict[str, Any]:
    """The ``/conformance`` response."""
    return {"conformsTo": list(CONFORMANCE_CLASSES)}


def health_document(*, backend: str, ready: bool, items: int | None = None) -> dict[str, Any]:
    """Build the ``/healthz`` liveness/readiness document.

    ``backend`` is ``"index"`` or ``"live"``; ``ready`` reports whether the
    search backend can currently answer queries (a local index is present, or
    live mode is configured); ``items`` is the indexed acquisition count when
    known. The document is deliberately tiny so a container ``HEALTHCHECK`` or a
    Kubernetes probe can poll it cheaply -- the endpoint itself always returns
    ``200`` once the HTTP server is up (liveness), and ``ready`` distinguishes a
    server that is still waiting on its first-boot index fetch (readiness).
    """
    doc: dict[str, Any] = {
        "status": "ok" if ready else "starting",
        "backend": backend,
        "ready": ready,
        "stac_version": STAC_VERSION,
    }
    if items is not None:
        doc["items"] = items
    return doc


def _temporal_interval(temporal: tuple[str | None, str | None] | None) -> list[list[str | None]]:
    start, end = temporal or (None, None)
    return [[start, end]]


def collection(
    base_url: str, *, temporal: tuple[str | None, str | None] | None = None
) -> dict[str, Any]:
    """The single ``umbra-open-data`` STAC Collection.

    ``temporal`` is the ``(start, end)`` ISO date span (typically from
    :meth:`CatalogIndex.stats`); a global spatial extent is used because the
    archive spans the whole Earth.
    """
    base = base_url.rstrip("/")
    return {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "id": COLLECTION_ID,
        "title": "Umbra Open Data",
        "description": (
            "Every acquisition in Umbra's open SAR data program: high-resolution "
            "X-band spotlight scenes published as GEC (analysis-ready GeoTIFF), "
            "CSI, SIDD, SICD and CPHD products. " + ATTRIBUTION
        ),
        "license": DATA_LICENSE,
        "keywords": ["sar", "umbra", "x-band", "open-data", "radar"],
        "providers": [
            {
                "name": "Umbra",
                "roles": ["producer", "licensor"],
                "url": "https://umbra.space/open-data/",
            },
            {
                "name": "umbra-py",
                "roles": ["processor", "host"],
                "url": "https://github.com/reesehammer/umbra-py",
            },
        ],
        "extent": {
            "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {"interval": _temporal_interval(temporal)},
        },
        "summaries": {
            "sar:product_type": list(PRODUCT_TYPE_EXPLANATIONS.keys()),
        },
        "links": [
            _link("self", f"{base}/collections/{COLLECTION_ID}"),
            _link("root", f"{base}/"),
            _link("parent", f"{base}/"),
            _link(
                "items",
                f"{base}/collections/{COLLECTION_ID}/items",
                type="application/geo+json",
            ),
        ],
    }


def item_to_stac(item: UmbraItem, base_url: str) -> dict[str, Any]:
    """Render one :class:`UmbraItem` as a STAC API ``Feature``.

    Starts from the item's original STAC JSON (``item.raw``) so nothing is lost,
    then normalises it for the API: stamps the collection, and rewrites the
    ``links`` to point at this server (self / root / parent / collection) rather
    than the static-catalog relative paths the bucket ships.
    """
    base = base_url.rstrip("/")
    feature = dict(item.raw) if item.raw else {}
    feature.setdefault("type", "Feature")
    feature.setdefault("stac_version", STAC_VERSION)
    feature["id"] = item.id
    feature["collection"] = COLLECTION_ID
    feature.setdefault("geometry", item.geometry)
    if item.bbox is not None:
        feature["bbox"] = list(item.bbox)
    feature.setdefault("properties", dict(item.properties))
    # Surface the baked reverse-geocoded label (from `umbra index bake`) as a
    # namespaced property so a STAC client shows a real place name, not just
    # the task codename. Only when the index resolved one and the raw item
    # didn't already carry it.
    if item.place and "umbra:place" not in feature["properties"]:
        feature["properties"]["umbra:place"] = item.place
    feature.setdefault("assets", dict(item.assets))

    item_path = f"{base}/collections/{COLLECTION_ID}/items/{item.id}"
    feature["links"] = [
        _link("self", item_path, type="application/geo+json"),
        _link("root", f"{base}/"),
        _link("parent", f"{base}/collections/{COLLECTION_ID}"),
        _link("collection", f"{base}/collections/{COLLECTION_ID}"),
    ]
    return feature


def search_result(
    items: list[UmbraItem],
    base_url: str,
    *,
    returned: int | None = None,
    next_href: str | None = None,
    self_href: str | None = None,
) -> dict[str, Any]:
    """Wrap items in a STAC ``FeatureCollection`` (the ``/search`` response).

    Adds the STAC ``context`` block (returned/limit counts) and a ``next`` link
    when the query paginated past this page.
    """
    base = base_url.rstrip("/")
    features = [item_to_stac(it, base_url) for it in items]
    links = [_link("root", f"{base}/")]
    if self_href:
        links.append(_link("self", self_href, type="application/geo+json"))
    if next_href:
        links.append(_link("next", next_href, type="application/geo+json", method="GET"))
    return {
        "type": "FeatureCollection",
        "stac_version": STAC_VERSION,
        "context": {
            "returned": returned if returned is not None else len(features),
            "limit": len(features),
        },
        "features": features,
        "links": links,
    }


# --------------------------------------------------------------------------
# Search execution over a backend (CatalogIndex or live UmbraCatalog)
# --------------------------------------------------------------------------


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def run_search(
    source: Any,
    *,
    bbox: BBox | None = None,
    intersects: Geometry | None = None,
    start: date | None = None,
    end: date | None = None,
    ids: list[str] | None = None,
    product_types: list[str] | None = None,
    area: str | None = None,
    fuzzy: bool = False,
    polarizations: list[str] | None = None,
    min_incidence: float | None = None,
    max_incidence: float | None = None,
    max_resolution: float | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[UmbraItem], bool]:
    """Execute a paged search against a ``source`` (anything with ``.search``).

    Returns ``(page_items, has_next)``. ``ids``, when given, filters by STAC
    item id in this layer (the index search filters by bbox/date/area, not id).
    ``product_types``, ``area`` and ``fuzzy`` are the Query-extension filters,
    and ``polarizations`` / ``min_incidence`` / ``max_incidence`` /
    ``max_resolution`` are the SAR acquisition-property filters -- all pushed
    down to the backend's ``search`` (both :class:`CatalogIndex` and the live
    :class:`~umbra_py.catalog.UmbraCatalog` accept them). Paging is
    deterministic offset paging over the source's stable ordering: we request
    one extra item to decide whether a ``next`` link is warranted.
    """
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    # Bound the work when we can. With an id filter we can't cap at the source
    # (an id can appear anywhere in the ordering), so scan and filter here.
    cap = None if ids else offset + limit + 1
    stream = source.search(
        bbox=bbox,
        intersects=intersects,
        start=start,
        end=end,
        product_types=product_types,
        area=area,
        fuzzy=fuzzy,
        polarizations=polarizations,
        min_incidence=min_incidence,
        max_incidence=max_incidence,
        max_resolution=max_resolution,
        limit=cap,
    )
    if ids:
        wanted = set(ids)
        stream = (it for it in stream if it.id in wanted)
    window = list(itertools.islice(stream, offset, offset + limit + 1))
    has_next = len(window) > limit
    return window[:limit], has_next


def get_one(source: Any, item_id: str) -> UmbraItem | None:
    """Fetch a single item by STAC id from a ``source``.

    Prefers a :class:`CatalogIndex`'s keyed :meth:`~CatalogIndex.get` (an
    ``idx_items_id``-backed point lookup) so ``GET .../items/{id}`` stays fast
    as the snapshot grows; a live :class:`~umbra_py.catalog.UmbraCatalog`, which
    only lists, falls back to an id-filtered :func:`run_search`.
    """
    if isinstance(source, CatalogIndex):
        return source.get(item_id)
    page, _ = run_search(source, ids=[item_id], limit=1)
    return page[0] if page else None


def _clamp_top(top: int | None) -> int:
    if top is None:
        return SITES_DEFAULT_TOP
    return max(1, min(int(top), SITES_MAX_TOP))


def run_sites(
    source: Any,
    *,
    bbox: BBox | None = None,
    intersects: Geometry | None = None,
    start: date | None = None,
    end: date | None = None,
    product_types: list[str] | None = None,
    area: str | None = None,
    fuzzy: bool = False,
    polarizations: list[str] | None = None,
    min_incidence: float | None = None,
    max_incidence: float | None = None,
    max_resolution: float | None = None,
    limit: int = SITES_POOL_LIMIT,
    top: int = SITES_DEFAULT_TOP,
    min_passes: int = SITES_MIN_PASSES,
    rank_by: str = "passes",
    active_since: DateLike = None,
    active_before: DateLike = None,
    first_since: DateLike = None,
    first_before: DateLike = None,
    max_revisit_days: float | None = None,
    median_revisit_days: float | None = None,
    min_span_days: float | None = None,
    max_span_days: float | None = None,
) -> list[SiteCoverage]:
    """Rank the most repeat-imaged sites in a filtered pool of the archive.

    The discovery answer *before* the analysis endpoints: ``/artifacts/change`` /
    ``timescan`` / ``stats`` all measure *what* changed at a site, but each
    assumes the caller already knows *which* site's passes to send. Umbra files
    every pass of an area under one task, so a site's coverage is how many dated
    passes share its task; the ones with the most are exactly where a time series
    exists to analyse.

    On the normal (index) backend the ranking is **whole-archive**: a site's
    depth is one ``GROUP BY task`` :meth:`CatalogIndex.rank_sites` answers over the
    index's entire contents, so a deeply-imaged site is ranked by all its passes
    rather than by the arbitrary window a pool cap admits -- the drop-in
    ``STRATEGY.md`` §8 names, so ``GET /sites`` no longer under-counts a deep site
    whose passes fall outside the first ``limit`` rows. ``limit`` sizes only the
    live-backend pool: a live :class:`~umbra_py.catalog.UmbraCatalog` (``umbra serve
    --live``) has no index to group over, so it re-lists a single capped
    ``source.search`` and ranks that (a bigger pool finds more sites and deeper
    series but scans more of the archive; ``area`` narrows it instead). The
    SAR/date/geometry filters are the same ones :func:`run_search` takes, pushed
    down to the backend either way.

    The ranking is :func:`~umbra_py.coverage.rank_site_coverage` (via
    :meth:`~umbra_py.CatalogIndex.rank_sites` on the index, which shares the same
    selector and summariser) -- the same one ``umbra sites``, the
    ``find_repeat_sites`` agent tool and the static showcase's featured gallery
    use, so no two surfaces disagree about what "most repeat-imaged" means. Pure
    ranking: no renderer and no model (``STRATEGY.md`` §7's determinism boundary
    applied to discovery).

    ``rank_by`` is one of :data:`umbra_py.coverage.SITE_RANKINGS`: the depth orders
    ``"passes"`` (raw pass count, the default) and ``"comparable"`` (the site's
    *analysable* depth -- the largest same-polarization dated subset a change verb
    can difference), or the temporal orders ``"recency"`` (newest dated pass first --
    the still-active monitoring/tasking target), ``"span"`` (longest observation
    baseline first -- the site watched long enough for slow change to show) and
    ``"cadence"`` (tightest *typical* revisit gap first -- the most-frequently-imaged
    site; the median gap, not the worst one), which order by the same figures
    ``active_since`` / ``active_before``, ``min_span`` / ``max_span`` and
    ``median_revisit`` filter on. It is forwarded unchanged to the index and
    pool rankers, so this endpoint orders sites exactly as ``umbra sites --rank-by``
    and the ``find_repeat_sites`` agent tool do.

    ``active_since`` keeps only sites still imaged *on or after* that date -- a
    recency filter on each site's newest pass (the discovery answer for "which
    repeat-imaged sites are still live monitoring targets?"). It is forwarded to the
    index (a ``MAX(acq_date)`` clause in the same ``GROUP BY``) and pool rankers
    unchanged, so this endpoint filters exactly as ``umbra sites --active-since``
    does, orthogonally to ``rank_by`` / ``min_passes`` and distinct from ``start`` /
    ``end`` (which bound the passes rather than select whole sites).

    ``active_before`` is the complement -- keep only sites whose newest pass is *on
    or before* that date (a dormant series) -- forwarded to the index (a twin
    ``MAX(acq_date) <= ?`` clause) and pool rankers unchanged, so with ``active_since``
    the two bound the site's latest pass to a window exactly as ``umbra sites
    --active-since --active-before`` does.

    ``first_since`` / ``first_before`` are the onset (first-seen) twins of the
    ``active_*`` pair -- they gate each site's *earliest* pass rather than its newest,
    selecting **newly-appeared** series (``first_since``) and **long-established** ones
    (``first_before``); set together they bound the onset to a window. Forwarded to the
    index (twin ``MIN(acq_date) >= ?`` / ``MIN(acq_date) <= ?`` clauses in the same
    ``GROUP BY``) and pool rankers unchanged, so this endpoint filters exactly as
    ``umbra sites --first-since`` / ``--first-before`` does, orthogonally to the
    ``active_*`` recency filters. ``None`` applies no onset filter.

    ``max_revisit_days`` keeps only sites revisited *at least this often* -- a cadence
    filter on each site's **worst-case** revisit gap (in days) -- forwarded to the
    index and pool rankers unchanged, so this endpoint filters exactly as ``umbra
    sites --max-revisit`` does: on the analysable series under ``rank_by="comparable"``,
    orthogonally to the recency filters. ``None`` applies no cadence filter.

    ``median_revisit_days`` is the *typical*-cadence twin of ``max_revisit_days`` --
    keep only sites whose **median** revisit gap (in days) is at most that -- forwarded
    to the index and pool rankers unchanged, so this endpoint filters exactly as
    ``umbra sites --median-revisit`` does: on the analysable series under
    ``rank_by="comparable"``, orthogonal to ``max_revisit_days`` (typical gap vs worst
    gap) and the recency filters. ``None`` applies no typical-cadence filter.

    ``min_span_days`` keeps only sites imaged over *at least this long* -- a baseline
    filter on each site's observation **span** (in days, first pass to last),
    forwarded to the index and pool rankers unchanged, so this endpoint filters
    exactly as ``umbra sites --min-span`` does: on the analysable series under
    ``rank_by="comparable"``, a different axis from ``max_revisit_days`` (cadence vs
    baseline) and orthogonal to the recency filters. ``None`` applies no span filter.

    ``max_span_days`` is the upper twin of ``min_span_days`` -- keep only sites imaged
    over *at most this long* (a short-lived series), forwarded to the index and pool
    rankers unchanged, so this endpoint filters exactly as ``umbra sites --max-span``
    does. Set with ``min_span_days`` the two bound each site's baseline to a window
    (``min_span_days <= span <= max_span_days``), as ``active_since`` / ``active_before``
    bound the newest pass. ``None`` applies no span ceiling.
    """
    from .coverage import (
        _check_max_revisit,
        _check_max_span,
        _check_median_revisit,
        _check_min_span,
        _check_ranking,
        rank_site_coverage,
    )

    _check_ranking(rank_by)
    _check_max_revisit(max_revisit_days)
    _check_median_revisit(median_revisit_days)
    _check_min_span(min_span_days)
    _check_max_span(max_span_days)
    top_n = _clamp_top(top)
    min_p = max(1, int(min_passes))

    if isinstance(source, CatalogIndex):
        return source.rank_sites(
            bbox=bbox,
            intersects=intersects,
            start=start,
            end=end,
            product_types=product_types,
            area=area,
            fuzzy=fuzzy,
            polarizations=polarizations,
            min_incidence=min_incidence,
            max_incidence=max_incidence,
            max_resolution=max_resolution,
            top=top_n,
            min_passes=min_p,
            rank_by=rank_by,
            active_since=active_since,
            active_before=active_before,
            first_since=first_since,
            first_before=first_before,
            max_revisit_days=max_revisit_days,
            median_revisit_days=median_revisit_days,
            min_span_days=min_span_days,
            max_span_days=max_span_days,
        )

    pool = list(
        source.search(
            bbox=bbox,
            intersects=intersects,
            start=start,
            end=end,
            product_types=product_types,
            area=area,
            fuzzy=fuzzy,
            polarizations=polarizations,
            min_incidence=min_incidence,
            max_incidence=max_incidence,
            max_resolution=max_resolution,
            limit=_clamp_limit(limit),
        )
    )
    return rank_site_coverage(
        pool,
        top=top_n,
        min_passes=min_p,
        rank_by=rank_by,
        active_since=active_since,
        active_before=active_before,
        first_since=first_since,
        first_before=first_before,
        max_revisit_days=max_revisit_days,
        median_revisit_days=median_revisit_days,
        min_span_days=min_span_days,
        max_span_days=max_span_days,
    )


def sites_result(
    sites: list[SiteCoverage],
    base_url: str,
    *,
    resolved_bbox: BBox | None = None,
    resolved_area: str | None = None,
    query: dict[str, Any] | None = None,
    self_href: str | None = None,
) -> dict[str, Any]:
    """Wrap ranked sites in the ``GET /sites`` response document.

    Each entry of ``sites`` is a :meth:`~umbra_py.coverage.SiteCoverage.to_dict`
    (``docs/schemas/site-coverage.schema.json`` -- the same shape ``umbra sites
    --json`` and the ``find_repeat_sites`` agent tool emit), so the HTTP surface
    reads the one contract the CLI and the agent tools already do. ``count``, the
    resolved geography (``resolved_bbox`` / ``resolved_area``) and the ``query``
    echo of the ranking-and-selection inputs
    (:func:`~umbra_py.coverage.site_query_echo`) sit beside them, so the answer is
    self-describing -- a caller reads how it was ranked and filtered from the
    response rather than from its own request -- with the CC-BY ``attribution`` the
    licence requires on every derived answer.
    """
    base = base_url.rstrip("/")
    links = [_link("root", f"{base}/")]
    if self_href:
        links.append(_link("self", self_href))
    return {
        "count": len(sites),
        "resolved_bbox": list(resolved_bbox) if resolved_bbox else None,
        "resolved_area": resolved_area,
        "query": query,
        "sites": [site.to_dict() for site in sites],
        "attribution": ATTRIBUTION,
        "links": links,
    }


def open_source(index_path: str | os.PathLike | None = None, *, live: bool = False) -> Any:
    """Open the search backend for the server.

    Index-first: opens the on-disk :class:`CatalogIndex` (default path unless
    ``index_path`` overrides it), raising a helpful error if none exists.
    ``live=True`` forces a live S3 walk instead -- correct but slow, so it is
    opt-in. A fresh backend is opened per request (SQLite connections are not
    shared across threads), so callers should close index sources they open.
    """
    if live:
        from .catalog import UmbraCatalog

        return UmbraCatalog()
    path = Path(index_path) if index_path is not None else default_index_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No local index at {path}. Fetch the published snapshot with "
            "'umbra index fetch', build one with 'umbra index build', or run "
            "'umbra serve --live' to walk S3 per request (slow)."
        )
    return CatalogIndex(path)


# --------------------------------------------------------------------------
# On-demand render artifacts (quicklook / change / timescan)
# --------------------------------------------------------------------------

#: Upper bound on acquisitions pulled into a single composite. A timescan's
#: statistics converge well before this, and it bounds per-request memory and
#: COG-streaming egress; a query resolving to more is evenly subsampled to it.
ARTIFACT_MAX_FRAMES = 60

#: Default downsample ceiling for artifact renders. Smaller than the library
#: default (2048) because these are interactive, streamed-in-the-request views.
ARTIFACT_MAX_SIZE = 1024


def default_artifact_cache_dir() -> Path:
    """Where rendered artifacts are cached (next to the index by default)."""
    return default_index_path().parent / "artifacts"


#: Dask schedulers ``POST /artifacts/stats`` may evaluate a lazy cube under.
#: ``"synchronous"`` runs the chunks on the calling thread -- the request's own
#: worker -- so a hosted instance's thread count stays whatever its ASGI server
#: was configured with; ``"threads"`` gives one render dask's default thread
#: pool, which is faster for a single request and multiplies under concurrent
#: ones. Deliberately not ``"processes"``: the chunks stream COG bytes through
#: GDAL handles that do not fork cleanly.
STACK_SCHEDULERS = ("synchronous", "threads")


@dataclass(frozen=True)
class StackExecution:
    """*How* ``POST /artifacts/stats`` builds its datacube -- an operator policy.

    The stats endpoint is the only one that stacks a whole series into memory,
    so it is the only one with a ceiling set by the number of passes rather than
    by one render. :func:`~umbra_py.load.to_stack`'s ``lazy=`` / ``chunk_size=``
    lift that ceiling, but turning them on inside a request handler is not the
    client's call to make: it needs the ``dask`` extra installed on *the server*
    and a decision about how many threads one request may spend. So it is
    configured once, per instance (``umbra serve --stack-lazy``), and never read
    off the request body.

    Because a lazy cube's numbers are identical to an eager one's -- only the
    peak memory differs -- this policy is deliberately **not** part of
    :func:`artifact_cache_key`: flipping it on an instance neither invalidates
    the artifact cache nor changes a single figure a client already fetched.

    Attributes
    ----------
    lazy:
        Defer each pass's read into a ``dask`` task, so the reduction walks the
        series a slice at a time instead of holding every pass at once. Needs
        the ``dask`` extra on the server; without it a stats request answers
        ``501`` naming the extra, exactly like a missing ``load``.
    chunk_size:
        Cut each pass into ``chunk_size``-square windows read independently, so
        one *scene* no longer has to fit either. Costs one range read per window
        instead of one per pass, which is why it is opt-in, and it requires
        ``lazy`` (an eager cube is read a slab at a time). It is also what makes
        a request's ``"windowed": true`` measurable: the windows a chunked cube
        is built in are the windows the reduction walks, so an instance without
        this refuses that option instead of estimating percentiles for nothing.
    scheduler:
        Which of :data:`STACK_SCHEDULERS` evaluates the chunks. Defaults to
        ``"synchronous"`` -- a request handler that quietly starts a thread pool
        per render is a worse surprise than a slower one.
    """

    lazy: bool = False
    chunk_size: int | None = None
    scheduler: str = "synchronous"

    def __post_init__(self) -> None:
        if self.chunk_size is not None:
            if not self.lazy:
                raise ValueError("chunk_size needs lazy=True; an eager cube is read a slab.")
            if int(self.chunk_size) < 1:
                raise ValueError(
                    f"chunk_size must be a positive pixel count; got {self.chunk_size!r}."
                )
        if self.scheduler not in STACK_SCHEDULERS:
            raise ValueError(
                f"scheduler must be one of {list(STACK_SCHEDULERS)}, got {self.scheduler!r}."
            )

    def describe(self) -> str:
        """One line an operator can read back at startup to confirm the policy."""
        if not self.lazy:
            return "eager (whole series in memory)"
        window = f"{self.chunk_size}px windows" if self.chunk_size else "one chunk per pass"
        return f"lazy ({window}, {self.scheduler} scheduler)"


#: The ``POST /artifacts/stats`` request options whose availability is a property
#: of the *instance* rather than of the request: they need the cube built a
#: particular way, and :class:`StackExecution` decides that once per server.
#: ``speckle_filter`` is now honoured whatever the policy (:func:`to_stack` reads
#: a halo per window), and stays here so one document answers "what can I ask
#: this server for?" for both stacking options rather than only the constrained
#: one.
STATS_INSTANCE_OPTIONS = ("windowed", "speckle_filter")

#: The landing-page link field that carries :func:`stats_capabilities`. Namespaced
#: like ``umbra:place`` on an item, because it is this façade's own vocabulary
#: rather than anything STAC defines.
STATS_CAPABILITY_FIELD = "umbra:options"


def stats_option_refusal(execution: StackExecution, option: str) -> str | None:
    """Why *this* instance cannot honour a stats option, or ``None`` if it can.

    The single source of truth for both halves of the answer: the renderer
    raises the string this returns (which the route maps to a ``400``), and
    :func:`stats_capabilities` puts the same string on the landing page. A
    client therefore reads the reason it *would* be refused for before spending
    a request, and the advertisement cannot drift from the refusal because
    there is only one of them.

    ``option`` must be one of :data:`STATS_INSTANCE_OPTIONS`; anything else is a
    programming error rather than a client's, so it raises instead of quietly
    reporting support.
    """
    if option not in STATS_INSTANCE_OPTIONS:
        raise ValueError(f"option must be one of {list(STATS_INSTANCE_OPTIONS)}, got {option!r}.")
    # A window-by-window reduction walks the cube's own chunks, so on an
    # instance that builds one chunk per pass (or none at all) it would estimate
    # the percentiles without holding any less -- a strictly worse answer.
    if option == "windowed" and not execution.chunk_size:
        return (
            "windowed measurement needs a chunked instance: this server stacks "
            f"{execution.describe()}, so measuring window by window would only "
            "estimate the percentiles without lowering the memory. Ask the operator "
            "for 'umbra serve --stack-lazy --stack-chunk-size N', or drop 'windowed'."
        )
    # ``speckle_filter`` has no instance condition left. It used to be refused on
    # a chunked instance, because a filter window centred near a chunk edge
    # averages cells the neighbouring chunk holds and "lee" read its speckle
    # parameter off the array it was handed. ``to_stack`` now reads each window
    # with a half-window halo and resolves that parameter once per pass, so a
    # chunked cube filters to the unchunked one's answer -- and the pair that was
    # unsatisfiable on every instance is satisfiable on the chunked one. It stays
    # in :data:`STATS_INSTANCE_OPTIONS` so the landing page keeps advertising
    # both stacking options in one place, now saying yes to this one everywhere.
    return None


def stats_capabilities(execution: StackExecution | None = None) -> dict[str, Any]:
    """What ``POST /artifacts/stats`` will accept on an instance, as a document.

    Each option in :data:`STATS_INSTANCE_OPTIONS` reports ``supported``, and an
    unsupported one carries the ``reason`` it would have been refused with (the
    same string, from :func:`stats_option_refusal`) -- so a client picks options
    that work before spending a request instead of discovering them by reading a
    ``400``. ``stacking`` is the operator-facing policy line the CLI echoes at
    startup, so a client debugging a refusal sees the shape of the instance
    rather than only the verdict.

    Today only ``windowed`` has an instance condition: it needs the cube built in
    windows, so it is refused on a server without ``--stack-chunk-size``.
    ``speckle_filter`` reports supported everywhere, and the two are no longer
    mutually exclusive -- a chunked instance answers both, which is the
    combination that measures a cube too large to hold with the speckle averaged
    out of it.
    """
    execution = execution or StackExecution()
    capabilities: dict[str, Any] = {"stacking": execution.describe()}
    for option in STATS_INSTANCE_OPTIONS:
        refusal = stats_option_refusal(execution, option)
        entry: dict[str, Any] = {"supported": refusal is None}
        if refusal is not None:
            entry["reason"] = refusal
        capabilities[option] = entry
    return capabilities


@dataclass(frozen=True)
class Renderers:
    """The render functions the artifact endpoints call, as opaque bytes.

    Injecting this (rather than importing :mod:`umbra_py.viz` directly in the
    routes) is what keeps the endpoints unit-testable in the core install: a
    test passes fakes that return fixed bytes with no network and no ``viz`` /
    ``load`` extra, while :func:`default_renderers` wires the real,
    lazily-imported implementations. Each callable takes the resolved items and
    a normalised options mapping (``asset`` / ``max_size`` / ``db``, plus the
    stacking options for ``stats``) and returns the artifact's bytes.
    """

    quicklook: Callable[[UmbraItem, Mapping[str, Any]], bytes]
    change: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]
    timescan: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]
    #: Unlike the three PNG compositors, ``swipe`` returns a self-contained HTML
    #: page (:func:`viz.swipe_map`), so its bytes are UTF-8 HTML, not a PNG.
    swipe: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]
    #: The one artifact that is not an image: the datacube reduction
    #: (:func:`~umbra_py.load.stack_stats`) serialised as UTF-8 JSON.
    stats: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]
    #: The one artifact that calls a **model**: a vision-language narration of the
    #: change between two passes (:func:`~umbra_py.narrate.narrate`), serialised as
    #: UTF-8 JSON. ``None`` unless the instance was started with a narrator
    #: (``umbra serve --narrate`` + a model API key), because it is the one
    #: renderer that spends money per call -- so it is opt-in, and a route that
    #: finds it ``None`` answers ``501`` rather than rendering.
    narrate: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes] | None = None


def _stack_scheduler(execution: StackExecution) -> Any:
    """The dask-scheduler context a lazy stats render is evaluated inside.

    An eager render computes nothing deferred, so it gets a no-op context and
    never imports ``dask``. A lazy one pins the scheduler for the duration of
    *this* render only (``dask.config.set`` is context-local), so one instance's
    policy cannot leak into another thread's render or into a caller's process.
    The import goes through :mod:`umbra_py.load`'s own gate, so a server started
    with ``--stack-lazy`` but without the extra fails with the same
    ``MissingDependencyError`` the route already maps to ``501``.
    """
    if not execution.lazy:
        return nullcontext()
    from .load import _require_dask

    dask, _ = _require_dask()
    return dask.config.set(scheduler=execution.scheduler)


def _png_bytes(image: Any) -> bytes:
    """Encode a ``PIL.Image`` to PNG bytes."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def default_renderers(
    stack_execution: StackExecution | None = None,
    *,
    narrator: Any | None = None,
) -> Renderers:
    """The production renderers, backed by :mod:`umbra_py.viz` (``viz`` extra).

    Imports are deferred to call time so building the app -- and importing this
    module -- never needs the heavy raster stack; only an actual render request
    pulls it in (and a missing extra surfaces as a clean error the route maps to
    HTTP 501).

    ``stack_execution`` is the instance-wide policy for how the one non-picture
    renderer builds its datacube (see :class:`StackExecution`); it defaults to
    the eager read every hosted instance has had until now.

    ``narrator`` is the injected model boundary
    (:data:`umbra_py.narrate.Narrator`) for the one renderer that calls a model.
    It defaults to ``None``, in which case :attr:`Renderers.narrate` stays
    ``None`` and the narrate endpoint answers ``501`` -- narration is opt-in
    because it is the only renderer that spends money. When present, the same
    ``narrator`` is reused across requests, so one instance holds one key and one
    model choice; :func:`~umbra_py.narrate.default_narrator` is what
    ``umbra serve --narrate`` builds from the operator's environment.
    """
    execution = stack_execution or StackExecution()

    def quicklook(item: UmbraItem, opts: Mapping[str, Any]) -> bytes:
        from . import viz

        image = viz.quicklook(item, asset=opts["asset"], max_size=opts["max_size"], db=opts["db"])
        return _png_bytes(image)

    def change(items: Sequence[UmbraItem], opts: Mapping[str, Any]) -> bytes:
        from . import viz

        image = viz.change_composite(
            list(items), asset=opts["asset"], max_size=opts["max_size"], db=opts["db"]
        )
        return _png_bytes(image)

    def timescan(items: Sequence[UmbraItem], opts: Mapping[str, Any]) -> bytes:
        from . import viz

        image = viz.timescan_composite(
            list(items), asset=opts["asset"], max_size=opts["max_size"], db=opts["db"]
        )
        return _png_bytes(image)

    def swipe(items: Sequence[UmbraItem], opts: Mapping[str, Any]) -> bytes:
        from . import viz

        before, after = items[0], items[1]
        m = viz.swipe_map(
            before, after, asset=opts["asset"], max_size=opts["max_size"], db=opts["db"]
        )
        return m.get_root().render().encode("utf-8")

    def stats(items: Sequence[UmbraItem], opts: Mapping[str, Any]) -> bytes:
        # The two options whose availability is the instance's rather than the
        # request's (see :func:`stats_option_refusal`, which is also what the
        # landing page advertises, so a refusal here is one a client could have
        # read ahead of time). Both land before the import, so an option this
        # instance cannot honour costs a ``400`` and not the ``load`` extra.
        for option in STATS_INSTANCE_OPTIONS:
            if opts[option]:
                refusal = stats_option_refusal(execution, option)
                if refusal is not None:
                    raise ValueError(refusal)
        # The one renderer behind the ``load`` extra rather than ``viz``: it
        # co-registers the passes into a datacube and reduces it to numbers.
        from .load import stack_stats, to_stack

        clip: BBox | None = tuple(opts["clip_bbox"]) if opts["clip_bbox"] else None
        # ``lazy``/``chunk_size`` come from the instance policy, never the body:
        # they change the memory the render costs the *server*, not the answer.
        # The whole build+reduce runs inside the chosen scheduler because it is
        # ``stack_stats`` -- reading a slice at a time -- that computes chunks.
        with _stack_scheduler(execution):
            cube = to_stack(
                list(items),
                asset=opts["asset"],
                bbox=clip,
                max_size=opts["max_size"],
                db=opts["db"],
                extent=opts["extent"],
                crs=opts["crs"],
                lazy=execution.lazy,
                chunk_size=execution.chunk_size,
                # Unlike ``lazy``/``chunk_size`` these are the client's: they
                # change what the cells *are*, and the cube records them so the
                # reduction's caveats say the resolution the numbers cost.
                speckle_filter=opts["speckle_filter"],
                speckle_window=opts["speckle_window"] or SPECKLE_WINDOW_DEFAULT,
            )
            payload = stack_stats(
                cube,
                change_threshold_db=opts["change_threshold_db"],
                blocks=opts["blocks"],
                block_series=opts["block_series"],
                # Unlike ``lazy``/``chunk_size`` this one *is* the client's, and
                # is in the cache key: it moves the percentiles it estimates.
                windowed=opts["windowed"],
            )
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def narrate(items: Sequence[UmbraItem], opts: Mapping[str, Any]) -> bytes:
        # The one renderer that calls a model. It is only wired when a narrator
        # was injected (see below), so reaching here means the instance opted in.
        from .load import best_change_interval
        from .narrate import narrate as narrate_change
        from .narrate import render_change_png

        frames = list(items)
        selection: dict[str, Any] | None = None
        # The "scan many, narrate two" half: a series longer than a composite can
        # encode is reduced to the pair whose change stands furthest clear of the
        # speckle floor (deterministically -- the number picks the frames, not the
        # model), and that pair is what the model reads.
        if len(frames) > 3:
            picked = best_change_interval(
                frames,
                asset=opts["asset"],
                max_size=opts["max_size"],
                change_threshold_db=opts["change_threshold_db"],
            )
            if picked is None:
                raise ValueError(
                    "Could not find a comparable pair to narrate in the series "
                    "(fewer than two datable passes)."
                )
            frames = picked["pair"]
            selection = picked["selection"]

        def render(its: Sequence[UmbraItem]) -> tuple[bytes, Any]:
            return render_change_png(
                its,
                asset=opts["asset"],
                max_size=opts["max_size"],
                db=opts["db"],
                grid=opts["grid"],
                change_threshold_db=opts["change_threshold_db"],
            )

        narration = narrate_change(
            frames,
            narrator=narrator,
            render=render,
            asset=opts["asset"],
            change_threshold_db=opts["change_threshold_db"],
        )
        payload = narration.to_dict()
        # Say which two passes were narrated and why, when they were chosen from a
        # longer series -- so the answer carries the selection, not just the read.
        if selection is not None:
            payload["selected_interval"] = selection
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    return Renderers(
        quicklook=quicklook,
        change=change,
        timescan=timescan,
        swipe=swipe,
        stats=stats,
        narrate=narrate if narrator is not None else None,
    )


def artifact_options(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise the render options a request carries into a stable mapping.

    Deterministic and offline: it is part of the cache key, so a test can assert
    two requests hash the same. ``asset`` defaults to the detected amplitude
    GeoTIFF, ``max_size`` to :data:`ARTIFACT_MAX_SIZE`, ``db`` off.
    """
    body = body or {}
    return {
        "asset": str(body.get("asset") or "GEC"),
        "max_size": max(64, min(int(body.get("max_size") or ARTIFACT_MAX_SIZE), 8192)),
        "db": bool(body.get("db", False)),
    }


#: Ceiling on the ``blocks`` grid a stats request may ask for. A breakdown is a
#: payload per block (N x N of them), so this bounds the response, not the maths.
STATS_MAX_BLOCKS = 16


def stats_options(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise the options a ``POST /artifacts/stats`` request carries.

    Extends :func:`artifact_options` with what the datacube reduction needs on
    top of a render (``extent`` / ``crs`` / ``clip_bbox`` / ``blocks`` /
    ``block_series`` / ``windowed`` / ``change_threshold_db``). Two defaults
    deliberately differ from the picture
    endpoints, matching the ``stack_stats`` agent tool: the shared grid is the
    site's **UTM zone**, so a cell count is an area and ``changed_area_km2``
    means something, and values are **decibels**, the scale on which a ratio of
    backscatter is a difference. Pass ``"crs": null`` for a lon/lat grid (and
    accept ``None`` areas rather than wrong ones).

    ``windowed`` and ``speckle_filter`` are here — in the request options the
    cache key hashes — rather than in the instance's :class:`StackExecution`,
    because they are the stacking choices that *move a number*. ``windowed``
    trades each pass's exact median/p5/p95 for histogram estimates in exchange
    for never holding a whole slice; ``speckle_filter`` averages the interference
    pattern down before anything is measured, which is the correction with the
    largest effect on a per-cell decibel delta and the one that spends resolution
    to get it. A cached artifact whose numbers depended on a server flag nobody
    could see is the failure mode that decides both; asking for either is asking
    for a different artifact.

    ``speckle_window`` is normalised to ``None`` when no filter was asked for, so
    two unfiltered requests that differ only in a window nobody applied are one
    artifact rather than two cache entries.

    Raises ``ValueError`` for an unknown ``extent``, a non-positive threshold, a
    ``block_series`` asked for without a ``blocks`` grid to hang it on, and an
    unknown ``speckle_filter`` or a window that cannot be centred — each of which
    the route maps to a ``400``. ``windowed`` together with ``speckle_filter``
    used to be refused here as unsatisfiable on *every* instance; it no longer is
    (:func:`~umbra_py.load.to_stack` reads a halo per window, so a chunked build
    filters too), and the pair is now exactly as available as ``windowed`` alone.
    Whether a particular instance can honour that is not knowable here, so it
    stays in the renderer (:func:`stats_option_refusal`).
    """
    body = body or {}
    # ``db`` defaults to True here (radiometric scale), unlike the composites.
    options = artifact_options({**body, "db": body.get("db", True)})

    extent = str(body.get("extent") or "intersection").lower()
    if extent not in STACK_EXTENTS:
        raise ValueError(f"extent must be one of {list(STACK_EXTENTS)}, got {extent!r}.")

    # ``crs`` is absent -> the UTM default; explicitly null -> the lon/lat grid.
    crs = body["crs"] if "crs" in body else STACK_AUTO_CRS
    threshold = float(body.get("change_threshold_db") or 3.0)
    if threshold <= 0:
        raise ValueError(f"change_threshold_db must be > 0, got {threshold}.")

    # Distinct from the request's ``bbox``, which selects *which* acquisitions
    # are stacked; this one clips the cube to a sub-area inside them.
    clip = parse_bbox(body.get("clip_bbox"))

    options.update(
        extent=extent,
        crs=str(crs) if crs else None,
        clip_bbox=list(clip) if clip else None,
        blocks=max(0, min(int(body.get("blocks") or 0), STATS_MAX_BLOCKS)),
        block_series=bool(body.get("block_series", False)),
        windowed=bool(body.get("windowed", False)),
        change_threshold_db=threshold,
        **_speckle_options(body),
    )
    if options["block_series"] and not options["blocks"]:
        raise ValueError("block_series needs a blocks grid; send blocks: N as well.")
    return options


def _speckle_options(body: Mapping[str, Any]) -> dict[str, Any]:
    """The speckle-filtering half of :func:`stats_options`, validated at the request.

    Checked here rather than inside the render for the same reason
    :func:`~umbra_py.load._resolve_speckle` checks at the call: a misspelt filter
    or an even window is the client's mistake, and it should cost a ``400`` with
    the name in it rather than an error raised from somewhere inside a datacube
    build. The window is dropped entirely when no filter runs, so it never splits
    the cache for an artifact it had no effect on.
    """
    from .convert import _check_speckle_window

    requested = body.get("speckle_filter")
    name = str(requested).lower() if requested else None
    if name is not None and name not in SPECKLE_FILTERS:
        raise ValueError(
            f"speckle_filter must be one of {list(SPECKLE_FILTERS)}, got {requested!r}."
        )
    window = (
        _check_speckle_window(int(body.get("speckle_window") or SPECKLE_WINDOW_DEFAULT))
        if name is not None
        else None
    )
    return {"speckle_filter": name, "speckle_window": window}


#: Ceiling on the ``grid`` a narration request may ask for -- the coarse dB grid
#: the model is grounded in. A cell is a compass-located number the narration
#: cites, so this bounds how fine that grounding is, not the maths.
NARRATE_MAX_GRID = 12


def narrate_options(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise the options a ``POST /artifacts/narrate`` request carries.

    Extends :func:`artifact_options` (``asset`` / ``max_size`` / ``db``) with
    the two the change grid the narration is grounded in needs:
    ``change_threshold_db`` (the decibel move a cell must clear to count as
    changed) and ``grid`` (the coarse N x N grounding grid). The model is *not*
    a request option: it is the instance's, chosen once by the operator who
    holds the key, so a client cannot make one instance spend on another's
    model. Part of the cache key, so two identical narration requests are one
    artifact (and one model call).

    Raises ``ValueError`` for a non-positive threshold or an out-of-range grid,
    which the route maps to ``400``.
    """
    body = body or {}
    options = artifact_options(body)
    threshold = float(body.get("change_threshold_db") or 3.0)
    if threshold <= 0:
        raise ValueError(f"change_threshold_db must be > 0, got {threshold}.")
    grid = int(body.get("grid") or 6)
    if not 1 <= grid <= NARRATE_MAX_GRID:
        raise ValueError(f"grid must be between 1 and {NARRATE_MAX_GRID}, got {grid}.")
    options.update(change_threshold_db=threshold, grid=grid)
    return options


class NarrationBudget:
    """A per-day cap on how many *live* narrations an instance will spend.

    A narration is a paid model call, so an endpoint open to the internet is an
    open wallet. This caps the calls that actually reach the model -- a cache hit
    spends nothing and never consults it -- and resets at UTC midnight.
    ``limit=None`` is unlimited, which is the default because the cap is opt-in
    like the endpoint itself (``umbra serve --narrate-daily-limit N`` sets it).

    Thread-safe: the server answers requests on a pool, so :meth:`reserve` takes
    a lock to make "check the day, check the count, spend one" atomic. It counts
    *attempts* rather than successes, which is the conservative choice -- a model
    call that then fails still cost something, and a budget that only counted
    successes could be spun against a failing model without bound.
    """

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._day: date | None = None
        self._spent = 0

    def reserve(self) -> bool:
        """Claim one narration for today; ``False`` if today's cap is reached."""
        if self._limit is None:
            return True
        today = datetime.now(tz=timezone.utc).date()
        with self._lock:
            if today != self._day:
                self._day, self._spent = today, 0
            if self._spent >= self._limit:
                return False
            self._spent += 1
            return True

    def remaining(self) -> int | None:
        """How many narrations today's cap still allows (``None`` if unlimited)."""
        if self._limit is None:
            return None
        today = datetime.now(tz=timezone.utc).date()
        with self._lock:
            if today != self._day:
                return self._limit
            return max(0, self._limit - self._spent)


def client_identity(request: Any) -> str:
    """A stable, low-cardinality key for the per-client narration cap.

    A ``Bearer`` token in the ``Authorization`` header identifies a caller more
    precisely than an address does, so it wins when present -- it is hashed
    rather than kept, since the budget only needs to tell clients apart, not hold
    their secrets. Otherwise the immediate peer address (``request.client.host``)
    is the key.

    That address is the *socket* peer, so behind a reverse proxy every client
    reads as the proxy unless the proxy is trusted to set a forwarded-for header
    and uvicorn is run with ``--proxy-headers``. That is the operator's decision,
    not this function's: honouring an ``X-Forwarded-For`` that an untrusted client
    can forge would make the per-client cap trivially evadable, which is worse
    than a proxy that must be configured to be believed.
    """
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        token = token.strip()
        if scheme.lower() == "bearer" and token:
            return f"token:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return f"ip:{host or 'unknown'}"


class ClientNarrationBudget:
    """A per-client daily cap on *live* narrations, layered under the global one.

    :class:`NarrationBudget` protects the operator's wallet in aggregate; this
    protects it from a *single* client, so one caller cannot spend the whole
    day's budget in a burst -- the hardening a public, unauthenticated instance
    needs on top of the global cap. It is keyed by :func:`client_identity` (a
    bearer token, else the peer address), counts only calls that reach the model
    (a cache hit never consults it), and resets at UTC midnight like the global
    cap. ``limit=None`` is unlimited -- the default, since like the endpoint
    itself the per-client cap is opt-in (``umbra serve --narrate-client-limit N``).

    Thread-safe (one lock over "roll the day, read the count, spend one"), and it
    counts *attempts* for the reason the global budget does: a call that then
    fails still cost something. The daily reset also bounds the tracking dict,
    which is dropped whenever the day rolls over.
    """

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._day: date | None = None
        self._counts: dict[str, int] = {}

    def _roll(self, today: date) -> None:
        if today != self._day:
            self._day, self._counts = today, {}

    def reserve(self, client: str) -> bool:
        """Claim one narration for ``client`` today; ``False`` at their cap."""
        if self._limit is None:
            return True
        today = datetime.now(tz=timezone.utc).date()
        with self._lock:
            self._roll(today)
            spent = self._counts.get(client, 0)
            if spent >= self._limit:
                return False
            self._counts[client] = spent + 1
            return True

    def remaining(self, client: str) -> int | None:
        """How many narrations ``client``'s cap still allows (``None`` if unlimited)."""
        if self._limit is None:
            return None
        today = datetime.now(tz=timezone.utc).date()
        with self._lock:
            self._roll(today)
            return max(0, self._limit - self._counts.get(client, 0))


def public_secret_names() -> list[str]:
    """Env vars a public instance is holding that it must not.

    Empty means the process is safe to expose. Checked at ``serve()`` startup
    for ``public=True``, not in :func:`build_app`, so tests can construct a
    public app on a developer machine that happens to have a model key.
    """
    return [name for name in PUBLIC_SECRET_ENV if os.environ.get(name)]


class RateLimiter:
    """Sliding-window per-client request cap (stdlib, no extra).

    A public unauthenticated instance is a shared SQLite reader plus, when MCP
    is mounted, the render tools that stream Umbra COGs. ``limit`` requests per
    ``window_s`` seconds per :func:`client_identity` is the cheap abuse brake.
    ``max_clients`` bounds the tracking dict so a rotating-identity adversary
    cannot grow it without bound (evict the least-recently-seen).
    """

    def __init__(
        self,
        limit: int,
        window_s: float = PUBLIC_RATE_WINDOW_S,
        *,
        max_clients: int = 10_000,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("rate limit must be >= 1 (pass None/0 to disable)")
        self.limit = limit
        self.window_s = window_s
        self.max_clients = max_clients
        self._time = time_fn
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def hit(self, client: str) -> tuple[bool, int, int]:
        """Record one request.

        Returns ``(allowed, remaining, retry_after_s)``. ``retry_after_s`` is
        ``0`` when the request is allowed.
        """
        now = self._time()
        cutoff = now - self.window_s
        with self._lock:
            q = self._hits.get(client)
            if q is None:
                q = deque()
                self._hits[client] = q
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                retry = max(1, int(q[0] + self.window_s - now + 0.999))
                return False, 0, retry
            q.append(now)
            if len(self._hits) > self.max_clients:
                oldest = min(
                    (k for k in self._hits if k != client),
                    key=lambda k: self._hits[k][-1] if self._hits[k] else 0,
                    default=None,
                )
                if oldest is not None:
                    del self._hits[oldest]
            return True, self.limit - len(q), 0


@dataclass(frozen=True)
class NarrationAllowlist:
    """Which acquisitions a public narration endpoint will spend a model call on.

    The narrate endpoint is an *unauthenticated proxy over the operator's model
    budget*, so an open instance wants it bounded to the archive its showcase
    actually surfaces rather than pointable at arbitrary scenes. ``bbox`` is that
    bound: a ``(min_lon, min_lat, max_lon, max_lat)`` rectangle every narrated
    frame's footprint centroid must fall inside. ``bbox=None`` (the default) is
    no bound, since -- like the endpoint -- the allowlist is opt-in
    (``umbra serve --narrate-allow-bbox min_lon,min_lat,max_lon,max_lat``).

    The centroid, not the footprint, is the test on purpose: a scene whose large
    footprint merely clips the corner of the allowed area is not *in* it, and a
    frame whose footprint is unknown cannot be shown to be inside, so it is
    refused -- an open wallet fails closed.
    """

    bbox: BBox | None = None

    def permits(self, item: UmbraItem) -> bool:
        """Whether ``item`` sits inside the allowed area (always, if unbounded)."""
        if self.bbox is None:
            return True
        if item.bbox is None:
            return False
        cx = (item.bbox[0] + item.bbox[2]) / 2.0
        cy = (item.bbox[1] + item.bbox[3]) / 2.0
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return min_lon <= cx <= max_lon and min_lat <= cy <= max_lat

    def disallowed(self, frames: Sequence[UmbraItem]) -> UmbraItem | None:
        """The first frame outside the allowed area, or ``None`` if all pass."""
        if self.bbox is None:
            return None
        return next((it for it in frames if not self.permits(it)), None)


def narrate_capabilities(
    allowlist: NarrationAllowlist | None = None,
    *,
    daily_limit: int | None = None,
    client_limit: int | None = None,
) -> dict[str, Any]:
    """The narrate endpoint's spend policy, for the landing page to advertise.

    Mirrors :func:`stats_capabilities`: the ``narrate`` link carries this under
    :data:`STATS_CAPABILITY_FIELD` so a client discovers an instance's spend caps
    and area bound by reading ``/`` rather than by sending a request that a
    ``403``/``429`` then refuses. Every field is a *policy* the operator set, not
    a request option -- the model, its key and these bounds are the instance's,
    never a client's to send.
    """
    allowed = allowlist.bbox if allowlist is not None else None
    return {
        "allowed_bbox": list(allowed) if allowed is not None else None,
        "daily_limit": daily_limit,
        "client_daily_limit": client_limit,
    }


def artifact_cache_key(kind: str, item_ids: Sequence[str], options: Mapping[str, Any]) -> str:
    """A stable content hash for a render request.

    Pure and order-sensitive on ``item_ids`` (a change composite is *not* the
    same artifact with its frames reversed), so the cache never confuses two
    distinct renders. Options are hashed order-independently.
    """
    payload = {
        "kind": kind,
        "items": list(item_ids),
        "options": {k: options[k] for k in sorted(options)},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_items(
    source: Any,
    *,
    bbox: BBox | None = None,
    start: date | None = None,
    end: date | None = None,
    ids: Sequence[str] | None = None,
    cap: int = ARTIFACT_MAX_FRAMES,
) -> list[UmbraItem]:
    """Gather the acquisitions a composite request refers to.

    Either an explicit ``ids`` list (the client controls chronology -- the
    returned order matches the requested order) or a ``bbox``/date query (the
    source's stable acquisition-date order, i.e. chronological). ``ids`` cannot
    be capped at the source since an id may appear anywhere, so it scans; a
    bbox/date query is capped at ``cap``.
    """
    if ids:
        by_id: dict[str, UmbraItem] = {}
        wanted = set(ids)
        for it in source.search(bbox=bbox, start=start, end=end, limit=None):
            if it.id in wanted:
                by_id[it.id] = it
                if len(by_id) == len(wanted):
                    break
        return [by_id[i] for i in ids if i in by_id]
    return list(source.search(bbox=bbox, start=start, end=end, limit=cap))


def _evenly_spaced(items: list[UmbraItem], n: int) -> list[UmbraItem]:
    """``n`` items spread across ``items``, always keeping the first and last."""
    if len(items) <= n:
        return items
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in idx]


def change_frames(items: list[UmbraItem]) -> list[UmbraItem]:
    """Pick the 2--3 frames :func:`viz.change_composite` needs from a query.

    Two resolved acquisitions render the two-date (green/magenta) composite;
    three or more collapse to a first/middle/last three-date temporal-RGB.
    """
    if len(items) < 2:
        raise ValueError(
            f"A change composite needs at least 2 acquisitions, resolved {len(items)}. "
            "Widen the date range or pass explicit ids."
        )
    if len(items) <= 3:
        return items
    return _evenly_spaced(items, 3)


def timescan_frames(items: list[UmbraItem]) -> list[UmbraItem]:
    """Pick the frames :func:`viz.timescan_composite` needs (>=3, capped)."""
    if len(items) < 3:
        raise ValueError(
            f"A timescan needs at least 3 acquisitions, resolved {len(items)}. "
            "Widen the date range, or use /artifacts/change for two dates."
        )
    return _evenly_spaced(items, ARTIFACT_MAX_FRAMES)


def swipe_frames(items: list[UmbraItem]) -> list[UmbraItem]:
    """Pick the two frames :func:`viz.swipe_map` compares (before / after).

    A swipe is inherently a two-pass comparison. Exactly two resolved
    acquisitions become the before/after pair directly; a query that resolves
    more collapses to its temporal endpoints (first and last) so the seam sweeps
    the widest change the selection spans. Order is the resolved order (the
    client controls chronology via ``ids``; a bbox/date query is chronological).
    """
    if len(items) < 2:
        raise ValueError(
            f"A swipe needs at least 2 acquisitions, resolved {len(items)}. "
            "Widen the date range or pass two explicit ids."
        )
    return [items[0], items[-1]]


def stats_frames(items: list[UmbraItem]) -> list[UmbraItem]:
    """Pick (and vet) the passes the stats reduction measures.

    Needs at least two acquisitions -- change is a comparison -- and caps a long
    series at :data:`ARTIFACT_MAX_FRAMES` (evenly spaced, keeping the temporal
    endpoints, so the net baseline-to-latest record still spans the request).

    It also refuses to mix polarizations, which the *picture* endpoints tolerate.
    A composite with mixed polarizations is merely confusing to look at; a
    mixed-polarization *number* is wrong, because the difference between HH and
    VV lands on the time axis and reads as change. This is the same refusal the
    ``stack_stats`` agent tool makes.
    """
    if len(items) < 2:
        raise ValueError(
            f"A stats reduction needs at least 2 acquisitions, resolved {len(items)}. "
            "Widen the date range or pass explicit ids."
        )
    combos = {tuple(it.polarizations) for it in items if it.polarizations}
    if len(combos) > 1:
        listed = ", ".join(sorted("+".join(c) for c in combos))
        raise ValueError(
            f"Refusing to measure change across mixed polarizations ({listed}): the "
            "difference between them would land on the time axis and read as change. "
            "Filter the selection to one polarization (e.g. the 'polarizations' "
            "search parameter) or pass explicit ids."
        )
    return _evenly_spaced(items, ARTIFACT_MAX_FRAMES)


# --------------------------------------------------------------------------
# Async render jobs (202 Accepted + poll; the disk cache is the result store)
# --------------------------------------------------------------------------
#
# The composite endpoints render synchronously by default -- a downsampled
# overview returns in seconds, which is the honest first slice. But a large
# ``max_size`` or a long timescan can take tens of seconds, and a synchronous
# request holds a worker for the whole render. The productized shape
# (``docs/TODO.md``) is a small job queue: a
# request can opt in to ``"async": true``, get a ``202 Accepted`` + a job id
# back immediately, poll ``GET /jobs/{id}`` for status, and fetch the finished
# artifact from ``GET /jobs/{id}/result``. There is no separate result store --
# the render still writes the same content-addressed disk cache the synchronous
# path uses, so a completed job's result *is* a cache entry (and an async
# request whose key is already cached returns an already-``succeeded`` job with
# no work). The core stays deterministic and offline-testable: the executor is
# injectable, so a test drives the queue with an inline or hand-stepped runner
# and never depends on wall-clock timing.

#: Job lifecycle states. ``queued`` -> ``running`` -> ``succeeded`` | ``failed``.
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

#: Background workers for the default job pool. Small: renders are CPU/IO heavy
#: and this only exists to keep long renders off the request path, not to scale.
ARTIFACT_JOB_WORKERS = 2


def _utcnow_iso() -> str:
    """Current UTC time as an RFC3339 string (job timestamps)."""
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class RenderJob:
    """One queued/running/finished artifact render.

    The job holds only what is needed to report status and locate the result:
    the render ``kind``, the content-addressed ``cache_key`` + ``suffix`` that
    name its disk-cache entry, the ``media_type`` to serve it as, and (on
    failure) the error and the HTTP status the synchronous path would have used
    for it (``501`` for a missing render extra, ``500`` otherwise).
    """

    id: str
    kind: str
    cache_key: str
    suffix: str
    media_type: str
    status: str = JOB_QUEUED
    #: True when the result was already on disk at submit time (no work run).
    cached: bool = False
    error: str | None = None
    error_status: int = 500
    created: str = field(default_factory=_utcnow_iso)
    started: str | None = None
    finished: str | None = None


class JobStore:
    """A thread-safe in-memory registry of :class:`RenderJob`\\ s.

    In-memory is deliberate: the *artifacts* survive process restarts (they are
    the disk cache), so a lost job record only costs a re-submit, and a durable
    queue would be scope the first slice does not need. All state transitions
    go through this class so they are serialised under one lock.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, RenderJob] = {}
        self._lock = threading.Lock()

    def create(
        self,
        kind: str,
        cache_key: str,
        media_type: str,
        suffix: str,
        *,
        status: str = JOB_QUEUED,
        cached: bool = False,
    ) -> RenderJob:
        job = RenderJob(
            id=uuid.uuid4().hex,
            kind=kind,
            cache_key=cache_key,
            suffix=suffix,
            media_type=media_type,
            status=status,
            cached=cached,
        )
        if status == JOB_SUCCEEDED:
            job.finished = _utcnow_iso()
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> RenderJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def mark_running(self, job: RenderJob) -> None:
        with self._lock:
            job.status = JOB_RUNNING
            job.started = _utcnow_iso()

    def mark_succeeded(self, job: RenderJob) -> None:
        with self._lock:
            job.status = JOB_SUCCEEDED
            job.finished = _utcnow_iso()

    def mark_failed(self, job: RenderJob, error: str, status: int) -> None:
        with self._lock:
            job.status = JOB_FAILED
            job.error = error
            job.error_status = status
            job.finished = _utcnow_iso()


def job_to_dict(job: RenderJob, base_url: str) -> dict[str, Any]:
    """Serialise a job for ``GET /jobs/{id}`` (deterministic; no server needed).

    A ``self`` link is always present; a ``result`` link (to
    ``/jobs/{id}/result``) is added only once the job has ``succeeded``, and the
    error message is surfaced when it has ``failed``.
    """
    base = base_url.rstrip("/")
    links = [_link("self", f"{base}/jobs/{job.id}")]
    payload: dict[str, Any] = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "created": job.created,
        "started": job.started,
        "finished": job.finished,
        "links": links,
    }
    if job.status == JOB_SUCCEEDED:
        links.append(
            _link("result", f"{base}/jobs/{job.id}/result", type=job.media_type, title="Result")
        )
        payload["cache"] = "hit" if job.cached else "miss"
    if job.status == JOB_FAILED:
        payload["error"] = job.error
    return payload


class _InlineJobExecutor:
    """A :class:`~concurrent.futures.ThreadPoolExecutor`-shaped runner that runs
    work synchronously on ``submit``.

    Not used by the server (which wants a real pool), but the honest default for
    tests: a submitted job finishes before ``submit`` returns, so ``POST
    ...`` with ``"async": true`` yields an already-``succeeded`` job with no
    timing races. It mirrors the tiny slice of the executor protocol the server
    uses (``submit`` / ``shutdown``).
    """

    def submit(self, fn: Callable[[], Any]) -> None:
        fn()

    def shutdown(self, wait: bool = True) -> None:  # noqa: FBT001, FBT002 - stdlib signature
        pass


# --------------------------------------------------------------------------
# The published contracts, as OpenAPI component schemas
# --------------------------------------------------------------------------

#: The committed ``docs/schemas/`` contracts this server's routes actually
#: emit, mapped to the component name they take in the generated OpenAPI
#: document. Only these three: the picture endpoints return bytes, and a
#: document describing shapes no route emits would be a claim rather than a
#: contract.
OPENAPI_SCHEMAS: dict[str, str] = {
    "StackStats": "stack-stats",
    "StackProvenance": "stack-provenance",
    "RenderJob": "render-job",
}

#: Contracts the *always-mounted* routes reference, so they belong in every
#: instance's OpenAPI document -- unlike :data:`OPENAPI_SCHEMAS`, which describe
#: the ``/artifacts/*`` routes and appear only when those are mounted.
CORE_OPENAPI_SCHEMAS: dict[str, str] = {
    "SiteCoverage": "site-coverage",
}


def _rewrite_refs(node: Any, base: str) -> Any:
    """Re-root a schema's internal ``$ref``\\ s at its place in the document.

    A published schema resolves ``#/$defs/pass`` against its own ``$id``; the
    same schema inlined under ``components/schemas/StackStats`` has to resolve it
    against the OpenAPI document, so the pointer becomes
    ``#/components/schemas/StackStats/$defs/pass``. A cross-file ``$ref`` (the
    relative filename form ``render-manifest`` and ``watch-delta`` use) has no
    such home and is refused rather than emitted as a dangling reference -- a
    generated client that cannot resolve a shape is worse than one that was
    never given it.
    """
    if isinstance(node, dict):
        rewritten: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                if not value.startswith("#/"):
                    raise ValueError(
                        f"Cannot inline a schema with the cross-file reference {value!r}; "
                        "publish the referenced schema as its own OpenAPI component first."
                    )
                rewritten[key] = base + value[1:]
            else:
                rewritten[key] = _rewrite_refs(value, base)
        return rewritten
    if isinstance(node, list):
        return [_rewrite_refs(item, base) for item in node]
    return node


def _as_component(component: str, schema_name: str) -> dict[str, Any]:
    """One published schema, prepared to live under ``components/schemas``.

    ``$schema`` and ``$id`` are dropped: OpenAPI 3.1 already declares the
    2020-12 dialect for the whole document, and an ``$id`` would re-base the
    internal pointers :func:`_rewrite_refs` has just re-rooted onto a URL nothing
    can fetch. The identity is kept as ``x-umbra-schema-id`` instead, so a
    generated client can still see which committed contract the component is a
    copy of.
    """
    schema = load_schema(schema_name)
    schema.pop("$schema", None)
    source = schema.pop("$id", None)
    document = _rewrite_refs(schema, f"#/components/schemas/{component}")
    if source is not None:
        document["x-umbra-schema-id"] = source
    return document


def openapi_components() -> dict[str, Any]:
    """``docs/schemas/`` as OpenAPI component schemas, ready to be merged in.

    The generated OpenAPI document is how an OpenAPI-driven agent (and every
    client generator) reads this API, so describing ``/artifacts/stats``'
    response as a bare object while ``docs/schemas/stack-stats.schema.json``
    describes it exactly made the published contract unreachable from the
    surface that most needs it. These are the same files, byte-for-byte bar the
    two keywords :func:`_as_component` re-homes -- not a restatement -- so the
    HTTP surface cannot drift from the contract the CLI and the agent tools
    already emit.
    """
    return {
        component: _as_component(component, schema_name)
        for component, schema_name in OPENAPI_SCHEMAS.items()
    }


def core_openapi_components() -> dict[str, Any]:
    """The contracts the always-mounted routes reference (:data:`CORE_OPENAPI_SCHEMAS`).

    Separate from :func:`openapi_components` because these belong in *every*
    instance's document -- ``GET /sites`` is mounted whether or not the artifact
    routes are -- whereas the artifact contracts describe shapes an artifactless
    instance never emits.
    """
    return {
        component: _as_component(component, schema_name)
        for component, schema_name in CORE_OPENAPI_SCHEMAS.items()
    }


def _json_response(component: str, description: str) -> dict[str, Any]:
    """An OpenAPI response object pointing at one committed contract."""
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{component}"}}},
    }


def _binary_response(media_type: str, description: str) -> dict[str, Any]:
    """An OpenAPI response object for an artifact that is bytes, not a document."""
    return {"description": description, "content": {media_type: {}}}


#: The job document every async artifact request answers with while it renders.
_JOB_RESPONSE = _json_response(
    "RenderJob", "Render queued; poll the `self` link until `status` leaves `queued`/`running`."
)

#: The ``GET /sites`` response: the ranked wrapper over ``SiteCoverage`` records
#: (the ``sites`` items ``$ref`` the committed ``site-coverage`` contract).
_SITES_RESPONSE: dict[str, Any] = {
    "description": (
        "Ranked repeat-imaged sites, each a `docs/schemas/site-coverage` record, "
        "best-covered first."
    ),
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "resolved_bbox": {
                        "type": ["array", "null"],
                        "items": {"type": "number"},
                    },
                    "resolved_area": {"type": ["string", "null"]},
                    "query": {
                        "type": "object",
                        "description": (
                            "Echo of the ranking-and-selection inputs (the `rank_by` "
                            "order, `top`/`min_passes`, and the recency/onset/cadence/"
                            "baseline bounds), as the caller expressed them, so the "
                            "answer records how it was ranked and filtered."
                        ),
                        "properties": {
                            "rank_by": {"type": "string"},
                            "top": {"type": "integer"},
                            "min_passes": {"type": "integer"},
                            "active_since": {"type": ["string", "null"]},
                            "active_before": {"type": ["string", "null"]},
                            "first_since": {"type": ["string", "null"]},
                            "first_before": {"type": ["string", "null"]},
                            "max_revisit_days": {"type": ["number", "null"]},
                            "median_revisit_days": {"type": ["number", "null"]},
                            "min_span_days": {"type": ["number", "null"]},
                            "max_span_days": {"type": ["number", "null"]},
                        },
                    },
                    "sites": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/SiteCoverage"},
                    },
                    "attribution": {"type": "string"},
                },
            }
        }
    },
}


# --------------------------------------------------------------------------
# FastAPI application factory
# --------------------------------------------------------------------------


def build_app(
    index_path: str | os.PathLike | None = None,
    *,
    live: bool = False,
    artifacts: bool = True,
    renderers: Renderers | None = None,
    stack_execution: StackExecution | None = None,
    narrator: Any | None = None,
    narration_daily_limit: int | None = None,
    narration_client_limit: int | None = None,
    narration_allow_bbox: BBox | None = None,
    cache_dir: str | os.PathLike | None = None,
    job_executor: Any | None = None,
    mcp: bool = False,
    public: bool = False,
    rate_limit: int | None = None,
) -> FastAPI:
    """Construct the FastAPI STAC API application.

    ``index_path`` selects the catalog index (default: the shared
    :func:`~umbra_py.default_index_path`); ``live=True`` serves from a live S3
    walk instead. A fresh backend is opened and closed per request so the app
    is safe under FastAPI's thread pool.

    When ``artifacts`` is true (the default) the on-demand artifact endpoints
    (``/artifacts/quicklook/{id}.png``, ``POST /artifacts/change``,
    ``POST /artifacts/timescan``, ``POST /artifacts/swipe``, ``POST
    /artifacts/stats``) are mounted, along with the async job endpoints
    (``GET /jobs/{id}``, ``GET /jobs/{id}/result``) they use when a request opts
    in to ``"async": true``. ``renderers`` overrides the render functions
    (defaults to :func:`default_renderers`, which needs the ``viz`` extra -- and
    for stats the ``load`` extra -- at request time); ``stack_execution`` is the
    instance-wide policy for how ``POST /artifacts/stats`` builds its datacube
    (:class:`StackExecution`) and applies only to the default renderers, since
    injected ones do their own stacking.
    The narrate endpoint's spend is bounded by three optional policies:
    ``narration_daily_limit`` (the instance-wide model-call cap),
    ``narration_client_limit`` (the same cap per client, so no single caller
    bursts through the day's budget), and ``narration_allow_bbox`` (a curated
    ``(min_lon, min_lat, max_lon, max_lat)`` area outside which the endpoint
    refuses to spend at all). All three are ``None`` (unbounded) by default and
    are advertised on the landing page's ``narrate`` link.
    ``cache_dir`` overrides where rendered PNGs are cached (defaults to
    :func:`default_artifact_cache_dir`). ``job_executor`` overrides the
    background runner for async jobs (anything with ``submit(fn)`` / ``shutdown``,
    e.g. a :class:`~concurrent.futures.ThreadPoolExecutor`); it defaults to a
    small thread pool, and a test can inject :class:`_InlineJobExecutor` to run
    jobs synchronously. Requires the ``serve`` extra.

    ``mcp=True`` mounts Streamable HTTP MCP at ``/mcp`` on this same app (needs
    the ``mcp`` extra) so one process is both front doors. ``public=True`` is
    the hosted-community bundle: artifacts off, MCP on, a per-client rate
    limit (:data:`PUBLIC_RATE_LIMIT` unless ``rate_limit`` is set; ``0``
    disables it), CC-BY license headers, and a refuse of ``live=True``. Secret
    env vars are checked in :func:`serve`, not here, so tests can build a
    public app on a machine that happens to have a model key.
    """
    fastapi = _require_serve()
    from fastapi import Body, HTTPException, Query, Request, Response
    from fastapi.responses import JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    if public:
        if live:
            raise ValueError(
                "A public instance cannot serve a live S3 walk; fetch the "
                "published index instead (the §6 crawl-polite guardrail)."
            )
        artifacts = False
        mcp = True
        if rate_limit is None:
            rate_limit = PUBLIC_RATE_LIMIT

    # This module uses ``from __future__ import annotations``, so the route
    # handlers' annotations are strings that FastAPI resolves against the
    # module globals. ``Request``/``JSONResponse``/``Response`` are imported
    # lazily inside this factory (to keep the fastapi import behind the
    # ``serve`` extra), so publish them into the module namespace for that
    # resolution to succeed.
    globals().update(Request=Request, JSONResponse=JSONResponse, Response=Response)

    if renderers is None:
        renderers = default_renderers(stack_execution, narrator=narrator)
    cache_path = Path(cache_dir) if cache_dir is not None else default_artifact_cache_dir()
    # The paid-call caps for the narrate endpoint. Both are consulted only on a
    # cache miss (see ``post_narrate``), so a cached narration never spends
    # against either: the global one protects the operator's wallet in aggregate,
    # the per-client one stops a single caller bursting through it. The allowlist
    # bounds *which* scenes a (possibly public) instance will spend on at all.
    narration_budget = NarrationBudget(narration_daily_limit)
    client_narration_budget = ClientNarrationBudget(narration_client_limit)
    narration_allowlist = NarrationAllowlist(narration_allow_bbox)
    narration_policy = narrate_capabilities(
        narration_allowlist,
        daily_limit=narration_daily_limit,
        client_limit=narration_client_limit,
    )

    # Async render jobs: an in-memory registry + a background runner. Both are
    # created only when artifacts are enabled; the executor is injectable so a
    # test can drive the queue deterministically (see ``_InlineJobExecutor``).
    job_store = JobStore()
    if job_executor is None and artifacts:
        from concurrent.futures import ThreadPoolExecutor

        job_executor = ThreadPoolExecutor(
            max_workers=ARTIFACT_JOB_WORKERS, thread_name_prefix="umbra-render"
        )

    mcp_asgi = None
    if mcp:
        from .mcp_server import build_server as _build_mcp_server

        mcp_asgi = _build_mcp_server().streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
        )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # MCP's session manager (even in stateless HTTP) starts in its lifespan;
        # run it around the STAC app so ``POST /mcp`` is live for the same
        # process that answers ``/search``.
        mcp_life = (
            mcp_asgi.router.lifespan_context(mcp_asgi)
            if mcp_asgi is not None and mcp_asgi.router.lifespan_context is not None
            else nullcontext()
        )
        async with mcp_life:
            try:
                yield
            finally:
                # Drain the background render pool on shutdown so a stopping
                # server does not hang on (or leak) in-flight renders.
                if job_executor is not None:
                    job_executor.shutdown(wait=False)

    app = fastapi.FastAPI(
        title="Umbra Open Data STAC API",
        description=(
            "Read-only STAC API over Umbra's open SAR archive, served by "
            "umbra-py from a local catalog index."
        ),
        version=STAC_VERSION,
        lifespan=_lifespan,
    )

    # The server is read-only, so a permissive CORS policy is safe and is what
    # browser STAC clients (leafmap, stac-browser) and the static ``umbra demo``
    # page -- which loads from ``file://`` or a different static host -- need to
    # call ``/search`` and the ``/artifacts/...`` render endpoints cross-origin.
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[
            "Link",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-Umbra-Attribution",
            "X-Umbra-License",
        ],
    )

    limiter = RateLimiter(rate_limit) if rate_limit else None

    class _GuardMiddleware(BaseHTTPMiddleware):
        """CC-BY headers on every response; optional per-client request cap."""

        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            path = request.url.path
            remaining: int | None = None
            if (
                limiter is not None
                and request.method != "OPTIONS"
                and path not in RATE_LIMIT_EXEMPT_PATHS
            ):
                allowed, remaining, retry_after = limiter.hit(client_identity(request))
                if not allowed:
                    return JSONResponse(
                        {
                            "error": "RateLimited",
                            "message": (
                                f"Rate limit exceeded ({limiter.limit} requests "
                                f"per {int(limiter.window_s)}s)."
                            ),
                            "hint": f"Retry after {retry_after} seconds.",
                        },
                        status_code=429,
                        headers={
                            "Retry-After": str(retry_after),
                            "X-RateLimit-Limit": str(limiter.limit),
                            "X-RateLimit-Remaining": "0",
                            "X-Umbra-License": DATA_LICENSE,
                            "X-Umbra-Attribution": ATTRIBUTION,
                            "Link": f'<{LICENSE_URL}>; rel="license"',
                        },
                    )
            response = await call_next(request)
            response.headers["X-Umbra-License"] = DATA_LICENSE
            response.headers["X-Umbra-Attribution"] = ATTRIBUTION
            response.headers["Link"] = f'<{LICENSE_URL}>; rel="license"'
            if limiter is not None:
                response.headers.setdefault("X-RateLimit-Limit", str(limiter.limit))
                if remaining is not None:
                    response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
            return response

    app.add_middleware(_GuardMiddleware)

    def _open():
        try:
            return open_source(index_path, live=live)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _close(source: Any) -> None:
        close = getattr(source, "close", None)
        if callable(close):
            close()

    def _temporal() -> tuple[str | None, str | None]:
        source = _open()
        try:
            stats = getattr(source, "stats", None)
            if stats is None:
                return (None, None)
            s = stats()
            return (s.get("start"), s.get("end"))
        finally:
            _close(source)

    geojson = "application/geo+json"

    @app.get("/", tags=["STAC"])
    def get_landing(request: Request) -> dict[str, Any]:
        # The stacking policy rides along so the ``stats`` link can advertise
        # which of its instance-dependent options this server accepts. It
        # describes the *default* renderers, as the policy itself does; injected
        # ones do their own stacking and answer for themselves.
        return landing_page(
            str(request.base_url),
            artifacts=artifacts,
            stack_execution=stack_execution,
            narrate=renderers.narrate is not None,
            narrate_policy=narration_policy,
            mcp=mcp,
            public=public,
        )

    @app.get("/conformance", tags=["STAC"])
    def get_conformance() -> dict[str, Any]:
        return conformance()

    @app.get("/healthz", tags=["Ops"])
    def get_health() -> dict[str, Any]:
        # Liveness + readiness for container orchestration (a Docker
        # HEALTHCHECK, a Kubernetes probe). Returns 200 whenever the HTTP
        # server is up; the body's `ready` reports whether the search backend
        # can answer queries yet -- on first boot the published-index fetch may
        # still be in flight, so a server can be alive but not yet ready.
        if live:
            return health_document(backend="live", ready=True)
        try:
            source = open_source(index_path, live=False)
        except FileNotFoundError:
            return health_document(backend="index", ready=False)
        try:
            items: int | None = None
            stats = getattr(source, "stats", None)
            if stats is not None:
                try:
                    items = int(stats()["items"])
                except (KeyError, TypeError, ValueError):
                    items = None
            return health_document(backend="index", ready=True, items=items)
        finally:
            _close(source)

    @app.get("/collections", tags=["STAC"])
    def get_collections(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {
            "collections": [collection(base, temporal=_temporal())],
            "links": [
                _link("self", f"{base}/collections"),
                _link("root", f"{base}/"),
            ],
        }

    @app.get("/collections/{collection_id}", tags=["STAC"])
    def get_collection(collection_id: str, request: Request) -> dict[str, Any]:
        if collection_id != COLLECTION_ID:
            raise HTTPException(status_code=404, detail=f"No collection {collection_id!r}")
        return collection(str(request.base_url), temporal=_temporal())

    def _do_search(
        request: Request,
        *,
        bbox: BBox | None,
        intersects: Geometry | None = None,
        start: date | None,
        end: date | None,
        ids: list[str] | None,
        product_types: list[str] | None = None,
        area: str | None = None,
        fuzzy: bool = False,
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
        limit: int,
        offset: int,
        self_href: str,
    ) -> JSONResponse:
        source = _open()
        try:
            page, has_next = run_search(
                source,
                bbox=bbox,
                intersects=intersects,
                start=start,
                end=end,
                ids=ids,
                product_types=product_types,
                area=area,
                fuzzy=fuzzy,
                polarizations=polarizations,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
                limit=limit,
                offset=offset,
            )
        finally:
            _close(source)
        next_href = None
        if has_next:
            sep = "&" if "?" in self_href else "?"
            next_href = f"{self_href}{sep}token={offset + limit}"
        result = search_result(
            page,
            str(request.base_url),
            returned=len(page),
            next_href=next_href,
            self_href=self_href,
        )
        return JSONResponse(content=result, media_type=geojson)

    @app.get("/collections/{collection_id}/items", tags=["STAC"])
    def get_items(
        collection_id: str,
        request: Request,
        bbox: str | None = Query(default=None),
        datetime: str | None = Query(default=None),
        product_types: str | None = Query(
            default=None, description="Comma-separated product types, e.g. GEC,SICD"
        ),
        area: str | None = Query(default=None, description="Free-text task/site substring"),
        fuzzy: bool = Query(default=False, description="Token-wise fuzzy area match"),
        polarizations: str | None = Query(
            default=None, description="Comma-separated polarizations, e.g. VV,VH"
        ),
        min_incidence: float | None = Query(
            default=None, description="Minimum view incidence angle (degrees)"
        ),
        max_incidence: float | None = Query(
            default=None, description="Maximum view incidence angle (degrees)"
        ),
        max_resolution: float | None = Query(
            default=None, description="Coarsest range/azimuth resolution to keep (metres)"
        ),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        token: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        if collection_id != COLLECTION_ID:
            raise HTTPException(status_code=404, detail=f"No collection {collection_id!r}")
        try:
            parsed_bbox = parse_bbox(bbox)
            start, end = parse_datetime(datetime)
            wanted_products = parse_product_types(product_types)
            wanted_pols = parse_polarizations(polarizations)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _do_search(
            request,
            bbox=parsed_bbox,
            start=start,
            end=end,
            ids=None,
            product_types=wanted_products,
            area=area,
            fuzzy=fuzzy,
            polarizations=wanted_pols,
            min_incidence=min_incidence,
            max_incidence=max_incidence,
            max_resolution=max_resolution,
            limit=limit,
            offset=token,
            self_href=str(request.url),
        )

    @app.get("/collections/{collection_id}/items/{item_id}", tags=["STAC"])
    def get_item(collection_id: str, item_id: str, request: Request) -> JSONResponse:
        if collection_id != COLLECTION_ID:
            raise HTTPException(status_code=404, detail=f"No collection {collection_id!r}")
        source = _open()
        try:
            item = get_one(source, item_id)
        finally:
            _close(source)
        if item is None:
            raise HTTPException(status_code=404, detail=f"No item {item_id!r}")
        return JSONResponse(content=item_to_stac(item, str(request.base_url)), media_type=geojson)

    @app.get("/search", tags=["STAC"])
    def get_search(
        request: Request,
        bbox: str | None = Query(default=None),
        intersects: str | None = Query(
            default=None, description="GeoJSON polygon geometry as a JSON string"
        ),
        datetime: str | None = Query(default=None),
        ids: str | None = Query(default=None, description="Comma-separated item ids"),
        collections: str | None = Query(default=None),
        product_types: str | None = Query(
            default=None, description="Comma-separated product types, e.g. GEC,SICD"
        ),
        area: str | None = Query(default=None, description="Free-text task/site substring"),
        fuzzy: bool = Query(default=False, description="Token-wise fuzzy area match"),
        polarizations: str | None = Query(
            default=None, description="Comma-separated polarizations, e.g. VV,VH"
        ),
        min_incidence: float | None = Query(
            default=None, description="Minimum view incidence angle (degrees)"
        ),
        max_incidence: float | None = Query(
            default=None, description="Maximum view incidence angle (degrees)"
        ),
        max_resolution: float | None = Query(
            default=None, description="Coarsest range/azimuth resolution to keep (metres)"
        ),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        token: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        _check_collections(collections.split(",") if collections else None)
        if intersects and bbox:
            raise HTTPException(
                status_code=400, detail="bbox and intersects are mutually exclusive"
            )
        try:
            parsed_bbox = parse_bbox(bbox)
            parsed_geometry = parse_intersects(intersects)
            start, end = parse_datetime(datetime)
            wanted_products = parse_product_types(product_types)
            wanted_pols = parse_polarizations(polarizations)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        id_list = [i for i in ids.split(",") if i] if ids else None
        return _do_search(
            request,
            bbox=parsed_bbox,
            intersects=parsed_geometry,
            start=start,
            end=end,
            ids=id_list,
            product_types=wanted_products,
            area=area,
            fuzzy=fuzzy,
            polarizations=wanted_pols,
            min_incidence=min_incidence,
            max_incidence=max_incidence,
            max_resolution=max_resolution,
            limit=limit,
            offset=token,
            self_href=str(request.url),
        )

    @app.post("/search", tags=["STAC"])
    def post_search(request: Request, body: dict[str, Any] = Body(default={})) -> JSONResponse:
        _check_collections(body.get("collections"))
        if body.get("intersects") is not None and body.get("bbox") is not None:
            raise HTTPException(
                status_code=400, detail="bbox and intersects are mutually exclusive"
            )
        try:
            parsed_bbox = parse_bbox(body.get("bbox"))
            parsed_geometry = parse_intersects(body.get("intersects"))
            start, end = parse_datetime(body.get("datetime"))
            # The Query-extension filters can arrive either as a STAC ``query``
            # object or as plain top-level fields; a top-level field, when given,
            # overrides the same field inside ``query``.
            q = parse_query(body.get("query"))
            top_products = parse_product_types(body.get("product_types"))
            wanted_products = top_products if top_products is not None else q.product_types
            top_area = body.get("area")
            area = str(top_area).strip() if top_area not in (None, "") else q.area
            top_pols = parse_polarizations(body.get("polarizations"))
            polarizations = top_pols if top_pols is not None else q.polarizations
            min_incidence = _opt_float(body.get("min_incidence"), "min_incidence")
            if min_incidence is None:
                min_incidence = q.min_incidence
            max_incidence = _opt_float(body.get("max_incidence"), "max_incidence")
            if max_incidence is None:
                max_incidence = q.max_incidence
            max_resolution = _opt_float(body.get("max_resolution"), "max_resolution")
            if max_resolution is None:
                max_resolution = q.max_resolution
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ids = body.get("ids")
        fuzzy = bool(body.get("fuzzy", False))
        limit = _clamp_limit(body.get("limit"))
        offset = int(body.get("token") or 0)
        base = str(request.base_url).rstrip("/")
        source = _open()
        try:
            page, has_next = run_search(
                source,
                bbox=parsed_bbox,
                intersects=parsed_geometry,
                start=start,
                end=end,
                ids=list(ids) if ids else None,
                product_types=wanted_products,
                area=area,
                fuzzy=fuzzy,
                polarizations=polarizations,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
                limit=limit,
                offset=offset,
            )
        finally:
            _close(source)
        next_href = f"{base}/search?token={offset + limit}" if has_next else None
        result = search_result(
            page,
            str(request.base_url),
            returned=len(page),
            next_href=next_href,
            self_href=f"{base}/search",
        )
        return JSONResponse(content=result, media_type=geojson)

    @app.get(
        "/sites",
        tags=["Discovery"],
        responses={200: _SITES_RESPONSE},
    )
    def get_sites(
        request: Request,
        bbox: str | None = Query(default=None),
        intersects: str | None = Query(
            default=None, description="GeoJSON polygon geometry as a JSON string"
        ),
        datetime: str | None = Query(default=None),
        product_types: str | None = Query(
            default=None, description="Comma-separated product types, e.g. GEC,SICD"
        ),
        area: str | None = Query(default=None, description="Free-text task/site substring"),
        fuzzy: bool = Query(default=False, description="Token-wise fuzzy area match"),
        polarizations: str | None = Query(
            default=None, description="Comma-separated polarizations, e.g. VV,VH"
        ),
        min_incidence: float | None = Query(
            default=None, description="Minimum view incidence angle (degrees)"
        ),
        max_incidence: float | None = Query(
            default=None, description="Maximum view incidence angle (degrees)"
        ),
        max_resolution: float | None = Query(
            default=None, description="Coarsest range/azimuth resolution to keep (metres)"
        ),
        limit: int = Query(
            default=SITES_POOL_LIMIT,
            ge=1,
            le=MAX_LIMIT,
            description=(
                "Pool size for a live (`--live`) backend only; ignored on an "
                "index, which ranks the whole archive"
            ),
        ),
        top: int = Query(
            default=SITES_DEFAULT_TOP,
            ge=1,
            le=SITES_MAX_TOP,
            description="How many sites to return, best-covered first",
        ),
        min_passes: int = Query(
            default=SITES_MIN_PASSES,
            ge=1,
            description=(
                "How many passes a site needs to qualify, counted on the depth "
                "rank_by measures: raw dated passes by default, the usable "
                "(comparable) series' depth under rank_by=comparable"
            ),
        ),
        rank_by: str = Query(
            default="passes",
            description=(
                "Order sites by depth -- 'passes' (raw pass count) or 'comparable' "
                "(the usable series' depth, the largest same-polarization dated "
                "subset a change verb can difference) -- or by a temporal axis: "
                "'recency' (newest pass first, the still-active target), 'span' "
                "(longest baseline first, for slow change) or 'cadence' (tightest "
                "typical revisit first, the most-frequently-imaged site -- the median "
                "gap, not the worst one), ordering by the same figures "
                "active_since/before, min_span/max_span and median_revisit filter on"
            ),
        ),
        active_since: str | None = Query(
            default=None,
            description=(
                "Keep only sites still imaged on or after this date (an ISO date, "
                "bare year/month, or relative expression like '6 months ago') -- a "
                "recency filter on each site's newest pass, for live monitoring "
                "targets. Unlike the datetime filter, it selects whole sites and "
                "keeps each survivor's full history"
            ),
        ),
        active_before: str | None = Query(
            default=None,
            description=(
                "The complement of active_since: keep only sites last imaged on or "
                "before this date (a dormant series). Set both to select sites whose "
                "newest pass falls within a window. Same grammar, but a bare "
                "year/month covers the whole period (2024 is 'on or before "
                "2024-12-31'), symmetric with the datetime end"
            ),
        ),
        first_since: str | None = Query(
            default=None,
            description=(
                "Keep only sites FIRST imaged on or after this date -- an onset filter "
                "on each site's earliest pass, selecting newly-appeared series, where "
                "active_since gates the newest pass (still live). Same grammar as "
                "active_since. Orthogonal to the active_* recency filters"
            ),
        ),
        first_before: str | None = Query(
            default=None,
            description=(
                "The complement of first_since (the onset twin of active_before): keep "
                "only sites first imaged on or before this date (a long-established "
                "series). Set both to bound the onset to a window. A bare year/month "
                "covers the whole period (2024 is 'on or before 2024-12-31')"
            ),
        ),
        max_revisit: float | None = Query(
            default=None,
            gt=0,
            description=(
                "Keep only sites revisited at least this often -- a cadence filter on "
                "each site's worst-case revisit gap (in days), so a series with any "
                "stretch longer than this between consecutive passes is dropped. Gates "
                "the cadence rank_by measures (the usable series' worst gap under "
                "rank_by=comparable); orthogonal to the active_since/before recency "
                "filters"
            ),
        ),
        median_revisit: float | None = Query(
            default=None,
            gt=0,
            description=(
                "The typical-cadence twin of max_revisit: keep only sites whose MEDIAN "
                "revisit gap (in days) is at most this, so a site usually imaged often "
                "is kept even if a single stretch runs long. 'Usually imaged frequently' "
                "rather than 'never blind for longer than N days'; set both to combine "
                "them. Gates the cadence rank_by measures (the usable series' typical "
                "gap under rank_by=comparable); orthogonal to max_revisit and the "
                "recency filters"
            ),
        ),
        min_span: float | None = Query(
            default=None,
            gt=0,
            description=(
                "Keep only sites imaged over at least this long -- a baseline filter on "
                "each site's observation span (in days, first pass to last), so a series "
                "confined to a short window is dropped. The discovery answer for slow "
                "change (subsidence, construction) that needs a long window to show; a "
                "different axis from max_revisit (cadence vs baseline). Gates the span "
                "rank_by measures (the usable series' span under rank_by=comparable); "
                "orthogonal to the recency and cadence filters"
            ),
        ),
        max_span: float | None = Query(
            default=None,
            gt=0,
            description=(
                "The upper twin of min_span: keep only sites imaged over at most this "
                "long (a short-lived series, now over). Set with min_span the two bound "
                "each site's baseline to a window (min_span <= span <= max_span), as "
                "active_since/active_before bound the newest pass. Gates the span "
                "rank_by measures (the usable series' span under rank_by=comparable)"
            ),
        ),
    ) -> JSONResponse:
        """Rank the archive's most repeat-imaged sites — discovery before analysis.

        The HTTP front door for the discovery step every analysis endpoint
        assumes: ``/artifacts/change`` / ``timescan`` / ``stats`` answer *what*
        changed at a site, but each needs the caller to already know *which*
        site's passes to send. Umbra files every pass of an area under one task,
        so a site's coverage is how many dated passes share it; the ones with the
        most are exactly where a time series exists to measure.

        The filters scope the ranking and are the same ones ``GET /search`` takes
        (``bbox`` or ``intersects``, ``datetime``, ``product_types``, ``area`` /
        ``fuzzy``, and the SAR properties). On an index the depth is measured
        **whole-archive** (a ``GROUP BY task`` over the entire catalog), so a site
        deep in passes ranks by all of them rather than by whatever a pool cap
        admitted; ``limit`` sizes the pool only on a ``--live`` backend, which has
        no index to group over. ``top`` caps how many sites come back, and
        ``min_passes`` is how many passes a site needs to qualify (2 is the minimum
        a change composite can use), counted on the depth ``rank_by`` measures --
        raw dated passes by default, the usable (comparable) series' depth under
        ``rank_by=comparable``, so that pairing returns only sites whose
        differenceable series is at least ``min_passes`` deep. Each returned site is a
        ``site-coverage`` record with
        the pass ``hrefs`` **oldest-first**, ready to send straight to
        ``POST /artifacts/stats`` / ``change``. The ranking is single-sourced with
        ``umbra sites`` and the ``find_repeat_sites`` agent tool, and no model is
        called (a number ranks the sites).
        """
        if intersects and bbox:
            raise HTTPException(
                status_code=400, detail="bbox and intersects are mutually exclusive"
            )
        try:
            parsed_bbox = parse_bbox(bbox)
            parsed_geometry = parse_intersects(intersects)
            start, end = parse_datetime(datetime)
            wanted_products = parse_product_types(product_types)
            wanted_pols = parse_polarizations(polarizations)
            # Coerce here (not in run_sites) so a malformed date is a clean 400,
            # like start/end; a relative expression ('6 months ago') resolves too.
            resolved_active_since = _coerce_date(active_since)
            # ``is_end`` snaps a span (a bare year/month) to its last day, so an
            # upper recency bound covers the whole named period, like the end bound.
            resolved_active_before = _coerce_date(active_before, is_end=True)
            # The onset (first-seen) bounds gate the earliest pass, snapping the same
            # way the recency pair does: first_since to a span's first day, first_before
            # to its last, so a bare year/month bounds the whole named period.
            resolved_first_since = _coerce_date(first_since)
            resolved_first_before = _coerce_date(first_before, is_end=True)
            from .coverage import _check_ranking  # noqa: PLC0415

            _check_ranking(rank_by)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source = _open()
        try:
            sites = run_sites(
                source,
                bbox=parsed_bbox,
                intersects=parsed_geometry,
                start=start,
                end=end,
                product_types=wanted_products,
                area=area,
                fuzzy=fuzzy,
                polarizations=wanted_pols,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
                limit=limit,
                top=top,
                min_passes=min_passes,
                rank_by=rank_by,
                active_since=resolved_active_since,
                active_before=resolved_active_before,
                first_since=resolved_first_since,
                first_before=resolved_first_before,
                max_revisit_days=max_revisit,
                median_revisit_days=median_revisit,
                min_span_days=min_span,
                max_span_days=max_span,
            )
        finally:
            _close(source)
        base = str(request.base_url).rstrip("/")
        # Echo the ranking-and-selection inputs as the caller expressed them (the
        # raw recency/onset expressions, not the coerced dates), so the answer says
        # how it was ranked and filtered -- single-sourced with the agent tool.
        query = site_query_echo(
            rank_by=rank_by,
            top=top,
            min_passes=min_passes,
            active_since=active_since,
            active_before=active_before,
            first_since=first_since,
            first_before=first_before,
            max_revisit_days=max_revisit,
            median_revisit_days=median_revisit,
            min_span_days=min_span,
            max_span_days=max_span,
        )
        result = sites_result(
            sites,
            str(request.base_url),
            resolved_bbox=parsed_bbox,
            resolved_area=area,
            query=query,
            self_href=f"{base}/sites",
        )
        return JSONResponse(content=result)

    @app.post(
        "/sites",
        tags=["Discovery"],
        responses={200: _SITES_RESPONSE},
    )
    def post_sites(request: Request, body: dict[str, Any] = Body(default={})) -> JSONResponse:
        """Rank the archive's most repeat-imaged sites from a JSON body.

        The POST twin of ``GET /sites``, mirroring the ``GET``/``POST /search``
        pair: same ranking, same ``site-coverage`` records, same filters -- but a
        body carries ``intersects`` as a GeoJSON object rather than the
        JSON-string query param ``GET`` needs, which is the ergonomic form for a
        real area-of-interest polygon (the reason ``POST /search`` exists beside
        ``GET /search``). The SAR/date filters arrive as top-level fields or
        inside a STAC ``query`` object exactly as ``POST /search`` accepts them (a
        top-level field overrides the same field in ``query``); ``limit`` sizes
        the pool only on a ``--live`` backend, and ``top`` / ``min_passes`` cap
        and qualify the ranking as on ``GET``. ``active_since`` (a top-level field)
        keeps only sites still imaged on or after that date, and ``active_before``
        its complement (sites last imaged on or before it); ``first_since`` /
        ``first_before`` are the onset twins (sites *first* imaged on or after / before
        a date -- newly-appeared vs long-established series, set both for a window);
        ``max_revisit`` (days)
        keeps only sites revisited at least that often, and ``median_revisit`` (days)
        its typical-cadence twin (sites whose *median* gap is at most that -- usually
        imaged often, tolerating the odd outage); ``min_span`` (days) keeps only
        sites imaged over at least that long (an observation-baseline filter, a
        different axis from ``max_revisit``), and ``max_span`` (days) its upper twin
        (sites imaged over at most that long, a short-lived series -- set with
        ``min_span`` to bound the baseline to a window) -- all top-level fields, exactly
        as on ``GET``.
        """
        if body.get("intersects") is not None and body.get("bbox") is not None:
            raise HTTPException(
                status_code=400, detail="bbox and intersects are mutually exclusive"
            )
        try:
            parsed_bbox = parse_bbox(body.get("bbox"))
            parsed_geometry = parse_intersects(body.get("intersects"))
            start, end = parse_datetime(body.get("datetime"))
            # The Query-extension filters can arrive either as a STAC ``query``
            # object or as plain top-level fields; a top-level field, when given,
            # overrides the same field inside ``query`` -- identical to POST /search.
            q = parse_query(body.get("query"))
            top_products = parse_product_types(body.get("product_types"))
            wanted_products = top_products if top_products is not None else q.product_types
            top_area = body.get("area")
            area = str(top_area).strip() if top_area not in (None, "") else q.area
            top_pols = parse_polarizations(body.get("polarizations"))
            polarizations = top_pols if top_pols is not None else q.polarizations
            min_incidence = _opt_float(body.get("min_incidence"), "min_incidence")
            if min_incidence is None:
                min_incidence = q.min_incidence
            max_incidence = _opt_float(body.get("max_incidence"), "max_incidence")
            if max_incidence is None:
                max_incidence = q.max_incidence
            max_resolution = _opt_float(body.get("max_resolution"), "max_resolution")
            if max_resolution is None:
                max_resolution = q.max_resolution
            limit = _opt_int(body.get("limit"), "limit")
            top = _opt_int(body.get("top"), "top")
            min_passes = _opt_int(body.get("min_passes"), "min_passes")
            rank_by = body.get("rank_by", "passes")
            # Coerce here so a malformed date is a 400, like start/end; accepts an
            # ISO date, a bare year/month or a relative expression ('6 months ago').
            resolved_active_since = _coerce_date(body.get("active_since"))
            # ``is_end`` snaps a span to its last day, like the datetime end bound.
            resolved_active_before = _coerce_date(body.get("active_before"), is_end=True)
            # The onset (first-seen) bounds gate the earliest pass, snapping the same
            # way the recency pair does (first_before to a span's last day).
            resolved_first_since = _coerce_date(body.get("first_since"))
            resolved_first_before = _coerce_date(body.get("first_before"), is_end=True)
            max_revisit = _opt_float(body.get("max_revisit"), "max_revisit")
            median_revisit = _opt_float(body.get("median_revisit"), "median_revisit")
            min_span = _opt_float(body.get("min_span"), "min_span")
            max_span = _opt_float(body.get("max_span"), "max_span")
            from .coverage import (  # noqa: PLC0415
                _check_max_revisit,
                _check_max_span,
                _check_median_revisit,
                _check_min_span,
                _check_ranking,
            )

            _check_ranking(rank_by)
            # A non-positive cadence bound is a clean 400 here, like GET's gt=0.
            _check_max_revisit(max_revisit)
            # And its typical-cadence twin, symmetric with the worst-case bound.
            _check_median_revisit(median_revisit)
            # A non-positive span bound is a clean 400 too, like GET's gt=0.
            _check_min_span(min_span)
            # And its upper twin, symmetric with the floor.
            _check_max_span(max_span)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fuzzy = bool(body.get("fuzzy", False))
        # Default top/min_passes once, so the ranking and the query echo report the
        # same effective values (the echo says what actually ranked, not what the
        # body happened to omit).
        eff_top = top if top is not None else SITES_DEFAULT_TOP
        eff_min_passes = min_passes if min_passes is not None else SITES_MIN_PASSES
        source = _open()
        try:
            sites = run_sites(
                source,
                bbox=parsed_bbox,
                intersects=parsed_geometry,
                start=start,
                end=end,
                product_types=wanted_products,
                area=area,
                fuzzy=fuzzy,
                polarizations=polarizations,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
                limit=limit if limit is not None else SITES_POOL_LIMIT,
                top=eff_top,
                min_passes=eff_min_passes,
                rank_by=rank_by,
                active_since=resolved_active_since,
                active_before=resolved_active_before,
                first_since=resolved_first_since,
                first_before=resolved_first_before,
                max_revisit_days=max_revisit,
                median_revisit_days=median_revisit,
                min_span_days=min_span,
                max_span_days=max_span,
            )
        finally:
            _close(source)
        base = str(request.base_url).rstrip("/")
        # Echo the ranking-and-selection inputs as the body expressed them (the raw
        # recency/onset expressions, not the coerced dates) -- single-sourced with
        # GET /sites and the agent tool.
        query = site_query_echo(
            rank_by=rank_by,
            top=eff_top,
            min_passes=eff_min_passes,
            active_since=body.get("active_since"),
            active_before=body.get("active_before"),
            first_since=body.get("first_since"),
            first_before=body.get("first_before"),
            max_revisit_days=max_revisit,
            median_revisit_days=median_revisit,
            min_span_days=min_span,
            max_span_days=max_span,
        )
        result = sites_result(
            sites,
            str(request.base_url),
            resolved_bbox=parsed_bbox,
            resolved_area=area,
            query=query,
            self_href=f"{base}/sites",
        )
        return JSONResponse(content=result)

    def _check_collections(collections: list[str] | None) -> None:
        if collections and COLLECTION_ID not in collections:
            raise HTTPException(
                status_code=400,
                detail=f"Only the {COLLECTION_ID!r} collection is served.",
            )

    # ----------------------------------------------------------------------
    # On-demand render artifacts
    # ----------------------------------------------------------------------

    def _cache_file(key: str, suffix: str) -> Path:
        return cache_path / f"{key}.{suffix}"

    def _write_cache(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(payload)
        tmp.replace(path)

    def _serve_artifact(
        kind: str,
        items: list[UmbraItem],
        options: Mapping[str, Any],
        render: Callable[[], bytes],
        *,
        media_type: str = "image/png",
        suffix: str = "png",
    ) -> Response:
        """Cache-or-render an artifact and return it with cache metadata.

        Defaults to a PNG; ``media_type``/``suffix`` let the swipe endpoint
        serve a ``text/html`` product from its own cache entry without confusing
        it with the PNG composites (a swipe and a change over the same items are
        distinct files).

        A render's ``ValueError`` is the client's mistake, not the server's --
        acquisitions whose footprints share no ground under
        ``extent="intersection"`` is the common one -- so it answers ``400``
        with the message rather than a ``500`` the caller can only read as "the
        server broke".
        """
        key = artifact_cache_key(kind, [it.id for it in items], options)
        path = _cache_file(key, suffix)
        if path.exists():
            return Response(
                content=path.read_bytes(),
                media_type=media_type,
                headers={"X-Umbra-Cache": "hit"},
            )
        try:
            payload = render()
        except MissingDependencyError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _write_cache(path, payload)
        return Response(
            content=payload,
            media_type=media_type,
            headers={"X-Umbra-Cache": "miss"},
        )

    def _run_job(job: RenderJob, render: Callable[[], bytes]) -> None:
        """Execute one queued render on the background runner.

        Renders, writes the shared disk cache, and records the terminal state on
        the job. A missing render extra becomes a ``failed`` job the result
        endpoint reports as ``501`` and bad input a ``400`` (mirroring the
        synchronous path); any other error becomes a ``500``. Exceptions never
        escape -- they are the job's recorded outcome, not a crash of the worker
        thread.
        """
        job_store.mark_running(job)
        try:
            payload = render()
        except MissingDependencyError as exc:
            job_store.mark_failed(job, str(exc), 501)
            return
        except ValueError as exc:
            job_store.mark_failed(job, str(exc), 400)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the client via job status
            job_store.mark_failed(job, str(exc), 500)
            return
        _write_cache(_cache_file(job.cache_key, job.suffix), payload)
        job_store.mark_succeeded(job)

    def _submit_artifact(
        request: Request,
        kind: str,
        items: list[UmbraItem],
        options: Mapping[str, Any],
        render: Callable[[], bytes],
        *,
        media_type: str,
        suffix: str,
    ) -> JSONResponse:
        """Queue an async render (or short-circuit an already-cached one).

        Returns a job document. When the content-addressed result is already on
        disk the job is born ``succeeded`` (HTTP ``200``, no work run); otherwise
        it is ``queued`` on the executor and the response is ``202 Accepted``
        with a ``Location`` header pointing at the poll URL.
        """
        # The async artifact routes are only mounted when an executor exists
        # (``job_executor`` is set whenever artifacts are enabled), so this
        # closure never runs without one.
        assert job_executor is not None
        key = artifact_cache_key(kind, [it.id for it in items], options)
        base = str(request.base_url).rstrip("/")
        if _cache_file(key, suffix).exists():
            job = job_store.create(kind, key, media_type, suffix, status=JOB_SUCCEEDED, cached=True)
        else:
            job = job_store.create(kind, key, media_type, suffix)
            job_executor.submit(lambda: _run_job(job, render))
        # Report the job's actual state: 200 once it has succeeded (an already
        # cached result, or a synchronous executor that finished during submit),
        # 202 while it is still queued/running in the background.
        status_code = 200 if job.status == JOB_SUCCEEDED else 202
        return JSONResponse(
            status_code=status_code,
            content=job_to_dict(job, str(request.base_url)),
            headers={"Location": f"{base}/jobs/{job.id}"},
        )

    def _resolve_for_composite(body: Mapping[str, Any]) -> list[UmbraItem]:
        try:
            bbox = parse_bbox(body.get("bbox"))
            start, end = parse_datetime(body.get("datetime"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raw_ids = body.get("ids")
        ids = [str(i) for i in raw_ids] if raw_ids else None
        source = _open()
        try:
            return resolve_items(source, bbox=bbox, start=start, end=end, ids=ids)
        finally:
            _close(source)

    if artifacts:

        @app.get(
            "/artifacts/quicklook/{item_id}.png",
            tags=["Artifacts"],
            response_class=Response,
            responses={200: _binary_response("image/png", "The acquisition's SAR quicklook.")},
        )
        def get_quicklook(
            item_id: str,
            asset: str = Query(default="GEC"),
            max_size: int = Query(default=ARTIFACT_MAX_SIZE, ge=64, le=8192),
            db: bool = Query(default=False),
        ) -> Response:
            source = _open()
            try:
                page, _ = run_search(source, ids=[item_id], limit=1)
            finally:
                _close(source)
            if not page:
                raise HTTPException(status_code=404, detail=f"No item {item_id!r}")
            item = page[0]
            options = artifact_options({"asset": asset, "max_size": max_size, "db": db})
            return _serve_artifact(
                "quicklook", [item], options, lambda: renderers.quicklook(item, options)
            )

        @app.get(
            "/artifacts/thumbnail/{item_id}.png",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _binary_response("image/png", "The baked quicklook thumbnail, unrendered.")
            },
        )
        def get_thumbnail(item_id: str) -> Response:
            """Serve a baked quicklook thumbnail straight from the index.

            Unlike ``/artifacts/quicklook`` this renders nothing: it returns the
            small PNG ``umbra index bake-thumbnails`` stored in the index, so a
            demo grid or map preview is an instant, offline file read instead of
            an S3 COG stream. A ``404`` means the acquisition is unknown *or* its
            thumbnail has not been baked -- fall back to ``/artifacts/quicklook``.
            """
            source = _open()
            try:
                png = source.get_thumbnail(item_id) if isinstance(source, CatalogIndex) else None
            finally:
                _close(source)
            if png is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No baked thumbnail for {item_id!r}. Bake the index with "
                        "'umbra index bake-thumbnails', or use "
                        f"/artifacts/quicklook/{item_id}.png to render on demand."
                    ),
                )
            return Response(content=png, media_type="image/png")

        def _composite(
            request: Request,
            body: Mapping[str, Any],
            kind: str,
            pick_frames: Callable[[list[UmbraItem]], list[UmbraItem]],
            render_one: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes],
            *,
            media_type: str = "image/png",
            suffix: str = "png",
            make_options: Callable[[Mapping[str, Any]], dict[str, Any]] = artifact_options,
        ) -> Response:
            """Resolve frames for a composite and render it sync or async.

            Frame resolution and validation are always synchronous, so a bad
            request (too few acquisitions, malformed bbox) is a fast ``400`` and
            never becomes a doomed background job. Only the render itself is
            deferred, and only when the request opts in to ``"async": true`` --
            in which case the caller gets a job document instead of the artifact.

            ``make_options`` normalises the request's render options (and is part
            of the cache key); the stats endpoint passes its own, which carries
            the stacking parameters the picture endpoints have no use for.
            """
            items = _resolve_for_composite(body)
            try:
                frames = pick_frames(items)
                options = make_options(body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            def render() -> bytes:
                return render_one(frames, options)

            if body.get("async"):
                return _submit_artifact(
                    request, kind, frames, options, render, media_type=media_type, suffix=suffix
                )
            return _serve_artifact(
                kind, frames, options, render, media_type=media_type, suffix=suffix
            )

        @app.post(
            "/artifacts/change",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _binary_response("image/png", "The change composite."),
                202: _JOB_RESPONSE,
            },
        )
        def post_change(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            return _composite(request, body, "change", change_frames, renderers.change)

        @app.post(
            "/artifacts/timescan",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _binary_response("image/png", "The temporal-statistics composite."),
                202: _JOB_RESPONSE,
            },
        )
        def post_timescan(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            return _composite(request, body, "timescan", timescan_frames, renderers.timescan)

        @app.post(
            "/artifacts/swipe",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _binary_response("text/html", "A self-contained before/after swipe map page."),
                202: _JOB_RESPONSE,
            },
        )
        def post_swipe(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            return _composite(
                request,
                body,
                "swipe",
                swipe_frames,
                renderers.swipe,
                media_type="text/html; charset=utf-8",
                suffix="html",
            )

        @app.post(
            "/artifacts/stats",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _json_response(
                    "StackStats", "The datacube measurement (`docs/schemas/stack-stats`)."
                ),
                202: _JOB_RESPONSE,
            },
        )
        def post_stats(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            """Measure how much a site changed across its passes, as JSON.

            The numeric sibling of ``/artifacts/change``: same request shape
            (``ids`` or a ``bbox``/``datetime`` query, ``async`` opt-in, the same
            content-addressed cache), but the answer is
            :func:`~umbra_py.load.stack_stats`' reduction rather than a picture --
            per-pass decibel statistics, the signed change against the previous
            pass, how much ground moved past ``change_threshold_db`` (in km²,
            since the grid defaults to the site's UTM zone), and with
            ``"blocks": N`` an N x N breakdown saying *which part* of the site
            moved and between which two passes -- plus, with
            ``"block_series": true``, the whole pass-to-pass sequence each of
            those peaks was picked from.

            ``"windowed": true`` measures the cube window by window instead of a
            slice per pass, so a long or sharp series never has a whole pass
            resident. It needs an instance started with ``--stack-lazy
            --stack-chunk-size N`` (otherwise a ``400`` naming the flag), and it
            is the one option that changes an answer rather than the memory
            spent on it: every count, mean, standard deviation and change figure
            stays exact, while each pass's median/p5/p95 become histogram
            estimates. The response says which is which -- ``quantile_method``
            and ``quantile_bin_db`` appear exactly when they are estimates --
            and it caches apart from the exact reduction.

            ``"speckle_filter": "boxcar" | "lee"`` (with an optional odd
            ``"speckle_window"``, default 5) averages **speckle** down on the
            shared grid before anything is measured -- the correction with the
            largest effect on a per-cell decibel delta, since single-look speckle
            scatters as widely as its own mean. ``boxcar`` is the multilook;
            ``lee`` averages only where a window is no more variable than speckle
            alone explains, so edges and bright points survive. What it spends is
            resolution, which the response's ``caveats`` state rather than the
            request having to remember. Every instance honours it -- a chunked
            build reads each window with a half-window halo -- so it composes
            with ``"windowed": true`` rather than excluding it.
            """
            return _composite(
                request,
                body,
                "stats",
                stats_frames,
                renderers.stats,
                media_type="application/json",
                suffix="json",
                make_options=stats_options,
            )

        @app.post(
            "/artifacts/provenance",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _json_response(
                    "StackProvenance",
                    "What the selection's sources say their pixel values are, and whether "
                    "the series stacks (`docs/schemas/stack-provenance`). A mix is a 200: "
                    "reporting it is the point.",
                )
            },
        )
        def post_provenance(body: dict[str, Any] = Body(default={})) -> Response:
            """Say whether a selection is one measurement, before stats is spent on it.

            ``/artifacts/stats`` refuses a selection whose rasters were made by
            different ``umbra convert`` settings -- a calibrated pass
            differenced against an uncalibrated one puts the difference between
            two *conversions* on the time axis and reports it as change on the
            ground. That refusal arrives as a ``400`` after the request has been
            spent, and its advice ("use only the acquisitions that share one")
            names a subset it cannot identify. This is the same question asked
            first, and answered with that subset.

            Send the body you would send to ``/artifacts/stats`` -- the same
            ``ids`` or ``bbox``/``datetime`` query, normalised by the same
            :func:`stats_options`, vetted into the same frames by
            :func:`stats_frames` -- so the answer is about the selection that
            endpoint would actually stack, and a bad option costs a ``400``
            here rather than there. The body's stacking options are read only
            for ``asset``: which raster to read the record from is the one
            choice that changes the answer.

            The response is :meth:`~umbra_py.load.StackProvenance.to_dict`,
            byte-for-byte the document ``umbra stack --provenance --json``
            emits: ``agrees``, the ``groups`` largest-first with the ``hrefs``
            to re-run on, ``shared`` when they agree, ``refusal`` (verbatim what
            ``/artifacts/stats`` would have answered) when they do not, and
            ``unreadable`` for sources that could not be opened at all.

            A mixed selection is a ``200``, not a ``400``: reporting the mix is
            what was asked for. Only a selection that could not be measured at
            all -- fewer than two passes, or mixed polarizations -- is a
            ``400``, because there is no stack to preflight.

            It costs the header reads a stack pays for anyway (kilobytes per
            pass by range request, no pixels) and nothing after them, so it is
            answered synchronously and is not cached: a re-converted source is
            exactly the case where a content-addressed answer would be stale,
            and a question asked to avoid spending a render should not need a
            job document of its own.
            """
            items = _resolve_for_composite(body)
            try:
                frames = stats_frames(items)
                options = stats_options(body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                # Deliberately not routed through ``renderers``: the report is
                # not a render, and its whole claim is that the verdict is
                # ``to_stack``'s own. An injectable provenance would be exactly
                # the second opinion that construction exists to rule out.
                report = stack_provenance(frames, asset=options["asset"])
            except MissingDependencyError as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(content=report.to_dict())

        @app.post(
            "/artifacts/narrate",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: {
                    "description": (
                        "A vision-language narration of the change between two passes, grounded "
                        "in the deterministic per-block dB grid and the speckle detection floor."
                    ),
                    "content": {"application/json": {}},
                },
                403: {
                    "description": "The requested scene is outside the instance's narration area."
                },
                429: {
                    "description": (
                        "The instance's daily (or per-client) narration budget is exhausted."
                    )
                },
                501: {"description": "Narration is not enabled on this instance."},
            },
        )
        def post_narrate(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            """Explain *what changed* between two passes in plain language (a model call).

            The interpretive sibling of ``/artifacts/change``: same request shape
            (``ids`` or a ``bbox``/``datetime`` query, the same content-addressed
            cache), but the answer is a validated
            :class:`~umbra_py.narrate.ChangeNarration` -- a short summary, the
            concrete changes the numbers support, a confidence and SAR-specific
            caveats -- grounded in the deterministic per-block decibel grid *and*
            the speckle detection floor, so the model reports change only where it
            stands clear of interference. The determinism boundary
            (``docs/STRATEGY.md`` §7) holds: the picture and the numbers are
            computed offline, and the model only interprets them.

            **The two capabilities compose.** Two or three passes are narrated
            directly. A **longer series is scanned first**
            (:func:`~umbra_py.load.best_change_interval`) and the pair whose
            change stands furthest clear of the speckle floor is the one narrated
            -- a number picks the frames, never the model -- and the chosen
            interval rides out on the response's ``selected_interval``.

            This is the one endpoint that spends money per call, so it is
            **opt-in** (a ``501`` unless the instance was started with
            ``umbra serve --narrate`` and a model API key) and **guarded**: the
            result is cached like every other artifact, so a repeat request costs
            nothing and never calls the model; and a per-day budget
            (``--narrate-daily-limit N``) caps the calls that actually reach the
            model, answering ``429`` when today's cap is reached. The model is the
            instance's, chosen once by the operator who holds the key -- it is not
            a request field, so no client can point one instance at another's
            model or spend.
            """
            if renderers.narrate is None:
                raise HTTPException(
                    status_code=501,
                    detail=(
                        "Change narration is not enabled on this instance. Start the "
                        "server with 'umbra serve --narrate' and a model API key "
                        "(ANTHROPIC_API_KEY, OPENROUTER_API_KEY, or OPENAI_API_KEY)."
                    ),
                )
            from .narrate import NarrateError  # noqa: PLC0415

            narrate_render = renderers.narrate
            items = _resolve_for_composite(body)
            try:
                frames = stats_frames(items)
                options = narrate_options(body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            # Policy first: a (possibly public) narrate endpoint is an
            # unauthenticated proxy over the operator's model budget, so an
            # instance may bound *which* scenes it will spend on. A frame outside
            # the allowed area is refused before the cache, the budgets and the
            # model -- it is not a request this instance answers at all.
            outside = narration_allowlist.disallowed(frames)
            if outside is not None:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Acquisition {outside.id!r} is outside this instance's "
                        "narration area. This endpoint is bounded to a curated region; "
                        "narrate a site within it."
                    ),
                )

            # A cache hit costs no model call, so it is served before -- and
            # without touching -- either budget. Only a miss is about to spend.
            key = artifact_cache_key("narrate", [it.id for it in frames], options)
            cached = _cache_file(key, "json")
            if cached.exists():
                return Response(
                    content=cached.read_bytes(),
                    media_type="application/json",
                    headers={"X-Umbra-Cache": "hit"},
                )
            # Fairness before the wallet: the per-client cap gates a single caller
            # first, so one client cannot burst through the whole instance's daily
            # budget. Keyed by bearer token, else peer address, and counted only
            # here -- on a miss that is about to spend.
            if not client_narration_budget.reserve(client_identity(request)):
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "You have reached this instance's per-client daily narration "
                        "limit. Try again tomorrow, or ask the operator to raise "
                        "--narrate-client-limit."
                    ),
                )
            if not narration_budget.reserve():
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "This instance's daily narration budget is exhausted. Try again "
                        "tomorrow, or ask the operator to raise --narrate-daily-limit."
                    ),
                )
            try:
                return _serve_artifact(
                    "narrate",
                    frames,
                    options,
                    lambda: narrate_render(frames, options),
                    media_type="application/json",
                    suffix="json",
                )
            except NarrateError as exc:
                # The model failed or returned something unparseable: an upstream
                # problem, not the client's request -- 502 rather than 400/500.
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        @app.get(
            "/jobs/{job_id}",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: _json_response(
                    "RenderJob", "The job's current state (`docs/schemas/render-job`)."
                )
            },
        )
        def get_job(job_id: str, request: Request) -> JSONResponse:
            job = job_store.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"No job {job_id!r}")
            return JSONResponse(content=job_to_dict(job, str(request.base_url)))

        @app.get(
            "/jobs/{job_id}/result",
            tags=["Artifacts"],
            response_class=Response,
            responses={
                200: {
                    # Which of the three it is depends on the job's ``kind``, so
                    # the honest description is all of them: a client reads the
                    # job document's ``result`` link ``type`` to know which.
                    "description": "The finished artifact, in the media type the job's "
                    "`result` link names.",
                    "content": {"image/png": {}, "text/html": {}, "application/json": {}},
                }
            },
        )
        def get_job_result(job_id: str) -> Response:
            job = job_store.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"No job {job_id!r}")
            if job.status in (JOB_QUEUED, JOB_RUNNING):
                raise HTTPException(
                    status_code=409,
                    detail=f"Job {job_id!r} is {job.status}; poll /jobs/{job_id} until succeeded.",
                )
            if job.status == JOB_FAILED:
                raise HTTPException(
                    status_code=job.error_status, detail=job.error or "render failed"
                )
            path = _cache_file(job.cache_key, job.suffix)
            if not path.exists():  # succeeded but the cached bytes were evicted
                raise HTTPException(
                    status_code=404, detail=f"Result for job {job_id!r} is no longer cached."
                )
            return Response(
                content=path.read_bytes(),
                media_type=job.media_type,
                headers={"X-Umbra-Cache": "hit"},
            )

    # The routes above reference their published contracts by ``$ref``; this is
    # what puts the contracts themselves in the document they point into. The
    # always-mounted ``GET /sites`` needs ``SiteCoverage`` in every instance's
    # document (:func:`core_openapi_components`); the ``/artifacts/*`` contracts
    # are added only when those routes are mounted, since a component nothing
    # references would be a shape this instance does not emit. FastAPI caches the
    # generated document on the app, so merging into the object it returns is
    # enough -- and idempotent.
    generate_openapi = app.openapi

    def _openapi() -> dict[str, Any]:
        document = generate_openapi()
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        schemas.update(core_openapi_components())
        if artifacts:
            schemas.update(openapi_components())
        return document

    app.openapi = _openapi

    # Catch-all last so FastAPI's ``/search`` / ``/healthz`` / ``/docs`` win;
    # unmatched ``POST /mcp`` falls through to the MCP Starlette app.
    if mcp_asgi is not None:
        app.mount("/", mcp_asgi)

    return app


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    index_path: str | os.PathLike | None = None,
    live: bool = False,
    artifacts: bool = True,
    stack_execution: StackExecution | None = None,
    narrator: Any | None = None,
    narration_daily_limit: int | None = None,
    narration_client_limit: int | None = None,
    narration_allow_bbox: BBox | None = None,
    cache_dir: str | os.PathLike | None = None,
    log_level: str = "info",
    mcp: bool = False,
    public: bool = False,
    rate_limit: int | None = None,
    proxy_headers: bool = False,
) -> None:
    """Build the app and run it with uvicorn (blocking). Requires ``serve``."""
    _require_serve()
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        raise MissingDependencyError(
            "The STAC API server needs the 'serve' extra. Install it with:\n"
            "    pip install 'umbra-py[serve]'",
            hint="pip install 'umbra-py[serve]'",
        ) from exc

    if public:
        secrets = public_secret_names()
        if secrets:
            raise ValueError(
                "A public instance must not hold "
                + ", ".join(secrets)
                + ". Unset them (Canopy is the commercial archive; model keys "
                "would spend on every describe/narrate tool call)."
            )
        proxy_headers = True

    app = build_app(
        index_path,
        live=live,
        artifacts=artifacts,
        stack_execution=stack_execution,
        narrator=narrator,
        narration_daily_limit=narration_daily_limit,
        narration_client_limit=narration_client_limit,
        narration_allow_bbox=narration_allow_bbox,
        cache_dir=cache_dir,
        mcp=mcp,
        public=public,
        rate_limit=rate_limit,
    )
    run_kw: dict[str, Any] = {"host": host, "port": port, "log_level": log_level}
    if proxy_headers:
        # Railway (and any TLS-terminating proxy) is the socket peer; without
        # this the per-client rate limit collapses to one bucket. Honouring
        # forwarded-for from *untrusted* clients would make the cap evadable,
        # so this is opt-in -- ``--public`` turns it on because that host is
        # always behind a proxy.
        run_kw["proxy_headers"] = True
        run_kw["forwarded_allow_ips"] = "*"
    uvicorn.run(app, **run_kw)
