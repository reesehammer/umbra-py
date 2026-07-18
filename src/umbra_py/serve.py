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
  ``umbra-mcp`` server: same index underneath, a different front door.

On top of *discovery* the server also renders *artifacts on demand*, so a
front end (or an agent) can trigger the library's visual products over **any**
site straight from HTTP, not just a curated set baked at build time
(``DEMO_APP_GAPS.md`` R4 / Path B):

- ``GET  /artifacts/quicklook/{item_id}.png`` -- one acquisition's SAR quicklook;
- ``POST /artifacts/change``   -- a 2--3 date change composite over a query;
- ``POST /artifacts/timescan`` -- a temporal-statistics composite over a series;
- ``POST /artifacts/swipe``    -- an interactive before/after swipe map (HTML).

These wrap the existing :mod:`umbra_py.viz` functions unchanged and cache every
result to disk keyed by its inputs, so a repeat request is a file read (closing
the "no artifact caching" gap for these endpoints). Two properties keep them in
the package's grain: the renderers are **injectable** (``build_app(...,
renderers=...)``), so the routes are unit-testable in the core install with no
network and no ``viz`` extra; and they are opt-out (``--no-artifacts``) for a
public instance that wants to bound COG-streaming egress.

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
``pip install 'umbra-py[serve]'``).
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._geometry import Geometry, parse_geometry
from .constants import (
    ATTRIBUTION,
    DATA_LICENSE,
    PRODUCT_ASSETS,
    PRODUCT_TYPE_EXPLANATIONS,
)
from .exceptions import MissingDependencyError
from .index import CatalogIndex, default_index_path
from .models import BBox, UmbraItem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

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
    # The Query extension, mapped to the index's two Umbra-specific filters
    # (``product_types`` and free-text ``area``); see :func:`parse_query`.
    "https://api.stacspec.org/v1.0.0/item-search#query",
)

#: Default page size, and the ceiling a client can request via ``limit``.
DEFAULT_LIMIT = 10
MAX_LIMIT = 10_000


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


def parse_query(query: Any) -> tuple[list[str] | None, str | None]:
    """Parse a STAC *Query* extension object into ``(product_types, area)``.

    The Umbra index filters by two fields that aren't STAC core properties:
    ``product_types`` (which asset a scene carries) and free-text ``area`` (a
    task/site substring). This maps the Query extension onto them:

    - ``{"product_types": {"in": ["GEC", "SICD"]}}`` (or a bare list/string)
    - ``{"area": {"like": "Beet Piler"}}`` (``eq`` is treated the same; the
      index match is already a case-insensitive substring), or a bare string.

    Any other property, or an unsupported operator, is a :class:`ValueError` so
    a client's filter is never silently dropped (which would return items the
    client asked to exclude). Returns ``(None, None)`` for an empty query.
    """
    if query is None:
        return (None, None)
    if not isinstance(query, dict):
        raise ValueError("query must be an object")

    supported = {"product_types", "area"}
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

    product_types = None
    if "product_types" in query:
        spec = _scalar("product_types", query["product_types"], ("in", "eq"))
        product_types = parse_product_types(spec)

    area = None
    if "area" in query:
        raw = _scalar("area", query["area"], ("like", "eq"))
        area = str(raw).strip() or None

    return (product_types, area)


# --------------------------------------------------------------------------
# STAC document builders (deterministic; unit-testable without a server)
# --------------------------------------------------------------------------


def _link(rel: str, href: str, *, type: str = "application/json", **extra: Any) -> dict[str, Any]:
    link = {"rel": rel, "href": href, "type": type}
    link.update(extra)
    return link


def landing_page(base_url: str, *, artifacts: bool = False) -> dict[str, Any]:
    """The STAC API landing page (a STAC ``Catalog`` with conformance + links).

    When ``artifacts`` is true the returned links also advertise the on-demand
    render endpoints (``/artifacts/...``) so a client can discover them without
    reading the OpenAPI document.
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
        ]
    return {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": COLLECTION_ID,
        "title": "Umbra Open Data STAC API",
        "description": (
            "A read-only STAC API over Umbra's open SAR archive, served by "
            "umbra-py from a local catalog index. Umbra publishes a static STAC "
            "catalog and no search API; this façade restores /search for the "
            f"standard STAC tooling. Data is {DATA_LICENSE}: {ATTRIBUTION}"
        ),
        "conformsTo": list(CONFORMANCE_CLASSES),
        "links": links,
    }


def conformance() -> dict[str, Any]:
    """The ``/conformance`` response."""
    return {"conformsTo": list(CONFORMANCE_CLASSES)}


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
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[UmbraItem], bool]:
    """Execute a paged search against a ``source`` (anything with ``.search``).

    Returns ``(page_items, has_next)``. ``ids``, when given, filters by STAC
    item id in this layer (the index search filters by bbox/date/area, not id).
    ``product_types``, ``area`` and ``fuzzy`` are the Query-extension filters,
    pushed down to the backend's ``search`` (both :class:`CatalogIndex` and the
    live :class:`~umbra_py.catalog.UmbraCatalog` accept them). Paging is
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


