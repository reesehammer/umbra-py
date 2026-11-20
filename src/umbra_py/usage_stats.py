"""Public usage totals for ``GET /stats``.

The hosted API answers a docs-site panel: how many real searches it has
served, how big the loaded index is, and which coarse places people
actually ask about. The numbers are a social-proof counter, so they start
at zero when the file is first created and are never backfilled.

What is counted is narrow on purpose. A successful ``GET`` or ``POST
/search``, and a successful MCP tool call, count. Probes, health checks,
unknown paths, ``HEAD`` / ``OPTIONS``, rate-limit and error responses,
smoke traffic, and known bot or uptime user agents do not. A request that
does not clearly qualify is dropped.

What is stored is narrower still. The file beside the catalog index holds
the total, the ``counting_since`` timestamp, and a per-place distinct-client
*count*. A client is a salted hash of the address the rate limiter already
uses. The salt is random, lives only in memory, and rotates each UTC day,
so the file cannot be turned back into an address. Raw query strings, user
agents, and the hash itself are not written. Place text is reduced to a
city and country, or to a baked index label near the query, before it is
kept.

Recording is off the success path's critical section: an in-memory update
under one lock, a batched flush, and a flush at shutdown. A stats failure
is logged and swallowed. If the volume cannot be written, ``/stats`` still
answers from memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: How long a computed ``GET /stats`` document is reused. The response
#: ``Cache-Control`` max-age is this same window.
STATS_CACHE_TTL_S = 3600.0
STATS_CACHE_CONTROL = f"public, max-age={int(STATS_CACHE_TTL_S)}"

#: A place is published only when at least this many distinct clients reached
#: it on one UTC day (the high-water mark of that daily count).
MIN_DISTINCT_CLIENTS = 3
TOP_PLACES = 10

#: Nearest baked place label, in degrees, that may name a bbox. About 111 km
#: at the equator: a metro, not a street.
PLACE_MATCH_DEGREES = 1.0
#: A box wider or taller than this is not named. A continent is not a city.
PLACE_MAX_SPAN_DEGREES = 5.0

#: In-memory increments between flushes. Shutdown flushes whatever is left.
FLUSH_EVERY = 50
FLUSH_INTERVAL_S = 30.0

#: Scope key holding the already-coarsened place for this one request.
USAGE_PLACE_KEY = "umbra.usage_place"

#: File schema. A different version is ignored and the counter starts over
#: rather than being interpreted as a count it is not.
_FILE_VERSION = 1
_MAX_FILE_BYTES = 1_000_000

#: User-agent substrings that are never counted, matched case-insensitively.
#: ``UMBRA_STATS_IGNORE_UA`` adds more (comma-separated). Our own probes are
#: listed by the exact agent they send.
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

_HEALTH_PATHS = frozenset(
    {
        "/healthz",
        "/healthz/live",
        "/healthz/ready",
        "/livez",
        "/readyz",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)

_PROBE_SNIPPETS = (
    "/.env",
    "/.git",
    "/.aws",
    "/wp-",
    "/wp-admin",
    "/phpmyadmin",
    "/cgi-bin",
    "/etc/passwd",
    "/boaform",
    "/manager/html",
    "/actuator",
    "/solr",
    "/vendor/phpunit",
    "/shell",
    "/admin",
    "/server-status",
    "/config.json",
)

_SMOKE_VALUES = frozenset({"1", "true", "yes", "on"})
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")

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


def coarsen_label(value: str | None) -> str | None:
    """A city and country from a place string, or ``None`` when it is not one.

    Nominatim labels run specific to general (``"Austin, Travis County, Texas,
    United States"``). The first and last segments that survive are the city
    and the country. A segment with a digit, a street word, an underscore, or
    anything that is not a place name is dropped, so a street address does not
    become a published place. One surviving segment (``"Tokyo"``, ``"Iceland"``)
    is kept as a city or a country.
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > 120:
        return None
    if any(ch in text for ch in "/\\?&=@#"):
        return None
    kept = [part.strip() for part in text.split(",") if _segment_ok(part.strip())]
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
    place: str | None = None,
    resolved_place: str | None = None,
    bbox: Sequence[float] | None = None,
    intersects: Any = None,
    anchors: Sequence[tuple[str, float, float]] = (),
) -> str | None:
    """One coarse place for a search, or ``None`` when none can be named.

    A geocoded label wins, then a ``place=`` string, then a bbox or intersects
    geometry whose centre is within :data:`PLACE_MATCH_DEGREES` of a baked
    index label. The geometry is not returned and is not stored. A box larger
    than :data:`PLACE_MAX_SPAN_DEGREES` is left unnamed.
    """
    named = coarsen_label(resolved_place)
    if named:
        return named
    named = coarsen_label(place)
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
    place: str | None = None,
    resolved_place: str | None = None,
    bbox: Sequence[float] | None = None,
    intersects: Any = None,
    anchors: Sequence[tuple[str, float, float]] = (),
) -> None:
    """Attach the coarse place to this request. The raw inputs are not kept.

    The search handler calls this after it has parsed the request. The usage
    middleware reads :data:`USAGE_PLACE_KEY` once the response status is known.
    A failure here must not fail the search.
    """
    try:
        name = coarse_place(
            place=place,
            resolved_place=resolved_place,
            bbox=bbox,
            intersects=intersects,
            anchors=anchors,
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
) -> bool:
    """Whether this HTTP response is a real search.

    ``GET`` and ``POST /search`` with a 2xx status, a user agent that is not a
    bot or an uptime probe, and no ``X-Umbra-Smoke`` opt-out. Everything else
    — health checks, unknown paths, exploit probes, ``HEAD``, ``OPTIONS``,
    429s, and errors — is false.
    """
    if method.upper() not in {"GET", "POST"}:
        return False
    if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300:
        return False
    if not _is_search_path(path):
        return False
    found = headers or {}
    if _is_smoke(_header_ci(found, "x-umbra-smoke")):
        return False
    if _identity_excluded(_header_ci(found, "user-agent")):
        return False
    return True


