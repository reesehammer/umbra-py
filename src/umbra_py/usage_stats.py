"""Public usage totals for ``GET /stats``.

The hosted API answers a docs-site panel: how many real searches it has
served, how big the loaded index is, and which coarse places people
actually ask about. The numbers are a social-proof counter, so they start
at zero when the file is first created and are never backfilled.

What is counted is narrow on purpose. The first page of a successful
``GET`` or ``POST /search`` (no paging token, or a token of zero), and a
successful MCP search tool (``search_catalog``, ``find_repeat_sites``,
``find_similar``, ``find_similar_text``, ``watch_site``), count. A later
page is the same search. Probes, health checks, unknown paths, ``HEAD`` /
``OPTIONS``, rate-limit and error responses, smoke traffic (including
this project's replace-crawler canary), metadata tools, and known bot or
uptime user agents do not. A request that does not clearly qualify is
dropped.

What is stored is narrower still. The file beside the catalog index holds
the total, the ``counting_since`` timestamp, and a per-place distinct-client
*count*. A client is a salted hash of the address the rate limiter already
uses. The salt is random, lives only in memory, and rotates each UTC day,
so the file cannot be turned back into an address. Raw query strings, user
agents, and the hash itself are not written. Place text is reduced to a
city and country, or to a region and country when the geocoder resolved a
venue, or to a baked index label near the query, before it is kept.

Recording is an in-memory update under one lock. A background thread
writes the file; the request path does not. A stats failure is logged and
swallowed. If the volume cannot be written, ``/stats`` still answers from
memory. The recorder is mounted only by ``umbra serve --public``.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from ._mcp_log import (
    _argument_mapping,
    _header_value,
    _initialize_client,
    _params_name_and_args,
    _request_host,
    _result_is_error,
)

logger = logging.getLogger(__name__)

#: How long a computed ``GET /stats`` document is reused. The response
#: ``Cache-Control`` max-age is this same window.
STATS_CACHE_TTL_S = 3600.0
STATS_CACHE_CONTROL = f"public, max-age={int(STATS_CACHE_TTL_S)}"

#: A place is published only when at least this many distinct clients reached
#: it on one UTC day (the high-water mark of that daily count).
MIN_DISTINCT_CLIENTS = 3
TOP_PLACES = 10
#: Cap on stored place names. On a UTC day change, names below
#: :data:`MIN_DISTINCT_CLIENTS` are dropped and anything still over this
#: cap loses the lowest counts.
MAX_PLACES = 2_000

#: Nearest baked place label, in degrees, that may name a bbox. About 111 km
#: at the equator: a metro, not a street.
PLACE_MATCH_DEGREES = 1.0
#: A box wider or taller than this is not named. A continent is not a city.
PLACE_MAX_SPAN_DEGREES = 5.0

#: In-memory increments between wakes of the flush thread. The request
#: path only sets an event. Shutdown writes whatever is left.
FLUSH_EVERY = 50
FLUSH_INTERVAL_S = 30.0

#: Scope key holding the already-coarsened place for this one request.
USAGE_PLACE_KEY = "umbra.usage_place"
#: Scope key the search handler sets once it knows the page offset.
#: True is the first page. Absent, the query string is what decides.
USAGE_FIRST_PAGE = "umbra.usage_first_page"
_UNMARKED = object()

#: Query keys that mean "another page of the same search".
_PAGE_KEYS = frozenset({"token", "offset", "page", "next"})

#: Nominatim ``addresstype`` / ``type`` / ``category`` values whose own name
#: is a settlement or an admin area. Anything else is a venue: the label's
#: first segment is the place's name, not a city.
_SETTLEMENT_KINDS = frozenset(
    {
        "administrative",
        "borough",
        "boundary",
        "city",
        "continent",
        "country",
        "county",
        "district",
        "hamlet",
        "island",
        "locality",
        "municipality",
        "neighborhood",
        "neighbourhood",
        "place",
        "postcode",
        "province",
        "quarter",
        "region",
        "state",
        "state_district",
        "suburb",
        "town",
        "village",
    }
)
_CITY_ADDRESS_KEYS = ("city", "town", "village", "municipality", "hamlet", "suburb", "county")
_STATE_ADDRESS_KEYS = ("state", "region", "state_district", "province")

#: File schema. A different version is ignored and the counter starts over
#: rather than being interpreted as a count it is not.
_FILE_VERSION = 1
_MAX_FILE_BYTES = 1_000_000

#: User-agent substrings that are never counted, matched case-insensitively.
#: Our own probes are listed by the exact agent they send.
DEFAULT_IGNORED_UA: tuple[str, ...] = (
    "umbra-py-healthz-monitor",
    "umbra-py-mcp-smoke",
    "uptimerobot",
    "pingdom",
    "statuscake",
    "better uptime",
    "betterstack",
    "uptime-kuma",
    "uptime",
    "freshping",
    "site24x7",
    "kube-probe",
    "googlehc",
    "elb-healthchecker",
    "healthcheck",
    "bot",
    "spider",
    "crawler",
    "slurp",
)

#: MCP tools that search the catalog. Metadata, renders, and stamps do not.
_SEARCH_TOOLS = frozenset(
    {
        "search_catalog",
        "find_repeat_sites",
        "find_similar",
        "find_similar_text",
        "watch_site",
    }
)

_SMOKE_VALUES = frozenset({"1", "true", "yes", "on"})
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

#: A segment that is a street rather than a city. ``st`` as the *first* token
#: ("St. Louis") is a saint, not a street.
_ADDRESS_WORDS = frozenset(
    {
        "street",
        "st",
        "avenue",
        "ave",
        "road",
        "rd",
        "lane",
        "ln",
        "drive",
        "dr",
        "boulevard",
        "blvd",
        "court",
        "ct",
        "highway",
        "hwy",
        "suite",
        "apt",
        "apartment",
        "floor",
        "fl",
        "unit",
    }
)

_EMPTY_FACTS: dict[str, Any] = {
    "scenes_indexed": None,
    "thumbnails": None,
    "index_built_at": None,
}

_installed: UsageStats | None = None
_installed_lock = threading.Lock()


def install_recorder(stats: UsageStats | None) -> None:
    """Publish the process recorder the MCP middleware reads.

    ``GET /search`` counts through the middleware that holds the instance.
    MCP tool calls are dispatched inside the SDK, which only has this
    module-level handle. ``None`` clears it.
    """
    global _installed
    with _installed_lock:
        _installed = stats


def current_recorder() -> UsageStats | None:
    """The recorder :func:`install_recorder` last published, if any."""
    return _installed


def stats_path_for(index_path: str | os.PathLike[str] | None) -> Path:
    """``usage-stats.json`` beside the catalog index.

    The hosted process keeps the index on the ``/data`` volume
    (``$XDG_CACHE_HOME/umbra-py/catalog.db``, or ``$UMBRA_INDEX_DB``). The
    counter lives in that same directory so a redeploy that keeps the volume
    keeps the total.
    """
    from .index import default_index_path  # noqa: PLC0415

    base = Path(index_path) if index_path is not None else default_index_path()
    return base.with_name("usage-stats.json")


def _count(value: Any) -> int | None:
    """An int, or ``None`` when ``value`` is a bool or some other type."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def index_facts(index_path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Scenes, baked previews, and the index build date. Nulls when unread.

    One read-only open of the catalog index. ``thumbnails`` is the preview
    count :meth:`~umbra_py.CatalogIndex.stats` already takes from the
    thumbnail sidecar. A missing file or a read error is nulls, not a raised
    error: ``/stats`` still answers.
    """
    if index_path is None or not Path(index_path).is_file():
        return dict(_EMPTY_FACTS)
    try:
        from .index import CatalogIndex  # noqa: PLC0415

        with CatalogIndex(index_path, readonly=True) as index:
            stats = index.stats()
        scenes = stats.get("items")
        thumbs = stats.get("thumbnailed")
        built = stats.get("built_at")
        return {
            "scenes_indexed": _count(scenes),
            "thumbnails": _count(thumbs),
            "index_built_at": built if isinstance(built, str) else None,
        }
    except Exception:
        logger.warning("usage stats could not read the catalog index", exc_info=True)
        return dict(_EMPTY_FACTS)


def read_place_anchors(index_path: str | os.PathLike[str] | None) -> list[tuple[str, float, float]]:
    """Baked place labels used to name a bbox. Empty when the index cannot be read."""
    if index_path is None or not Path(index_path).is_file():
        return []
    try:
        from .index import CatalogIndex  # noqa: PLC0415

        with CatalogIndex(index_path, readonly=True) as index:
            return index.place_anchors()
    except Exception:
        logger.warning("usage stats could not read place labels", exc_info=True)
        return []


def coarsen_label(
    value: str | None,
    *,
    address: Mapping[str, Any] | None = None,
    kind: str | None = None,
) -> str | None:
    """A city and country, or ``None`` when the text is not a place.

    Nominatim labels run specific to general (``"Austin, Travis County, Texas,
    United States"``). The first and last segments that survive are the city
    and the country. A segment with a digit, a street word, an underscore, or
    anything that is not a place name is dropped, so a street address does not
    become a published place. One surviving segment (``"Tokyo"``, ``"Iceland"``)
    is kept as a city or a country.

    A structured ``address`` wins when the geocoder returned one: city, town,
    village, or county, then the state or region, then the country. A venue
    (``kind`` is not a settlement or an admin area) does not keep its own
    name. ``"White House, Washington, District of Columbia, United States"``
    with kind ``tourism`` is ``"Washington, United States"``. With no city
    left, the state and country, or just the country, are the label.
    """
    named = _label_from_address(address)
    if named:
        return named
    return _label_from_display(value, drop_venue=_is_venue(kind))


def _is_venue(kind: str | None) -> bool:
    if not isinstance(kind, str):
        return False
    token = kind.strip().lower().replace("-", "_").replace(" ", "_")
    if not token:
        return False
    return token not in _SETTLEMENT_KINDS


def _label_from_address(address: Mapping[str, Any] | None) -> str | None:
    if not isinstance(address, Mapping):
        return None
    fields: dict[str, str] = {}
    for key, value in address.items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            fields[key.lower()] = value.strip()
    if not fields:
        return None
    country = fields.get("country") if _segment_ok(fields.get("country", "")) else None
    state = _first_component(fields, _STATE_ADDRESS_KEYS)
    city = _first_component(fields, _CITY_ADDRESS_KEYS)
    return _join_place(city, state, country)


def _first_component(fields: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = fields.get(key)
        if value and _segment_ok(value):
            return value
    return None


def _join_place(city: str | None, state: str | None, country: str | None) -> str | None:
    if city and country and city.casefold() != country.casefold():
        label = f"{city}, {country}"
        return label if len(label) <= 80 else country
    if city and state and city.casefold() != state.casefold():
        label = f"{city}, {state}"
        return label if len(label) <= 80 else city
    if state and country and state.casefold() != country.casefold():
        label = f"{state}, {country}"
        return label if len(label) <= 80 else country
    return country or city or state


def _label_from_display(value: str | None, *, drop_venue: bool) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > 120:
        return None
    if any(ch in text for ch in "/\\?&=@#"):
        return None
    parts = [part.strip() for part in text.split(",")]
    if drop_venue:
        parts = parts[1:]
    kept = [part for part in parts if _segment_ok(part)]
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    first, last = kept[0], kept[-1]
    if first.casefold() == last.casefold():
        return first
    label = f"{first}, {last}"
    return label if len(label) <= 80 else None


def coarse_place(
    *,
    resolved_place: str | None = None,
    bbox: Sequence[float] | None = None,
    intersects: Any = None,
    anchors: Sequence[tuple[str, float, float]] = (),
    address: Mapping[str, Any] | None = None,
    kind: str | None = None,
) -> str | None:
    """One coarse place for a search, or ``None`` when none can be named.

    Only geocoder output (``resolved_place`` or a tool's ``display_name``)
    and a bbox or intersects geometry whose centre is within
    :data:`PLACE_MATCH_DEGREES` of a baked index label. A free-text
    ``place=`` argument is not a name: when a bbox is also set, geocoding
    is skipped and that text would otherwise be stored. The geometry is
    not returned and is not stored. A box larger than
    :data:`PLACE_MAX_SPAN_DEGREES` is left unnamed.
    """
    named = coarsen_label(resolved_place, address=address, kind=kind)
    if named:
        return named
    box = _as_bbox(bbox) or _intersects_bbox(intersects)
    if box is None or not anchors:
        return None
    min_lon, min_lat, max_lon, max_lat = box
    return _nearest_anchor((min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0, anchors)


def remember_search_place(
    scope: MutableMapping[str, Any],
    *,
    resolved_place: str | None = None,
    bbox: Sequence[float] | None = None,
    intersects: Any = None,
    anchors: Sequence[tuple[str, float, float]] = (),
    address: Mapping[str, Any] | None = None,
    kind: str | None = None,
) -> None:
    """Attach the coarse place to this request. The raw inputs are not kept.

    The search handler calls this after it has parsed the request. The usage
    middleware reads :data:`USAGE_PLACE_KEY` once the response status is known.
    A failure here must not fail the search.
    """
    try:
        name = coarse_place(
            resolved_place=resolved_place,
            bbox=bbox,
            intersects=intersects,
            anchors=anchors,
            address=address,
            kind=kind,
        )
        if name:
            scope[USAGE_PLACE_KEY] = name
    except Exception:
        logger.warning("usage stats place skipped", exc_info=True)


def request_counts(
    method: str,
    path: str,
    status: int,
    headers: Mapping[str, str] | None = None,
    *,
    query: Mapping[str, Any] | None = None,
    first_page: bool | None = None,
) -> bool:
    """Whether this HTTP response is a real search.

    ``GET`` and ``POST /search`` with a 2xx status, a user agent that is not a
    bot or an uptime probe, and no ``X-Umbra-Smoke`` opt-out. Only the first
    page counts: no paging token, or a token of zero. A 200 with an empty
    page is still a search. Everything else — health checks, unknown paths,
    exploit probes, ``HEAD``, ``OPTIONS``, 429s, errors, and a later page —
    is false.

    ``first_page`` is the offset the handler already parsed (a POST body
    token lives there, not in the query). When it is omitted, ``query`` is
    what decides.
    """
    if method.upper() not in {"GET", "POST"}:
        return False
    if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300:
        return False
    if not _is_search_path(path):
        return False
    if first_page is False:
        return False
    if first_page is None and not _query_is_first_page(query):
        return False
    found = headers or {}
    if _is_smoke(_header_ci(found, "x-umbra-smoke")):
        return False
    if _identity_excluded(_header_ci(found, "user-agent")):
        return False
    return True


def _query_is_first_page(query: Mapping[str, Any] | None) -> bool:
    if not query:
        return True
    for key, value in query.items():
        if str(key).lower() not in _PAGE_KEYS:
            continue
        if isinstance(value, (list, tuple)) and not isinstance(value, str):
            values = value
        else:
            values = (value,)
        if any(_later_page(one) for one in values):
            return False
    return True


def _later_page(value: Any) -> bool:
    """True when this token is a page after the first."""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip() not in ("", "0")
    return True


def _query_from_scope(scope: Mapping[str, Any]) -> dict[str, list[str]]:
    raw = scope.get("query_string") or b""
    text = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else str(raw)
    found: dict[str, list[str]] = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        found.setdefault(key, []).append(value)
    return found


def _is_search_path(path: str) -> bool:
    return path == "/search"


def _identity_excluded(user_agent: str | None, client_name: str | None = None) -> bool:
    """True when the caller should not be counted.

    A missing user agent is excluded: a browser and an MCP client send one,
    and a blank agent is not a person we can defend as real. A known bot
    substring on the agent or the MCP client name is excluded too.
    """
    if _contains_ignored(user_agent) or _contains_ignored(client_name):
        return True
    if user_agent and user_agent.strip():
        return False
    return not (client_name and client_name.strip())


def _contains_ignored(value: str | None) -> bool:
    if not value:
        return False
    hay = value.lower()
    return any(needle in hay for needle in DEFAULT_IGNORED_UA)


def _is_smoke(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in _SMOKE_VALUES


def _header_ci(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name and isinstance(value, str):
            return value
    return None


def _segment_ok(text: str) -> bool:
    if len(text) < 2 or len(text) > 48:
        return False
    if any(ch.isdigit() or ch == "_" for ch in text):
        return False
    if not text[0].isalpha():
        return False
    for ch in text:
        if ch.isalpha() or ch in " .'’-":
            continue
        return False
    tokens = [tok.strip(".'’").lower() for tok in text.split() if tok.strip(".'’")]
    flagged = [tok for tok in tokens if tok in _ADDRESS_WORDS]
    if not flagged:
        return True
    # "St. Louis" names a city. "Main St" names a street.
    return flagged == ["st"] and tokens[0] == "st" and len(tokens) > 1


def _as_bbox(bbox: Sequence[float] | None) -> tuple[float, float, float, float] | None:
    if bbox is None or len(bbox) != 4:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if isinstance(bbox[0], bool):  # bool is a subclass of int
        return None
    if not all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat)):
        return None
    if min_lon > max_lon or min_lat > max_lat:
        return None
    if max_lon - min_lon > PLACE_MAX_SPAN_DEGREES or max_lat - min_lat > PLACE_MAX_SPAN_DEGREES:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def _intersects_bbox(intersects: Any) -> tuple[float, float, float, float] | None:
    if intersects is None:
        return None
    try:
        from ._geometry import geometry_bbox, parse_geometry  # noqa: PLC0415

        if isinstance(intersects, (dict, str)):
            geometry = parse_geometry(intersects)
        elif isinstance(intersects, list):
            geometry = intersects
        else:
            return None
        return _as_bbox(geometry_bbox(geometry))
    except (TypeError, ValueError):
        return None


def _nearest_anchor(
    lat: float, lon: float, anchors: Sequence[tuple[str, float, float]]
) -> str | None:
    best_name: str | None = None
    best = PLACE_MATCH_DEGREES**2
    cos = math.cos(math.radians(lat))
    for name, alat, alon in anchors:
        dlat = lat - alat
        dlon = (lon - alon) * cos
        dist = dlat * dlat + dlon * dlon
        nearer = best_name is None or dist < best or (dist == best and name < best_name)
        if dist <= best and nearer:
            best = dist
            best_name = name
    if best_name is None:
        return None
    return coarsen_label(best_name)


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d")


def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in scope.get("headers") or ():
        if isinstance(key, bytes) and isinstance(value, bytes):
            found[key.decode("latin-1")] = value.decode("latin-1", "replace")
    return found


def _bounded(places: dict[str, int], *, drop_below: int | None) -> dict[str, int]:
    """Keep at most :data:`MAX_PLACES` names, highest counts first.

    ``drop_below`` removes a name whose count is under that floor. The day
    roll uses it so a place that never reached :data:`MIN_DISTINCT_CLIENTS`
    is not kept forever. A snapshot does not, so a restart mid-day still
    has the clients counted so far.
    """
    kept = places
    if drop_below is not None:
        kept = {name: count for name, count in places.items() if count >= drop_below}
    if len(kept) <= MAX_PLACES:
        return kept
    ranked = sorted(kept, key=lambda name: (-kept[name], name))
    return {name: kept[name] for name in ranked[:MAX_PLACES]}


def _trim_today(today: dict[str, set[str]]) -> dict[str, set[str]]:
    """Drop the smallest same-day sets once the map is over :data:`MAX_PLACES`."""
    sizes = {name: len(hashes) for name, hashes in today.items()}
    kept = _bounded(sizes, drop_below=None)
    return {name: today[name] for name in kept}


def _host_of(client: Any) -> str | None:
    """Address from an ASGI ``(host, port)`` pair or an object with ``host``."""
    if isinstance(client, (list, tuple)) and client:
        host = client[0]
    else:
        host = getattr(client, "host", None)
    if isinstance(host, bytes):
        host = host.decode("latin-1", "replace")
    if isinstance(host, str) and host:
        return host
    return None


def _client_host(scope: Mapping[str, Any]) -> str | None:
    return _host_of(scope.get("client"))


class _GeocodeNote:
    """Structured geocoder fields for the MCP call in progress.

    The middleware creates one and stores it in a context variable before
    the tool runs. A sync tool thread receives that same object, so filling
    in ``address`` and ``kind`` is visible when the tool returns.
    """

    __slots__ = ("address", "kind")

    def __init__(self) -> None:
        self.address: Mapping[str, str] | None = None
        self.kind: str | None = None


_geocode_note: contextvars.ContextVar[_GeocodeNote | None] = contextvars.ContextVar(
    "umbra_usage_geocode", default=None
)


def note_geocode(address: Mapping[str, Any] | None, kind: str | None) -> None:
    """Remember address components for the MCP call in progress.

    No-op when the middleware did not arm a note. ``GET /search`` passes
    the same fields on the request scope, because that handler runs in
    another thread.
    """
    note = _geocode_note.get()
    if note is None:
        return
    cleaned: dict[str, str] | None = None
    if isinstance(address, Mapping):
        cleaned = {
            str(key): value.strip()
            for key, value in address.items()
            if isinstance(key, str) and isinstance(value, str) and value.strip()
        } or None
    note.address = cleaned
    note.kind = kind.strip() if isinstance(kind, str) and kind.strip() else None


class UsageStats:
    """In-memory counters flushed to an atomic JSON file.

    MCP counts while this object is the installed recorder.
    :func:`install_recorder` ``(None)`` on shutdown is what stops it. The
    HTTP middleware holds this object directly and does not use that flag.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        *,
        facts: Callable[[], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._facts = facts
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._flush_requested = threading.Event()
        self._salt = os.urandom(16)
        now = self._clock()
        self._day = _day(now)
        self._searches = 0
        self._places: dict[str, int] = {}
        self._today: dict[str, set[str]] = {}
        self._counting_since = _stamp(now)
        self._pending = 0
        self._cached: dict[str, Any] | None = None
        self._cached_until = 0.0
        self._persist = self.path is not None
        self._warned = False
        self._flusher: threading.Thread | None = None
        self._stop = threading.Event()
        self.anchors: list[tuple[str, float, float]] = []
        if self._persist:
            self._load()

    @property
    def searches_served(self) -> int:
        with self._lock:
            return self._searches

    @property
    def counting_since(self) -> str:
        return self._counting_since

    def distinct_clients(self, place: str) -> int:
        """High-water distinct clients for one coarse place. For tests and the payload."""
        with self._lock:
            return self._clients_locked(place)

    def open(self, index_path: str | os.PathLike[str] | None = None) -> None:
        """Start flushing and load baked place labels off the request path."""
        self._start_flusher()
        threading.Thread(
            target=self._load_anchors,
            args=(index_path,),
            name="umbra-usage-anchors",
            daemon=True,
        ).start()
        self.flush()

    def close(self) -> None:
        """Stop the flush thread and write what is still in memory."""
        self._stop.set()
        self._flush_requested.set()
        flusher = self._flusher
        if flusher is not None and flusher is not threading.current_thread():
            flusher.join(timeout=2)
        self._flusher = None
        self.flush()

    def observe_http(self, scope: Mapping[str, Any], status: int) -> None:
        """Count one HTTP response when it is a real search. Never raises."""
        try:
            headers = _scope_headers(scope)
            path = scope.get("path")
            method = scope.get("method")
            if not isinstance(method, str) or not isinstance(path, str):
                return
            marked = scope.get(USAGE_FIRST_PAGE, _UNMARKED)
            if marked is _UNMARKED:
                query: Mapping[str, Any] | None = _query_from_scope(scope)
                first_page = None
            else:
                query = None
                first_page = marked is True
            if not request_counts(
                method, path, status, headers, query=query, first_page=first_page
            ):
                return
            place = scope.get(USAGE_PLACE_KEY)
            self.record_search(
                client=_client_host(scope),
                place=place if isinstance(place, str) else None,
            )
        except Exception:
            logger.warning("usage stats dropped an HTTP request", exc_info=True)

    def observe_tool(self, ctx: Any, result: Any, geocode: _GeocodeNote | None = None) -> None:
        """Count one successful MCP tool call. Never raises.

        Called from server middleware after the tool returns, so the tool
        signature is unchanged. ``tools/list``, a raised tool error, and an
        ``isError`` result are not counts. One call is one search, whatever
        page token the arguments carried.
        """
        try:
            if not self._tool_counts(ctx, result):
                return
            self.record_search(
                client=_request_host(getattr(ctx, "request", None)),
                place=_tool_place(ctx, result, self.anchors, geocode),
            )
        except Exception:
            logger.warning("usage stats dropped an MCP call", exc_info=True)

    def record_search(self, *, client: str | None, place: str | None) -> None:
        """Increment the total and, when both are known, the place's daily set.

        ``place`` is coarsened again here. A label that does not survive is
        not stored. A search with no client address still counts, but it does
        not join a place: one shared anonymous bucket would invent clients.
        """
        try:
            label = coarsen_label(place) if place else None
            now = self._clock()
            with self._lock:
                self._roll_day_locked(now)
                self._searches += 1
                if label and client:
                    digest = hashlib.sha256(
                        self._salt + b"\0" + client.encode("utf-8", "replace")
                    ).hexdigest()
                    self._today.setdefault(label, set()).add(digest)
                self._pending += 1
                if self._pending >= FLUSH_EVERY:
                    self._flush_requested.set()
                if len(self._today) > MAX_PLACES:
                    self._today = _trim_today(self._today)
            # No disk I/O here. The flush thread wakes on the event, or on
            # its 30s interval, and close() writes whatever is left.
        except Exception:
            logger.warning("usage stats dropped a search", exc_info=True)

    def public_payload(self) -> dict[str, Any]:
        """The cached ``GET /stats`` document. Recomputed at most once an hour."""
        now = self._clock()
        with self._lock:
            if self._cached is not None and now < self._cached_until:
                return _copy(self._cached)
        facts = self._safe_facts()
        with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._cached_until:
                return _copy(self._cached)
            payload = {
                "searches_served": self._searches,
                "counting_since": self._counting_since,
                "scenes_indexed": facts["scenes_indexed"],
                "thumbnails": facts["thumbnails"],
                "index_built_at": facts["index_built_at"],
                "top_places": self._top_locked(),
                "generated_at": _stamp(now),
            }
            self._cached = payload
            self._cached_until = now + STATS_CACHE_TTL_S
            return _copy(payload)

    def flush(self) -> None:
        """Write the current totals. A disk error is logged once and ignored.

        The write lock covers the snapshot and the replace, so two flushes
        cannot publish an older snapshot after a newer one. The request
        path does not call this.
        """
        try:
            if not self._persist or self.path is None:
                return
            with self._write_lock:
                with self._lock:
                    document = self._snapshot_locked()
                    self._pending = 0
                self._write(document)
        except Exception:
            logger.warning("usage stats flush failed", exc_info=True)

    def _safe_facts(self) -> dict[str, Any]:
        if self._facts is None:
            return dict(_EMPTY_FACTS)
        try:
            raw = self._facts()
        except Exception:
            logger.warning("usage stats catalog facts failed", exc_info=True)
            return dict(_EMPTY_FACTS)
        if not isinstance(raw, Mapping):
            return dict(_EMPTY_FACTS)
        return {
            "scenes_indexed": raw.get("scenes_indexed"),
            "thumbnails": raw.get("thumbnails"),
            "index_built_at": raw.get("index_built_at"),
        }

    def _top_locked(self) -> list[dict[str, Any]]:
        names = set(self._places) | set(self._today)
        ranked = sorted(
            ((name, self._clients_locked(name)) for name in names),
            key=lambda item: (-item[1], item[0]),
        )
        return [
            {"place": name, "clients": count}
            for name, count in ranked
            if count >= MIN_DISTINCT_CLIENTS
        ][:TOP_PLACES]

    def _clients_locked(self, place: str) -> int:
        live = len(self._today.get(place, ()))
        return max(self._places.get(place, 0), live)

    def _roll_day_locked(self, now: float) -> None:
        day = _day(now)
        if day == self._day:
            return
        for name, hashes in self._today.items():
            self._places[name] = max(self._places.get(name, 0), len(hashes))
        self._today.clear()
        self._day = day
        self._salt = os.urandom(16)
        self._places = _bounded(self._places, drop_below=MIN_DISTINCT_CLIENTS)

    def _snapshot_locked(self) -> dict[str, Any]:
        places = dict(self._places)
        for name, hashes in self._today.items():
            places[name] = max(places.get(name, 0), len(hashes))
        kept = {
            name: count
            for name, count in places.items()
            if _count(count) == count and count > 0 and coarsen_label(name) == name
        }
        kept = _bounded(kept, drop_below=None)
        return {
            "version": _FILE_VERSION,
            "counting_since": self._counting_since,
            "searches_served": self._searches,
            "places": kept,
        }

    def _load(self) -> None:
        path = self.path
        if path is None or not path.is_file():
            return
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                self._quarantine("too large")
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._quarantine("unreadable")
            return
        if not isinstance(raw, dict) or raw.get("version") != _FILE_VERSION:
            self._quarantine("unreadable")
            return
        since = raw.get("counting_since")
        if isinstance(since, str) and _ISO_STAMP.fullmatch(since):
            self._counting_since = since
        searches = raw.get("searches_served")
        if isinstance(searches, int) and not isinstance(searches, bool) and searches >= 0:
            self._searches = searches
        places = raw.get("places")
        if not isinstance(places, dict):
            return
        loaded: dict[str, int] = {}
        for name, count in places.items():
            if not isinstance(name, str) or isinstance(count, bool) or not isinstance(count, int):
                continue
            if count < 1 or coarsen_label(name) != name:
                continue
            loaded[name] = count
        self._places = _bounded(loaded, drop_below=None)

    def _quarantine(self, reason: str) -> None:
        """Move an unusable counter aside so a later flush cannot erase it."""
        path = self.path
        if path is None or not path.is_file():
            return
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = path.with_name(f"{path.name}.bad-{stamp}")
        n = 0
        while dest.exists():
            n += 1
            dest = path.with_name(f"{path.name}.bad-{stamp}-{n}")
        try:
            os.replace(path, dest)
        except OSError:
            logger.warning(
                "usage stats file is %s and could not be moved aside; not overwriting it",
                reason,
            )
            self._persist = False
            return
        logger.warning(
            "usage stats file is %s; moved it to %s and starting from zero",
            reason,
            dest.name,
        )

    def _write(self, document: Mapping[str, Any]) -> None:
        path = self.path
        if path is None:
            return
        tmp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            tmp = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            tmp = None
        except OSError as exc:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            if not self._warned:
                self._warned = True
                logger.warning(
                    "usage stats could not be saved (%s); /stats keeps the in-memory copy",
                    exc,
                )
            else:
                logger.debug("usage stats save failed: %s", exc)

    def _start_flusher(self) -> None:
        if self._flusher is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self._flush_requested.wait(FLUSH_INTERVAL_S)
                self._flush_requested.clear()
                if self._stop.is_set():
                    break
                self.flush()

        thread = threading.Thread(target=loop, name="umbra-usage-flush", daemon=True)
        self._flusher = thread
        thread.start()

    def _load_anchors(self, index_path: str | os.PathLike[str] | None) -> None:
        anchors = read_place_anchors(index_path)
        if anchors:
            self.anchors = anchors

    def _tool_counts(self, ctx: Any, result: Any) -> bool:
        if getattr(ctx, "method", None) != "tools/call":
            return False
        name, _arguments = _params_name_and_args(ctx)
        if name not in _SEARCH_TOOLS:
            return False
        if _result_is_error(result):
            return False
        request = getattr(ctx, "request", None)
        if _is_smoke(_header_value(request, "x-umbra-smoke")):
            return False
        user_agent = _header_value(request, "user-agent")
        client_name, _version = _initialize_client(ctx)
        return not _identity_excluded(user_agent, client_name)


def _copy(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


def _tool_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        for key in ("structuredContent", "structured_content"):
            structured = result.get(key)
            if isinstance(structured, dict):
                return structured
        return result
    for attr in ("structured_content", "structuredContent"):
        structured = getattr(result, attr, None)
        if isinstance(structured, dict):
            return structured
    return {}


def _tool_place(
    ctx: Any,
    result: Any,
    anchors: Sequence[tuple[str, float, float]],
    geocode: _GeocodeNote | None = None,
) -> str | None:
    _name, arguments = _params_name_and_args(ctx)
    mapping = _argument_mapping(arguments)
    payload = _tool_payload(result)
    resolved = payload.get("resolved_place")
    if not isinstance(resolved, str):
        display = payload.get("display_name")
        resolved = display if isinstance(display, str) else None
    bbox = mapping.get("bbox")
    intersects = mapping.get("intersects")
    return coarse_place(
        resolved_place=resolved if isinstance(resolved, str) else None,
        bbox=bbox if isinstance(bbox, (list, tuple)) else None,
        intersects=intersects,
        anchors=anchors,
        address=None if geocode is None else geocode.address,
        kind=None if geocode is None else geocode.kind,
    )


class SearchUsageMiddleware:
    """Count a finished ``/search`` from the status the client actually got.

    Pure ASGI, outside the rate-limit guard, so a 429 the guard returns
    itself is visible and is not counted. A failure inside the counter is
    logged and does not change the response.
    """

    def __init__(self, app: Any, recorder: UsageStats) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        status = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                self.recorder.observe_http(scope, status)
            except Exception:
                logger.warning("usage stats dropped an HTTP request", exc_info=True)


class ToolUsageCount:
    """Count a successful MCP ``tools/call`` without changing the tool.

    Installed on the server middleware chain. With no recorder installed it
    is a pass-through. :func:`install_recorder` ``(None)`` on shutdown is
    what stops counting. The tool's arguments and its result are what the
    client sent and what the tool returned.
    """

    async def __call__(self, ctx: Any, call_next: Callable[..., Any]) -> Any:
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)
        note = _GeocodeNote()
        token = _geocode_note.set(note)
        try:
            result = await call_next(ctx)
        finally:
            _geocode_note.reset(token)
        recorder = current_recorder()
        if recorder is not None:
            recorder.observe_tool(ctx, result, note)
        return result