@dataclass(frozen=True)
class Renderers:
    """The three render functions the artifact endpoints call, as PNG bytes.

    Injecting this (rather than importing :mod:`umbra_py.viz` directly in the
    routes) is what keeps the endpoints unit-testable in the core install: a
    test passes fakes that return a fixed PNG with no network and no ``viz``
    extra, while :func:`default_renderers` wires the real, lazily-imported
    compositors. Each callable takes the resolved items and a normalised
    options mapping (``asset`` / ``max_size`` / ``db``) and returns PNG bytes.
    """

    quicklook: Callable[[UmbraItem, Mapping[str, Any]], bytes]
    change: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]
    timescan: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]
    #: Unlike the three PNG compositors, ``swipe`` returns a self-contained HTML
    #: page (:func:`viz.swipe_map`), so its bytes are UTF-8 HTML, not a PNG.
    swipe: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes]


def _png_bytes(image: Any) -> bytes:
    """Encode a ``PIL.Image`` to PNG bytes."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def default_renderers() -> Renderers:
    """The production renderers, backed by :mod:`umbra_py.viz` (``viz`` extra).

    Imports are deferred to call time so building the app -- and importing this
    module -- never needs the heavy raster stack; only an actual render request
    pulls it in (and a missing extra surfaces as a clean error the route maps to
    HTTP 501).
    """

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

    return Renderers(quicklook=quicklook, change=change, timescan=timescan, swipe=swipe)


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


# --------------------------------------------------------------------------
# Async render jobs (202 Accepted + poll; the disk cache is the result store)
# --------------------------------------------------------------------------
#
# The composite endpoints render synchronously by default -- a downsampled
# overview returns in seconds, which is the honest first slice. But a large
# ``max_size`` or a long timescan can take tens of seconds, and a synchronous
# request holds a worker for the whole render. The productized shape
# (``DEMO_APP_GAPS.md`` Path B step 2 / ``TODO.md``) is a small job queue: a
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
# FastAPI application factory
# --------------------------------------------------------------------------


def build_app(
    index_path: str | os.PathLike | None = None,
    *,
    live: bool = False,
    artifacts: bool = True,
    renderers: Renderers | None = None,
    cache_dir: str | os.PathLike | None = None,
    job_executor: Any | None = None,
) -> FastAPI:
    """Construct the FastAPI STAC API application.

    ``index_path`` selects the catalog index (default: the shared
    :func:`~umbra_py.default_index_path`); ``live=True`` serves from a live S3
    walk instead. A fresh backend is opened and closed per request so the app
    is safe under FastAPI's thread pool.

    When ``artifacts`` is true (the default) the on-demand render endpoints
    (``/artifacts/quicklook/{id}.png``, ``POST /artifacts/change``,
    ``POST /artifacts/timescan``, ``POST /artifacts/swipe``) are mounted, along
    with the async job endpoints (``GET /jobs/{id}``, ``GET /jobs/{id}/result``)
    the composite renders use when a request opts in to ``"async": true``.
    ``renderers`` overrides the render functions (defaults to
    :func:`default_renderers`, which needs the ``viz`` extra at request time);
    ``cache_dir`` overrides where rendered PNGs are cached (defaults to
    :func:`default_artifact_cache_dir`). ``job_executor`` overrides the
    background runner for async jobs (anything with ``submit(fn)`` / ``shutdown``,
    e.g. a :class:`~concurrent.futures.ThreadPoolExecutor`); it defaults to a
    small thread pool, and a test can inject :class:`_InlineJobExecutor` to run
    jobs synchronously. Requires the ``serve`` extra.
    """
    fastapi = _require_serve()
    from fastapi import Body, HTTPException, Query, Request, Response
    from fastapi.responses import JSONResponse

    # This module uses ``from __future__ import annotations``, so the route
    # handlers' annotations are strings that FastAPI resolves against the
    # module globals. ``Request``/``JSONResponse``/``Response`` are imported
    # lazily inside this factory (to keep the fastapi import behind the
    # ``serve`` extra), so publish them into the module namespace for that
    # resolution to succeed.
    globals().update(Request=Request, JSONResponse=JSONResponse, Response=Response)

    if renderers is None:
        renderers = default_renderers()
    cache_path = Path(cache_dir) if cache_dir is not None else default_artifact_cache_dir()

    # Async render jobs: an in-memory registry + a background runner. Both are
    # created only when artifacts are enabled; the executor is injectable so a
    # test can drive the queue deterministically (see ``_InlineJobExecutor``).
    job_store = JobStore()
    if job_executor is None and artifacts:
        from concurrent.futures import ThreadPoolExecutor

        job_executor = ThreadPoolExecutor(
            max_workers=ARTIFACT_JOB_WORKERS, thread_name_prefix="umbra-render"
        )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        # Drain the background render pool on shutdown so a stopping server does
        # not hang on (or leak) in-flight renders.
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
    )

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
        return landing_page(str(request.base_url), artifacts=artifacts)

    @app.get("/conformance", tags=["STAC"])
    def get_conformance() -> dict[str, Any]:
        return conformance()

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
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        token: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        if collection_id != COLLECTION_ID:
            raise HTTPException(status_code=404, detail=f"No collection {collection_id!r}")
        try:
            parsed_bbox = parse_bbox(bbox)
            start, end = parse_datetime(datetime)
            wanted_products = parse_product_types(product_types)
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
            query_products, query_area = parse_query(body.get("query"))
            top_products = parse_product_types(body.get("product_types"))
            wanted_products = top_products if top_products is not None else query_products
            top_area = body.get("area")
            area = str(top_area).strip() if top_area not in (None, "") else query_area
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
        endpoint reports as ``501`` (mirroring the synchronous path); any other
        error becomes a ``500``. Exceptions never escape -- they are the job's
        recorded outcome, not a crash of the worker thread.
        """
        job_store.mark_running(job)
        try:
            payload = render()
        except MissingDependencyError as exc:
            job_store.mark_failed(job, str(exc), 501)
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

        @app.get("/artifacts/quicklook/{item_id}.png", tags=["Artifacts"])
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

        def _composite(
            request: Request,
            body: Mapping[str, Any],
            kind: str,
            pick_frames: Callable[[list[UmbraItem]], list[UmbraItem]],
            render_one: Callable[[Sequence[UmbraItem], Mapping[str, Any]], bytes],
            *,
            media_type: str = "image/png",
            suffix: str = "png",
        ) -> Response:
            """Resolve frames for a composite and render it sync or async.

            Frame resolution and validation are always synchronous, so a bad
            request (too few acquisitions, malformed bbox) is a fast ``400`` and
            never becomes a doomed background job. Only the render itself is
            deferred, and only when the request opts in to ``"async": true`` --
            in which case the caller gets a job document instead of the artifact.
            """
            items = _resolve_for_composite(body)
            try:
                frames = pick_frames(items)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            options = artifact_options(body)

            def render() -> bytes:
                return render_one(frames, options)

            if body.get("async"):
                return _submit_artifact(
                    request, kind, frames, options, render, media_type=media_type, suffix=suffix
                )
            return _serve_artifact(
                kind, frames, options, render, media_type=media_type, suffix=suffix
            )

        @app.post("/artifacts/change", tags=["Artifacts"])
        def post_change(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            return _composite(request, body, "change", change_frames, renderers.change)

        @app.post("/artifacts/timescan", tags=["Artifacts"])
        def post_timescan(request: Request, body: dict[str, Any] = Body(default={})) -> Response:
            return _composite(request, body, "timescan", timescan_frames, renderers.timescan)

        @app.post("/artifacts/swipe", tags=["Artifacts"])
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

        @app.get("/jobs/{job_id}", tags=["Artifacts"])
        def get_job(job_id: str, request: Request) -> JSONResponse:
            job = job_store.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"No job {job_id!r}")
            return JSONResponse(content=job_to_dict(job, str(request.base_url)))

        @app.get("/jobs/{job_id}/result", tags=["Artifacts"])
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

    return app


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    index_path: str | os.PathLike | None = None,
    live: bool = False,
    artifacts: bool = True,
    cache_dir: str | os.PathLike | None = None,
    log_level: str = "info",
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

    app = build_app(index_path, live=live, artifacts=artifacts, cache_dir=cache_dir)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