def _is_search_path(path: str) -> bool:
    if not isinstance(path, str) or not path:
        return False
    lowered = path.lower()
    if ".." in path or "%2e" in lowered or "\\" in path:
        return False
    if any(snippet in lowered for snippet in _PROBE_SNIPPETS):
        return False
    trimmed = path.rstrip("/") or "/"
    if trimmed.lower() in _HEALTH_PATHS:
        return False
    return trimmed == "/search"


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
    for needle in DEFAULT_IGNORED_UA:
        if needle in hay:
            return True
    extra = os.environ.get("UMBRA_STATS_IGNORE_UA", "")
    for part in extra.split(","):
        token = part.strip().lower()
        if token and token in hay:
            return True
    return False


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


def _scope_header(scope: Mapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if isinstance(key, bytes) and key.lower() == name:
            if isinstance(value, bytes):
                return value.decode("latin-1", "replace")
            if isinstance(value, str):
                return value
    return None


def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in scope.get("headers") or ():
        if isinstance(key, bytes) and isinstance(value, bytes):
            found[key.decode("latin-1")] = value.decode("latin-1", "replace")
    return found


def _client_host(scope: Mapping[str, Any]) -> str | None:
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        host = client[0]
    else:
        host = getattr(client, "host", None)
    if isinstance(host, bytes):
        host = host.decode("latin-1", "replace")
    if isinstance(host, str) and host:
        return host
    return None


class UsageStats:
    """In-memory counters flushed to an atomic JSON file.

    ``accepting`` gates MCP recording. The HTTP middleware holds this object
    and counts whether or not the lifespan has opened it; MCP only counts
    after :meth:`open`, so a recorder left behind by a test does not keep
    counting tool calls.
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
        self.accepting = False
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

    def enable(self) -> None:
        """Count MCP calls without starting the flush thread."""
        self.accepting = True

    def open(self, index_path: str | os.PathLike[str] | None = None) -> None:
        """Start flushing and load baked place labels off the request path."""
        self.accepting = True
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
        self.accepting = False
        self._stop.set()
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
            if not request_counts(method, path, status, headers):
                return
            place = scope.get(USAGE_PLACE_KEY)
            self.record_search(
                client=_client_host(scope),
                place=place if isinstance(place, str) else None,
            )
        except Exception:
            logger.warning("usage stats dropped an HTTP request", exc_info=True)

    def observe_tool(self, ctx: Any, result: Any) -> None:
        """Count one successful MCP tool call. Never raises.

        Called from server middleware after the tool returns, so the tool
        signature is unchanged. ``tools/list``, a raised tool error, and an
        ``isError`` result are not counts.
        """
        try:
            if not self._tool_counts(ctx, result):
                return
            self.record_search(
                client=_tool_client(ctx),
                place=_tool_place(ctx, result, self.anchors),
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
                due = self._pending >= FLUSH_EVERY
            if due:
                self.flush()
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
        """Write the current totals. A disk error is logged once and ignored."""
        try:
            if not self._persist or self.path is None:
                return
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
        scenes = raw.get("scenes_indexed")
        thumbs = raw.get("thumbnails")
        built = raw.get("index_built_at")
        return {
            "scenes_indexed": _count(scenes),
            "thumbnails": _count(thumbs),
            "index_built_at": built if isinstance(built, str) else None,
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

    def _snapshot_locked(self) -> dict[str, Any]:
        places = dict(self._places)
        for name, hashes in self._today.items():
            places[name] = max(places.get(name, 0), len(hashes))
        kept = {
            name: count
            for name, count in places.items()
            if _count(count) == count and count > 0 and coarsen_label(name) == name
        }
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
                logger.warning("usage stats file is too large; starting from zero")
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("usage stats file could not be read; starting from zero")
            return
        if not isinstance(raw, dict) or raw.get("version") != _FILE_VERSION:
            logger.warning("usage stats file has an unknown version; starting from zero")
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
        self._places = loaded

    def _write(self, document: Mapping[str, Any]) -> None:
        path = self.path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
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
            while not self._stop.wait(FLUSH_INTERVAL_S):
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
        name, _arguments = _tool_params(ctx)
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            return False
        if _result_is_error(result):
            return False
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if _is_smoke(_header_get(headers, "x-umbra-smoke")):
            return False
        user_agent = _header_get(headers, "user-agent")
        client_name = _tool_client_name(ctx)
        return not _identity_excluded(user_agent, client_name)


def _copy(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload))


def _header_get(headers: Any, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        value = getter(name)
        if value is None:
            value = getter(name.title())
    except Exception:
        return None
    if isinstance(value, bytes):
        value = value.decode("latin-1", "replace")
    return value if isinstance(value, str) else None


def _tool_params(ctx: Any) -> tuple[Any, Any]:
    params = getattr(ctx, "params", None)
    if isinstance(params, dict):
        return params.get("name"), params.get("arguments")
    return getattr(params, "name", None), getattr(params, "arguments", None)


def _argument_mapping(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    dump = getattr(arguments, "model_dump", None)
    if not callable(dump):
        return {}
    try:
        dumped = dump()
    except Exception:
        return {}
    return dumped if isinstance(dumped, dict) else {}


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


def _result_is_error(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _tool_client(ctx: Any) -> str | None:
    request = getattr(ctx, "request", None)
    client = getattr(request, "client", None)
    if client is None:
        return None
    if isinstance(client, (tuple, list)) and client:
        host = client[0]
    else:
        host = getattr(client, "host", None)
    if isinstance(host, bytes):
        host = host.decode("latin-1", "replace")
    return host if isinstance(host, str) and host else None


def _tool_client_name(ctx: Any) -> str | None:
    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    if info is None and isinstance(params, dict):
        info = params.get("clientInfo") or params.get("client_info")
    if isinstance(info, dict):
        name = info.get("name")
    else:
        name = getattr(info, "name", None)
    return name if isinstance(name, str) else None


def _tool_place(ctx: Any, result: Any, anchors: Sequence[tuple[str, float, float]]) -> str | None:
    _name, arguments = _tool_params(ctx)
    mapping = _argument_mapping(arguments)
    payload = _tool_payload(result)
    resolved = payload.get("resolved_place")
    if not isinstance(resolved, str):
        display = payload.get("display_name")
        resolved = display if isinstance(display, str) else None
    place = mapping.get("place")
    bbox = mapping.get("bbox")
    intersects = mapping.get("intersects")
    return coarse_place(
        place=place if isinstance(place, str) else None,
        resolved_place=resolved if isinstance(resolved, str) else None,
        bbox=bbox if isinstance(bbox, (list, tuple)) else None,
        intersects=intersects,
        anchors=anchors,
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

    Installed on the server middleware chain. With no recorder, or before
    :meth:`UsageStats.open`, it is a pass-through. The tool's arguments and
    its result are what the client sent and what the tool returned.
    """

    def __init__(self, recorder: UsageStats | None = None) -> None:
        self._recorder = recorder

    async def __call__(self, ctx: Any, call_next: Callable[..., Any]) -> Any:
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)
        try:
            result = await call_next(ctx)
        except BaseException:
            raise
        recorder = self._recorder if self._recorder is not None else current_recorder()
        if recorder is not None and recorder.accepting:
            recorder.observe_tool(ctx, result)
        return result
