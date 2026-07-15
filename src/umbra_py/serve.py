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

import itertools
import os
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import ATTRIBUTION, DATA_LICENSE, PRODUCT_TYPE_EXPLANATIONS
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
            "    pip install 'umbra-py[serve]'"
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


# --------------------------------------------------------------------------
# STAC document builders (deterministic; unit-testable without a server)
# --------------------------------------------------------------------------


def _link(rel: str, href: str, *, type: str = "application/json", **extra: Any) -> dict[str, Any]:
    link = {"rel": rel, "href": href, "type": type}
    link.update(extra)
    return link


def landing_page(base_url: str) -> dict[str, Any]:
    """The STAC API landing page (a STAC ``Catalog`` with conformance + links)."""
    base = base_url.rstrip("/")
    geojson = "application/geo+json"
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
        "links": [
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
        ],
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
    start: date | None = None,
    end: date | None = None,
    ids: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[UmbraItem], bool]:
    """Execute a paged search against a ``source`` (anything with ``.search``).

    Returns ``(page_items, has_next)``. ``ids``, when given, filters by STAC
    item id in this layer (the index search filters by bbox/date/area, not id).
    Paging is deterministic offset paging over the source's stable ordering: we
    request one extra item to decide whether a ``next`` link is warranted.
    """
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    # Bound the work when we can. With an id filter we can't cap at the source
    # (an id can appear anywhere in the ordering), so scan and filter here.
    cap = None if ids else offset + limit + 1
    stream = source.search(bbox=bbox, start=start, end=end, limit=cap)
    if ids:
        wanted = set(ids)
        stream = (it for it in stream if it.id in wanted)
    window = list(itertools.islice(stream, offset, offset + limit + 1))
    has_next = len(window) > limit
    return window[:limit], has_next


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
# FastAPI application factory
# --------------------------------------------------------------------------


def build_app(index_path: str | os.PathLike | None = None, *, live: bool = False) -> FastAPI:
    """Construct the FastAPI STAC API application.

    ``index_path`` selects the catalog index (default: the shared
    :func:`~umbra_py.default_index_path`); ``live=True`` serves from a live S3
    walk instead. A fresh backend is opened and closed per request so the app
    is safe under FastAPI's thread pool. Requires the ``serve`` extra.
    """
    fastapi = _require_serve()
    from fastapi import Body, HTTPException, Query, Request
    from fastapi.responses import JSONResponse

    # This module uses ``from __future__ import annotations``, so the route
    # handlers' annotations are strings that FastAPI resolves against the
    # module globals. ``Request``/``JSONResponse`` are imported lazily inside
    # this factory (to keep the fastapi import behind the ``serve`` extra), so
    # publish them into the module namespace for that resolution to succeed.
    globals().update(Request=Request, JSONResponse=JSONResponse)

    app = fastapi.FastAPI(
        title="Umbra Open Data STAC API",
        description=(
            "Read-only STAC API over Umbra's open SAR archive, served by "
            "umbra-py from a local catalog index."
        ),
        version=STAC_VERSION,
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
        return landing_page(str(request.base_url))

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
        start: date | None,
        end: date | None,
        ids: list[str] | None,
        limit: int,
        offset: int,
        self_href: str,
    ) -> JSONResponse:
        source = _open()
        try:
            page, has_next = run_search(
                source, bbox=bbox, start=start, end=end, ids=ids, limit=limit, offset=offset
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
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        token: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        if collection_id != COLLECTION_ID:
            raise HTTPException(status_code=404, detail=f"No collection {collection_id!r}")
        try:
            parsed_bbox = parse_bbox(bbox)
            start, end = parse_datetime(datetime)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _do_search(
            request,
            bbox=parsed_bbox,
            start=start,
            end=end,
            ids=None,
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
            page, _ = run_search(source, ids=[item_id], limit=1)
        finally:
            _close(source)
        if not page:
            raise HTTPException(status_code=404, detail=f"No item {item_id!r}")
        return JSONResponse(
            content=item_to_stac(page[0], str(request.base_url)), media_type=geojson
        )

    @app.get("/search", tags=["STAC"])
    def get_search(
        request: Request,
        bbox: str | None = Query(default=None),
        datetime: str | None = Query(default=None),
        ids: str | None = Query(default=None, description="Comma-separated item ids"),
        collections: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        token: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        _check_collections(collections.split(",") if collections else None)
        try:
            parsed_bbox = parse_bbox(bbox)
            start, end = parse_datetime(datetime)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        id_list = [i for i in ids.split(",") if i] if ids else None
        return _do_search(
            request,
            bbox=parsed_bbox,
            start=start,
            end=end,
            ids=id_list,
            limit=limit,
            offset=token,
            self_href=str(request.url),
        )

    @app.post("/search", tags=["STAC"])
    def post_search(request: Request, body: dict[str, Any] = Body(default={})) -> JSONResponse:
        _check_collections(body.get("collections"))
        try:
            parsed_bbox = parse_bbox(body.get("bbox"))
            start, end = parse_datetime(body.get("datetime"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ids = body.get("ids")
        limit = _clamp_limit(body.get("limit"))
        offset = int(body.get("token") or 0)
        base = str(request.base_url).rstrip("/")
        source = _open()
        try:
            page, has_next = run_search(
                source,
                bbox=parsed_bbox,
                start=start,
                end=end,
                ids=list(ids) if ids else None,
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

    return app


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    index_path: str | os.PathLike | None = None,
    live: bool = False,
    log_level: str = "info",
) -> None:
    """Build the app and run it with uvicorn (blocking). Requires ``serve``."""
    _require_serve()
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        raise MissingDependencyError(
            "The STAC API server needs the 'serve' extra. Install it with:\n"
            "    pip install 'umbra-py[serve]'"
        ) from exc

    app = build_app(index_path, live=live)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
