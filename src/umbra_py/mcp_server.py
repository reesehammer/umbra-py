"""``umbra-mcp`` — a Model Context Protocol server over :mod:`umbra_py`.

*Agents are the new first-time users.* Umbra publishes no STAC API, so this
library **is** the query layer for a 17+ TB public SAR archive. This module
exposes that layer over MCP, turning any MCP-enabled client (Claude Desktop /
Code and a growing list of others) into a zero-install, natural-language front
door to the archive: *"show me what changed at Centerfield, Utah this spring"*
becomes a first-run experience instead of a tutorial chapter.

The tools are thin wrappers over the existing public API — the CLI subcommands
already map 1:1 to library functions, so the tool inventory was already
designed. Two design commitments carry over from the rest of the package:

- **Deterministic core, AI at the edges.** The server searches, geocodes,
  downloads and renders; the *client's* model plans and narrates. A public
  community host holds no vision key (that would be an open wallet), so
  ``describe_scene`` / ``narrate_change`` return a *reading kit* — the PNG, the
  packaged SAR-literacy prompt, and the JSON schema — and the client's already-
  running model does the reading. A local stdio server with an ``[ai]`` key
  still consults a vision model itself. Either way a model output never becomes
  a coordinate, a URL, or a filter without passing through this deterministic
  layer, and every server-side reading is stamped as an AI interpretation.
- **Images are the API.** ``quicklook``/``change_composite``/``timescan``
  return the PNG as an MCP image content block, so the model *sees* the radar
  scene. On a public host ``quicklook`` serves a baked catalog preview (no COG
  proxy); tools that must stream Umbra GeoTIFFs refuse with a local-server
  hint. Compact context cards (:meth:`UmbraItem.to_llm_context`), not full STAC
  JSON, are returned from search to protect the client's context window.

Run it with ``uvx --from 'umbra-py[mcp]' umbra-mcp`` (nothing installed),
``umbra mcp`` or ``python -m umbra_py.mcp_server`` (stdio transport either
way). ``umbra mcp --http`` serves Streamable HTTP instead (``/mcp``, plus
``GET /healthz``). The hosted community instance is ``umbra serve --public``:
the same ``/mcp`` plus a STAC API on one process.
Requires the ``mcp`` extra (``pip install 'umbra-py[mcp]'``) -- which is
why the zero-install form needs ``--from``: this console script lives in the
``umbra-py`` distribution, so handing its name to ``uvx`` alone would look for a
distribution called ``umbra-mcp`` (there is none) and would not install the
extra either. ``server.json`` publishes the stdio command to the MCP registry.
"""

from __future__ import annotations

import functools
import io
import json
import os
import shlex
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Annotated, Any, cast
from urllib.parse import unquote, urlsplit

from ._geometry import parse_geometry as _parse_geometry
from .catalog import UmbraCatalog
from .constants import (
    AI_PROVENANCE,
    ASSET_FILENAME_FIELD,
    ATTRIBUTION,
    CANOPY_TOKEN_ENV,
    DATA_LICENSE,
    POLARIZATION_CAVEAT,
    S3_BUCKET,
    S3_REGION,
    collection_ids_passed_as_products,
    unknown_collection_detail,
)
from .context import llm_context
from .convert import SPECKLE_WINDOW_DEFAULT
from .exceptions import CatalogError, GeocodeError, MissingDependencyError, UmbraError
from .geocode import geocode_place as _nominatim_geocode
from .index import (
    BakedPreview,
    CatalogIndex,
    asset_name_error_text,
    default_index_path,
    is_asset_name,
    resolve_asset_name,
)
from .models import UmbraItem
from .watch import MetaWatchStore, SearchSource, watch, watch_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.mcpserver import MCPServer


def _require_mcp():
    """Import the MCP SDK, or raise a helpful install hint.

    ``mcp.server.mcpserver`` is the 2.0 home of what 1.x called
    ``mcp.server.fastmcp``; the class is ``MCPServer`` rather than ``FastMCP``.
    The extra requires ``mcp>=2``, so a stale 1.x install lands here as a
    ``ModuleNotFoundError`` and gets the same upgrade hint as a missing one.
    """
    try:
        from mcp.server.mcpserver import Image, MCPServer
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        raise MissingDependencyError(
            "The MCP server needs the 'mcp' extra (mcp>=2). Install it with:\n"
            "    pip install 'umbra-py[mcp]'",
            hint="pip install 'umbra-py[mcp]'",
        ) from exc
    return MCPServer, Image


# --------------------------------------------------------------------------
# Shared helpers (deterministic; no MCP dependency so they are unit-testable
# without the SDK installed).
# --------------------------------------------------------------------------


def _item_from_index(url: str) -> UmbraItem | None:
    """Load ``url`` from the local catalog index, or ``None`` if it is not there."""
    path = default_index_path()
    if not path.exists():
        return None
    with _open_index(path) as index:
        item = index.get_by_href(url)
        if item is None and "://" not in url:
            item = index.get(url)
        return item


def _fetch_item(url: str) -> UmbraItem:
    """Load one STAC item, preferring the local catalog index.

    Search on a hosted instance is index-backed. Re-fetching the live sidecar
    for ``get_item`` / ``quicklook`` / ``describe_scene`` made those tools
    disagree with ``search_catalog`` (newer asset filenames, extra product
    types, ``s3://`` hrefs). When the index has this href, every tool sees
    the same snapshot.

    On the public host (:func:`_cog_streaming_allowed` is false) a miss is not
    an open fetch. The value must be an index id, a product filename, or an
    acquisition stem in the local index, or an https URL on Umbra's public
    open-data bucket. Anything else raises :class:`ValueError` and makes no
    request. Every ``_fetch_item`` caller (``get_item``, ``quicklook``,
    ``download_asset``, ``describe_scene``, the composite and stack tools,
    and the LangChain / LlamaIndex wrappers) goes through this gate.
    """
    indexed = _item_from_index(url)
    if indexed is not None:
        return indexed
    if not _cog_streaming_allowed():
        return _fetch_item_public(url)
    from ._http import get_json

    return UmbraItem.from_dict(get_json(url), href=url)


_PUBLIC_ITEM_REFUSAL = (
    "{url!r} is not an item id, product filename, or acquisition stem in the "
    "local catalog index, and it is not an https URL on Umbra's public "
    "open-data bucket. The public host did not request it."
)


class PublicHostBusy(ValueError):
    """The public host refused to start another outbound request."""


#: Quick outbound calls: HEAD, geocode, one item GET. anyio's worker pool is
#: 40; this leaves the rest for index-backed tools. One client reading a
#: handful of uncached bucket URLs still fits. Past the cap the call fails
#: at once.
_PUBLIC_NET_LIMIT = 8
#: Live catalog walks and semantic or embedding model calls. Separate from
#: the quick pool so a walk cannot fill every permit a geocode needs.
_PUBLIC_NET_LONG_LIMIT = 2
_PUBLIC_NET_BUSY = (
    "The public host is already handling its limit of network requests. Retry in a moment."
)
_PUBLIC_NET = threading.BoundedSemaphore(_PUBLIC_NET_LIMIT)
_PUBLIC_NET_LONG = threading.BoundedSemaphore(_PUBLIC_NET_LONG_LIMIT)


def _reset_public_net() -> None:
    """Drop any leaked public-network permits. Tests call this between cases."""
    global _PUBLIC_NET, _PUBLIC_NET_LONG
    _PUBLIC_NET = threading.BoundedSemaphore(_PUBLIC_NET_LIMIT)
    _PUBLIC_NET_LONG = threading.BoundedSemaphore(_PUBLIC_NET_LONG_LIMIT)


@contextmanager
def _public_net_slot(*, long: bool = False):
    """One public-host outbound permit, or a no-op off the public host.

    ``long=False`` is the quick pool (:data:`_PUBLIC_NET_LIMIT`): HEAD,
    geocode, and a single item GET. ``long=True`` is the pool for a live
    catalog walk or a semantic or embedding model call
    (:data:`_PUBLIC_NET_LONG_LIMIT`). Either pool raises
    :class:`PublicHostBusy` at once when it is full, with the same message.
    """
    if _cog_streaming_allowed():
        yield
        return
    pool = _PUBLIC_NET_LONG if long else _PUBLIC_NET
    if not pool.acquire(blocking=False):
        raise PublicHostBusy(_PUBLIC_NET_BUSY)
    try:
        yield
    finally:
        pool.release()


def _fetch_item_public(url: str) -> UmbraItem:
    """Index filename/stem, or one allowlisted JSON GET. No other request."""
    if is_asset_name(url):
        try:
            # This path runs only on the public host. Open read-only so a
            # filename lookup cannot write the snapshot or name its path.
            return resolve_asset_name(url, readonly=True)
        except CatalogError as exc:
            raise ValueError(asset_name_error_text(exc)) from exc
    if not _public_bucket_https(url):
        raise ValueError(_PUBLIC_ITEM_REFUSAL.format(url=url))
    from ._http import safe_get

    with _public_net_slot():
        raw = safe_get(url, _public_bucket_https)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"The document at {url!r} was not JSON.") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"The document at {url!r} was not a JSON object.")
    return UmbraItem.from_dict(doc, href=url)


def _png_bytes(image: Any) -> bytes:
    """Encode a ``PIL.Image`` as PNG bytes for an MCP image block."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


#: ``umbra serve --public`` sets this to ``"0"`` so MCP tools that would stream
#: Umbra COGs through the host refuse instead of proxying the archive. Local
#: stdio / self-hosted MCP leave it unset (streaming allowed). Tests override
#: via :func:`configure`.
COG_STREAMING_ENV = "UMBRA_MCP_COG_STREAMING"

_cog_streaming_override: bool | None = None


def configure(*, cog_streaming: bool | None = None) -> None:
    """Set process-wide MCP policy for this Python process.

    ``cog_streaming=False`` is the public-host guardrail (do not proxy Umbra
    COGs). ``None`` (the default) falls back to :data:`COG_STREAMING_ENV`.
    """
    global _cog_streaming_override
    _cog_streaming_override = cog_streaming


def _cog_streaming_allowed() -> bool:
    if _cog_streaming_override is not None:
        return _cog_streaming_override
    raw = os.environ.get(COG_STREAMING_ENV, "1")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


#: Public ``search_catalog`` page size. The tool's ``limit`` argument and its
#: default stay 20; a larger request is clamped and the result says so.
#: Local MCP (COG streaming allowed) keeps the caller's limit.
PUBLIC_CATALOG_LIMIT = 100

_PUBLIC_LIMIT_NOTE = (
    "limit clamped to 100; use the next page (narrow bbox, dates, or area; "
    "the STAC /search next link pages the rest)."
)


def _clamp_public_catalog_limit(limit: int) -> tuple[int, str | None]:
    """Cap ``search_catalog`` on the public host. Local calls pass through."""
    if _cog_streaming_allowed():
        return limit, None
    if isinstance(limit, bool) or not isinstance(limit, int):
        return limit, None
    if limit > PUBLIC_CATALOG_LIMIT:
        return PUBLIC_CATALOG_LIMIT, _PUBLIC_LIMIT_NOTE
    return limit, None


def _with_limit_note(payload: dict[str, Any], note: str | None) -> dict[str, Any]:
    if note:
        payload["note"] = note
    return payload


#: A miss that would wait longer than this, or join more than this many
#: callers already waiting on Nominatim, fails immediately.
_GEOCODE_MAX_WAIT_S = 5.0
_GEOCODE_MAX_WAITERS = 5
#: How long a "no such place" answer is remembered. A network failure is not.
_GEOCODE_NEGATIVE_TTL_S = 600.0
_GEOCODE_BUSY = "Geocoding is busy on this host; retry in a few seconds."


def _geocode_not_found(exc: GeocodeError) -> bool:
    text = str(exc)
    return text.startswith("No place matched") or "no usable bounding box" in text


class PublicGeocodeGate:
    """LRU cache and a global Nominatim throttle for the public MCP host.

    One shared address is what Nominatim bans. Cache hits do not wait and
    do not call out. A miss waits for the single in-flight call and for the
    one-second gap, and that whole wait is capped at about five seconds.
    More than five waiters, or a lock that is not free inside the remaining
    budget, fails immediately. "No such place" is remembered for about ten
    minutes. Any other failure is not cached. Local MCP does not use this.
    """

    def __init__(
        self,
        *,
        max_size: int = 256,
        min_interval: float = 1.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_size = max_size
        self._min_interval = min_interval
        self._time = time_fn
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._network = threading.Lock()
        self._cache: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
        self._next_at = 0.0
        self._waiters = 0

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._next_at = 0.0
            self._waiters = 0

    def resolve(self, query: str) -> tuple[Any, str]:
        key = " ".join(query.split()).casefold()
        cached = self._cached(key)
        if cached is not None:
            return cached
        deadline = self._time() + _GEOCODE_MAX_WAIT_S
        self._admit()
        acquired = False
        try:
            remaining = deadline - self._time()
            if remaining <= 0 or not self._network.acquire(timeout=remaining):
                raise GeocodeError(_GEOCODE_BUSY)
            acquired = True
            cached = self._cached(key)
            if cached is not None:
                return cached
            now = self._time()
            throttle = max(0.0, self._next_at - now)
            if throttle > deadline - now:
                raise GeocodeError(_GEOCODE_BUSY)
            self._next_at = now + throttle + self._min_interval
            if throttle > 0:
                self._sleep(throttle)
            try:
                result = _nominatim_geocode(query)
            except GeocodeError as exc:
                if _geocode_not_found(exc):
                    self._store(key, ("neg", self._time() + _GEOCODE_NEGATIVE_TTL_S, str(exc)))
                raise
            self._store(key, ("ok", result))
            return result
        finally:
            if acquired:
                self._network.release()
            with self._lock:
                self._waiters -= 1

    def _admit(self) -> None:
        with self._lock:
            if self._waiters >= _GEOCODE_MAX_WAITERS:
                raise GeocodeError(_GEOCODE_BUSY)
            self._waiters += 1

    def _cached(self, key: str) -> tuple[Any, str] | None:
        with self._lock:
            return self._cached_locked(key)

    def _cached_locked(self, key: str) -> tuple[Any, str] | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if hit[0] == "neg":
            if self._time() >= hit[1]:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            raise GeocodeError(hit[2])
        self._cache.move_to_end(key)
        return hit[1]

    def _store(self, key: str, entry: tuple[Any, ...]) -> None:
        with self._lock:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)


_public_geocode = PublicGeocodeGate()


def _geocode_place(query: str) -> tuple[Any, str]:
    """Nominatim, directly locally and through :class:`PublicGeocodeGate` on the public host."""
    if _cog_streaming_allowed():
        return _nominatim_geocode(query)
    return _public_geocode.resolve(query)


def _public_host() -> bool:
    """Whether this process is the public host (COG streaming off).

    ``umbra serve --public`` sets that guard. Local stdio and a self-hosted
    ``umbra serve`` leave streaming on, and those keep the writable index.
    """
    return not _cog_streaming_allowed()


def _with_optional_path(prefix: str, path: os.PathLike[str] | str, suffix: str) -> str:
    """``prefix at {path}. suffix`` locally; the same sentence without the path on a public host."""
    if _public_host():
        return f"{prefix}. {suffix}"
    return f"{prefix} at {path}. {suffix}"


def _open_index(path: os.PathLike[str] | str | None = None) -> CatalogIndex:
    """Open the catalog the MCP tools read.

    The public host uses ``mode=ro``, so a tool cannot persist into the
    shared snapshot. A local server stays read-write.
    """
    return CatalogIndex(
        default_index_path() if path is None else path,
        readonly=_public_host(),
    )


def _model_key_configured() -> bool:
    """Whether a vision/chat key is set — the same trio ``describe`` / ``narrate`` use."""
    return any(
        os.environ.get(var) for var in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
    )


_COG_REFUSAL = (
    "{tool} streams Umbra COGs (GeoTIFF overviews) through this host. "
    "The public community instance does not proxy the archive — asset hrefs "
    "from get_item point at Umbra's public S3 bucket for you to open locally "
    "(QGIS, rasterio, or a local `uvx --from 'umbra-py[mcp]' umbra-mcp` server). "
    "For a picture of one scene here, call quicklook: it serves a baked preview "
    "from the catalog index, with no COG proxy. change_composite, timescan, "
    "stack_stats, stack_provenance, pick_change_interval, narrate_change and "
    "find_similar need a local server."
)


def _require_cog_streaming(tool: str) -> None:
    """Refuse tools that would proxy Umbra rasters on a public instance."""
    if _cog_streaming_allowed():
        return
    raise ValueError(_COG_REFUSAL.format(tool=tool))


#: HTTPS hosts the public catalog actually emits for the open bucket.
#: Virtual-hosted: published STAC asset hrefs (no region, and the regional
#: form ``catalog._object_key`` accepts). Path-style: ``UmbraCatalog._url_for``
#: and :func:`umbra_py.models._derive_data_url` (``s3.<region>.amazonaws.com/<bucket>/...``).
_BUCKET_VIRTUAL_HOSTS = frozenset(
    {
        f"{S3_BUCKET}.s3.amazonaws.com",
        f"{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com",
    }
)
_BUCKET_PATH_HOST = f"s3.{S3_REGION}.amazonaws.com"

_LINK_NOTE = (
    "The hosted server does not write files. This is a direct link to the "
    "public object, not a download. A local `uvx --from 'umbra-py[mcp]' "
    "umbra-mcp` server can download this asset to your own disk "
    "(download_asset with confirm=true)."
)
_COMMANDS_OMITTED_NOTE = (
    "Fetch commands are omitted because this href is not an https URL on "
    "Umbra's public open-data bucket."
)


def _nonneg_int(value: Any) -> int | None:
    """A non-negative integer, or None. Booleans are not sizes."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _stac_asset_size(asset: dict[str, Any]) -> int | None:
    """Bytes recorded on the STAC asset (``file:size``, else ``size``)."""
    for key in ("file:size", "size"):
        if key not in asset:
            continue
        parsed = _nonneg_int(asset.get(key))
        if parsed is not None:
            return parsed
    return None


def _header_length(headers: Any) -> int | None:
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return _nonneg_int(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _public_bucket_https(href: str) -> bool:
    """Whether ``href`` is https on Umbra's public open-data bucket.

    The public host may HEAD only these URLs, and only to read
    ``Content-Length``. Anything else — another host, plain http, a userinfo
    trick, a non-443 port, or a path-style URL whose first segment is not the
    open bucket — is not requested. Redirects are not followed; this check is
    the URL as given.
    """
    if not isinstance(href, str) or not href:
        return False
    try:
        parts = urlsplit(href)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    if not host:
        return False
    host = host.lower().rstrip(".")
    path = unquote(parts.path)
    segments = [segment for segment in path.split("/") if segment != ""]
    if ".." in segments or "\\" in path:
        return False
    if host in _BUCKET_VIRTUAL_HOSTS:
        return True
    return host == _BUCKET_PATH_HOST and bool(segments) and segments[0] == S3_BUCKET


def _head_content_length(href: str, *, raise_on_failure: bool = False) -> int | None:
    """``Content-Length`` from HEAD, or None when it is missing or the check fails.

    Only an allowlisted https URL is requested, via :func:`requests.head` so
    the shared session does not retry, and redirects are not followed. A 404
    or 410 raises :class:`requests.HTTPError`. Any other non-2xx, including a
    redirect, or a network error, returns None so ``confirm=true`` still emits
    a link plan.

    ``raise_on_failure`` is the public ``confirm=false`` preview. A redirect
    is still not followed and returns None. Any other non-2xx raises
    :class:`requests.HTTPError`, and a network error propagates — that preview
    used to ``raise_for_status`` on the response it got back.
    """
    if not _public_bucket_https(href):
        return None
    import requests

    from ._http import DEFAULT_TIMEOUT

    try:
        with _public_net_slot():
            head = requests.head(href, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException:
        if raise_on_failure:
            raise
        return None
    # A redirect is not a size. Do not read its Content-Length or follow it.
    if 300 <= head.status_code < 400:
        return None
    if not 200 <= head.status_code < 300:
        if head.status_code in (404, 410) or raise_on_failure:
            head.raise_for_status()
        return None
    return _header_length(head.headers)


def _public_confirm_preview_size(item: UmbraItem, asset: str, href: str) -> int | None:
    """Byte size for ``confirm=false`` on the public host.

    An allowlisted href is HEADed. Anything else is not requested; the size
    is the STAC ``file:size`` or ``size``.
    """
    if _public_bucket_https(href):
        return _head_content_length(href, raise_on_failure=True)
    body = item._resolve_asset(asset)[1]
    return _stac_asset_size(body)


def _plain_basename(name: str) -> str:
    """Last path segment, or ``""`` when it is not a safe filename.

    Separators are ``/`` and ``\\``. A leading dot, a control character
    (below 0x20, or DEL), or more than 255 bytes is rejected so the caller
    can fall back to the href basename.
    """
    cleaned = name.replace("\\", "/").strip()
    if not cleaned:
        return ""
    base = cleaned.rsplit("/", 1)[-1].strip()
    if (
        not base
        or base.startswith(".")
        or len(base.encode("utf-8")) > 255
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in base)
    ):
        return ""
    return base


def _safe_download_filename(named: Any, href: str, fallback: str) -> str:
    """Basename for ``curl -o``. Falls back to the href, then the asset key."""
    from .download import _filename_from_url

    base = _plain_basename(named) if isinstance(named, str) else ""
    if base:
        return base
    from_href = _plain_basename(_filename_from_url(href))
    if from_href and from_href != "download":
        return from_href
    return _plain_basename(fallback) or "download"


def _how_to_fetch(
    item_arg: str, asset: str, filename: str, href: str, dest_dir: str
) -> dict[str, str | None]:
    """Copy-paste commands. Flags match ``umbra download --help``.

    Both commands are null unless ``href`` is https on the public open-data
    bucket. The keys stay so a caller can read them either way.
    """
    if not _public_bucket_https(href):
        return {"curl": None, "umbra": None}
    dest = dest_dir if isinstance(dest_dir, str) and dest_dir else "."
    curl = f"curl -L -C - -o {shlex.quote(filename)} {shlex.quote(href)}"
    umbra = (
        "umbra download "
        f"{shlex.quote(item_arg)} --asset {shlex.quote(asset)} --dest {shlex.quote(dest)}"
    )
    return {"curl": curl, "umbra": umbra}


def _link_plan(
    *,
    asset: str,
    filename: str,
    href: str,
    size: int | None,
    item_arg: str,
    dest_dir: str,
) -> dict[str, Any]:
    commands = _how_to_fetch(item_arg, asset, filename, href, dest_dir)
    note = _LINK_NOTE
    if commands["curl"] is None:
        note = f"{note} {_COMMANDS_OMITTED_NOTE}"
    return {
        "mode": "link",
        "downloaded": False,
        "asset": asset,
        "filename": filename,
        "href": href,
        "bytes": size,
        "how_to_fetch": commands,
        "note": note,
    }


def _public_host_confirmed_download(
    item: UmbraItem,
    asset: str,
    url: str,
    dest_dir: str,
    href: str,
) -> dict[str, Any]:
    """Link plan. Never GETs the asset and never writes a file.

    When ``href`` is https on the public bucket, ``bytes`` is the STAC
    ``file:size`` or ``size`` if that is recorded, otherwise ``Content-Length``
    from HEAD. Otherwise the href is not requested, ``bytes`` is null, and
    ``how_to_fetch`` leaves both commands null.
    """
    if not isinstance(href, str) or not href.startswith(("http://", "https://")):
        raise ValueError(
            f"No public HTTP(S) href for asset {asset!r} on item {item.id!r}. "
            "Nothing to link, and nothing was downloaded."
        )
    key, body = item._resolve_asset(asset)
    named = body.get(ASSET_FILENAME_FIELD)
    filename = _safe_download_filename(named, href, key)
    item_arg = url
    if isinstance(item.href, str) and item.href.startswith(("http://", "https://")):
        item_arg = item.href
    size: int | None = None
    if _public_bucket_https(href):
        stac_size = _stac_asset_size(body)
        if stac_size is not None:
            size = stac_size
        else:
            try:
                size = _head_content_length(href)
            except PublicHostBusy:
                # The plan is still the answer. The size can be read later.
                size = None
    return _link_plan(
        asset=asset,
        filename=filename,
        href=href,
        size=size,
        item_arg=item_arg,
        dest_dir=dest_dir,
    )


def _as_mcp_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool so anticipated failures reach the MCP client.

    Shared helpers raise :class:`ValueError` / :class:`UmbraError` so they stay
    unit-testable without the MCP extra. MCP SDK 2 treats anything other than
    ``ToolError`` as a crash and strips the message to ``Error executing tool
    <name>`` — which is how a public-host COG refusal arrived as that one line
    and not the local-server hint. Converting at registration keeps the
    callables themselves raising ``ValueError``.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, UmbraError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapped


def _canopy_token() -> str | None:
    """The configured Canopy commercial-archive token, if any.

    Read from ``$UMBRA_CANOPY_TOKEN`` — the same environment variable the CLI's
    ``--token`` falls back to. A token is a *secret configured once by the
    operator* in the MCP server's environment (an MCP client sets it in the
    server's ``env`` block), never a tool argument the client's model passes
    around. When it is set, the search and retrieval tools transparently query
    Umbra's authenticated commercial archive (a real STAC API) instead of the
    open bucket — the funnel made literal: the same tools a user learned on the
    free data now drive the paid Canopy catalog, with no new tool to learn.
    """
    return os.environ.get(CANOPY_TOKEN_ENV) or None


def _source_label(is_index: bool, is_archive: bool) -> str:
    """Name the backend a result came from, for the tool's ``source`` field."""
    if is_index:
        return "local-index"
    if is_archive:
        return "canopy-archive"
    return "live-catalog"


def _search_source(
    local: bool | None, token: str | None = None
) -> tuple[UmbraCatalog | CatalogIndex, bool]:
    """Pick the search backend.

    A configured Canopy ``token`` selects Umbra's authenticated commercial
    archive and takes precedence over the local/live open-data choice (the
    archive is a live STAC API, so it has no local index — combining it with
    ``local=True`` is a deliberate error). Otherwise: ``local=True`` forces the
    on-disk index (error if absent); ``local=False`` forces a live S3 walk;
    ``local=None`` (the default) uses the index when one exists at the default
    path and falls back to a live walk otherwise — the fast path for an agent
    that has run ``umbra index fetch``.
    """
    if token:
        if local is True:
            raise ValueError(
                "A Canopy token searches the live commercial archive, which has "
                "no local index, so it cannot be combined with local=True. Unset "
                f"${CANOPY_TOKEN_ENV} to search the local open-data index."
            )
        return UmbraCatalog(token=token), False
    path = default_index_path()
    if local is True:
        if not path.exists():
            raise FileNotFoundError(
                _with_optional_path(
                    "No local index",
                    path,
                    "Fetch the published snapshot with "
                    "'umbra index fetch', or build one with 'umbra index build'.",
                )
            )
        return _open_index(path), True
    if local is None and path.exists():
        return _open_index(path), True
    return UmbraCatalog(), False


def _require_same_polarization(items: list[UmbraItem]) -> None:
    """Guard change detection against the documented polarization pitfall.

    An HH scene and a VV scene of the same place measure different scattering
    and must not be differenced. The deterministic layer refuses the mix rather
    than emit a misleading composite; the model never gets to override this.
    """
    pols = {p for item in items for p in (item.polarizations or [])}
    if len(pols) > 1:
        raise ValueError(
            f"Refusing to composite mixed polarizations {sorted(pols)}: HH and "
            "VV measure different scattering and cannot be compared for change. "
            "Filter to a single polarization first."
        )


def _polarization_advisory(items: list[UmbraItem]) -> str | None:
    """Warn when a composite's same-polarization property could not be *verified*.

    :func:`_require_same_polarization` refuses a *visible* mix -- two passes whose
    known polarizations differ. What it cannot see is a pass that carries no
    ``sar:polarizations`` metadata at all: the composite still renders, but whether
    every pass measured the same scattering is then unknown, and an HH-vs-VV mix
    would read as false change. Return an advisory naming that gap so an agent
    handed the picture also sees why it may be suspect (design principle: images
    are the API -- return the artifact *with its provenance*), or ``None`` when
    every pass declares one and the same polarization.
    """
    missing = sum(1 for item in items if not item.polarizations)
    if missing:
        return (
            f"Caution: {missing} of {len(items)} pass(es) carry no polarization "
            "metadata, so umbra-py could not verify they share one polarization. "
            "An HH-vs-VV mix would read as false change; confirm the passes match "
            "before trusting this composite."
        )
    return None


def _resolve_semantic_area(
    query: str, *, top_k: int = 5, min_score: float = 0.0, model: str | None = None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Resolve a plain-language site *description* to catalog task names.

    This is how the semantic C1 capability (``umbra semantic search``) reaches the
    MCP surface: it ranks the prebuilt task-name embedding index against ``query``
    and returns ``(best_task, ranked_matches)`` — the single best task name to
    search plus the ranked candidates for the agent to audit — or ``(None, [])``
    when nothing clears the threshold. Like :func:`find_similar_text`, the only
    model call is turning the query into a vector (an injectable embedder gated on
    an ``[ai]`` key); the storage, cosine ranking and thresholding are
    deterministic and offline.

    Raises :class:`FileNotFoundError` if the semantic index has not been built,
    and (via :func:`~umbra_py.semantic.default_embedder`)
    :class:`~umbra_py.MissingDependencyError` when no embedding key is configured,
    so it never runs implicitly. Prefer ``search_catalog(area=..., fuzzy=True)``
    or :func:`find_repeat_sites` when the query shares words with a task name
    (``"beet piler"``); this path is for descriptions that share none.
    """
    from . import semantic as sem

    path = sem.default_semantic_path()
    if not path.exists():
        raise FileNotFoundError(
            _with_optional_path(
                "No semantic index",
                path,
                "For a site name you roughly know "
                "(e.g. 'beet piler'), call search_catalog(area=..., fuzzy=True) or "
                "find_repeat_sites(area=...) instead — those are deterministic and "
                "need no embedding key. semantic=True is for a description that "
                "shares no words with the task label (e.g. 'grain storage north "
                "dakota') and needs 'umbra semantic build' plus an embedding key.",
            )
        )
    embedder = sem.default_embedder(model=model)
    with sem.SemanticTaskIndex(path) as index:
        matches = index.matching_tasks(query, embedder, top_k=top_k, min_score=min_score)
    best = matches[0].task if matches else None
    ranked = [{"task": m.task, "score": round(m.score, 6)} for m in matches]
    return best, ranked


def _arg_desc(text: str) -> Any:
    """One-line description for an MCP argument, applied when a schema is built.

    Pydantic arrives with the ``mcp`` and ``langchain`` extras, not the core
    install. ``from __future__ import annotations`` stores this call as text,
    so the import happens only when a tool schema is evaluated.
    """
    from pydantic import Field

    return Field(description=text)


# --------------------------------------------------------------------------
# Tool implementations (plain functions; registered on the server below).
# --------------------------------------------------------------------------


def search_catalog(
    bbox: Annotated[
        list[float] | None,
        _arg_desc("WGS84 [min_lon, min_lat, max_lon, max_lat]. When set, place is not geocoded."),
    ] = None,
    intersects: Annotated[
        dict[str, Any] | None,
        _arg_desc(
            "One GeoJSON Point, or a Polygon or MultiPolygon, optionally wrapped in a "
            "Feature, FeatureCollection, or GeometryCollection. A point matches footprints "
            "that contain it. Cannot be combined with bbox or place."
        ),
    ] = None,
    place: Annotated[
        str | None,
        _arg_desc("Free-text place name. Geocoded to a bbox only when bbox is omitted."),
    ] = None,
    area: Annotated[
        str | None,
        _arg_desc(
            "Task-name substring, or a plain-language site description when semantic is true."
        ),
    ] = None,
    fuzzy: Annotated[
        bool,
        _arg_desc(
            "Loose task-name match for area (word order, punctuation, typos). "
            "No model. Cannot be combined with semantic."
        ),
    ] = False,
    semantic: Annotated[
        bool,
        _arg_desc(
            "Resolve area by embedding similarity. Needs a semantic index and an "
            "embedding key. Cannot be combined with fuzzy."
        ),
    ] = False,
    min_score: Annotated[
        float,
        _arg_desc("Cosine cutoff in [0, 1] for semantic matches. Scores below it are dropped."),
    ] = 0.0,
    start: Annotated[
        str | None,
        _arg_desc(
            "Inclusive acquisition-date lower bound: ISO date, bare year or month, "
            "or a relative expression such as 3 months ago."
        ),
    ] = None,
    end: Annotated[
        str | None,
        _arg_desc("Inclusive acquisition-date upper bound. Same grammar as start."),
    ] = None,
    products: Annotated[
        list[str] | None,
        _arg_desc(
            "Keep items that expose one of these types (GEC, CSI, SIDD, SICD, CPHD). "
            "A collection id such as umbra-gec or umbra-open-data raises; use the product type."
        ),
    ] = None,
    polarizations: Annotated[
        list[str] | None,
        _arg_desc(
            "Keep items exposing any listed polarization, such as VV. Missing values are dropped."
        ),
    ] = None,
    min_incidence: Annotated[
        float | None,
        _arg_desc("Minimum incidence angle in degrees. Items missing incidence are dropped."),
    ] = None,
    max_incidence: Annotated[
        float | None,
        _arg_desc("Maximum incidence angle in degrees. Items missing incidence are dropped."),
    ] = None,
    max_resolution: Annotated[
        float | None,
        _arg_desc(
            "Keep items this fine or finer, in metres. Items missing resolution are dropped."
        ),
    ] = None,
    canonical: Annotated[
        bool | None,
        _arg_desc(
            "True keeps the preferred copy of a republished collect; False keeps "
            "only the other copies. Omit to keep every item."
        ),
    ] = None,
    limit: Annotated[
        int,
        _arg_desc(
            "Maximum cards. Default 20. The public host clamps a value above 100 "
            "to 100 and adds note."
        ),
    ] = 20,
    max_per_task: Annotated[
        int | None,
        _arg_desc("Maximum items per task (site). 1 is one pin per site."),
    ] = None,
    local: Annotated[
        bool | None,
        _arg_desc(
            "True uses only the local index; False forces a live walk. Omit to "
            "use the index when present. True cannot be combined with a Canopy token."
        ),
    ] = None,
) -> dict[str, Any]:
    """Search Umbra's catalog and return compact context cards.

    Filters (all optional, combine freely): ``bbox`` as
    ``[min_lon, min_lat, max_lon, max_lat]`` in WGS84 degrees; ``intersects`` as
    a GeoJSON object (a ``Point``, ``Polygon``, or ``MultiPolygon``, or a
    ``Feature`` / ``FeatureCollection`` wrapping one; a point matches footprints
    that contain it) to keep only footprints
    intersecting it -- a tighter spatial filter than the rectangular ``bbox``,
    and mutually exclusive with ``bbox`` / ``place``; ``place`` as a
    free-text name geocoded to a bbox; ``area`` as a substring of the Umbra
    task (site) name (set ``fuzzy`` to match it loosely -- word-order- and
    punctuation-independent and typo-tolerant, resolved deterministically with
    no model call, so ``"beet piler"`` still reaches ``"Beet Piler - ND"`` and
    ``"utah centerfield"`` still reaches ``"Centerfield,
    Utah"``; set ``semantic`` instead to treat ``area`` as a plain-language
    *description* of a site whose name you don't know and resolve it to the
    closest task names by *meaning* via the prebuilt embedding index, so
    ``"grain storage north dakota"`` reaches ``"Beet Piler - ND"`` -- a query
    that shares no word with the label, which ``fuzzy`` cannot and should not
    fake; ``semantic`` and ``fuzzy`` are mutually exclusive, and the resolved
    task name is returned as ``resolved_area`` with the ranked candidates in
    ``semantic_matches`` so you can audit and retry with a different one, and
    ``min_score`` -- a cosine threshold in ``[0, 1]`` -- drops weak aliases, so
    a description that matches nothing confidently returns an empty result
    rather than an arbitrary top pick); ``start``/``end`` as acquisition-date
    bounds -- an ISO
    ``YYYY-MM-DD`` date, a bare year/month (``2024``, ``2024-03``), or a
    relative expression (``today``, ``yesterday``, ``3 months ago``,
    ``last month``), resolved deterministically with no model call;
    ``products`` to restrict to product types
    (any of GEC, SICD, SIDD, CPHD); the SAR acquisition-property filters
    ``polarizations`` (keep items exposing any of these, e.g. ``["VV"]`` -- the
    filter that keeps a change comparison like-with-like), ``min_incidence`` /
    ``max_incidence`` (view incidence-angle bounds in degrees) and
    ``max_resolution`` (keep items at least this fine, in metres) -- each a hard
    predicate that excludes items missing that metadata; ``canonical`` to keep
    the preferred copy of a collect Umbra republished under another prefix
    (``True``), only the other copies (``False``), or every item when omitted;
    ``limit`` to cap
    results (on the public host a value above 100 is clamped to 100 and the
    result includes ``note``; the argument and its default are unchanged);
    ``max_per_task`` to cap items per site (use 1 for a one-pin-per-site
    overview).

    ``local`` selects the backend: leave it unset to use the on-disk index when
    present (instant) and fall back to a live S3 walk otherwise. When the server
    is configured with a Canopy token (``$UMBRA_CANOPY_TOKEN``), the search runs
    against Umbra's authenticated commercial archive instead and ``source`` is
    reported as ``"canopy-archive"``. Returns the per-item context cards, not
    full STAC JSON, to protect the context window; call ``get_item`` for one
    item's full metadata.

    Prefer ``fuzzy=True`` (or :func:`find_repeat_sites`) when ``area`` shares
    words with a task name; that path is deterministic and needs no key.
    ``semantic=True`` is only for a description that shares **no** tokens with
    the label. It requires a task-embedding index built ahead of time with
    ``umbra semantic build`` and a user-supplied embedding key (``OPENAI_API_KEY``,
    optionally ``OPENAI_BASE_URL`` / ``UMBRA_EMBED_MODEL``); it raises a helpful
    setup error otherwise, so it never runs implicitly. The only model call is
    turning ``area`` into a vector -- the resolved search over the chosen backend
    stays fully deterministic.
    """
    if intersects is not None and (bbox or place):
        raise ValueError("pass intersects or bbox/place, not both")
    mistaken = collection_ids_passed_as_products(products)
    if mistaken:
        raise ValueError(unknown_collection_detail(mistaken, product_parameter="products"))
    if semantic and fuzzy:
        raise ValueError(
            "pass semantic or fuzzy, not both: semantic resolves a plain-language "
            "site description via embeddings, fuzzy is a deterministic token match"
        )
    limit, limit_note = _clamp_public_catalog_limit(limit)

    resolved_area = None
    semantic_matches = None
    search_area = area
    search_fuzzy = fuzzy
    if semantic:
        if not area:
            raise ValueError(
                "semantic=True needs `area` set to the plain-language site "
                "description to resolve (e.g. 'grain storage north dakota')"
            )
        with _public_net_slot(long=True):
            resolved_area, semantic_matches = _resolve_semantic_area(area, min_score=min_score)
        if resolved_area is None:
            # Nothing cleared the similarity threshold: report the (empty) audit
            # trail rather than silently running an unfiltered catalog search.
            return _with_limit_note(
                {
                    "count": 0,
                    "source": "semantic-index",
                    "resolved_place": None,
                    "resolved_bbox": None,
                    "resolved_area": None,
                    "semantic_matches": [],
                    "items": [],
                    "attribution": ATTRIBUTION,
                },
                limit_note,
            )
        # Search the resolved exact task name literally (it is a real label, so a
        # substring match hits it; no fuzzy widening on an already-resolved name).
        search_area = resolved_area
        search_fuzzy = False

    # bbox is a 4-element [minx, miny, maxx, maxy]; cast the length-erased
    # ``tuple(...)`` back to the fixed-arity bbox type the backends expect.
    resolved_bbox = cast("tuple[float, float, float, float] | None", tuple(bbox) if bbox else None)
    resolved_place = None
    if place and resolved_bbox is None:
        with _public_net_slot():
            resolved_bbox, resolved_place = _geocode_place(place)
    geometry = _parse_geometry(intersects) if intersects is not None else None

    token = _canopy_token()
    source, is_index = _search_source(local, token=token)
    label = _source_label(is_index, bool(token))
    try:
        with _public_net_slot(long=True) if not is_index else nullcontext():
            results = source.search(
                bbox=resolved_bbox,
                intersects=geometry,
                start=start,
                end=end,
                product_types=list(products) if products else None,
                area=search_area,
                fuzzy=search_fuzzy,
                polarizations=list(polarizations) if polarizations else None,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
                canonical=canonical,
                limit=limit,
                max_per_task=max_per_task,
            )
            cards = [item.to_llm_context() for item in results]
    finally:
        if isinstance(source, CatalogIndex):
            source.close()

    return _with_limit_note(
        {
            "count": len(cards),
            "source": label,
            "resolved_place": resolved_place,
            "resolved_bbox": list(resolved_bbox) if resolved_bbox else None,
            "resolved_area": resolved_area,
            "semantic_matches": semantic_matches,
            "items": cards,
            "attribution": ATTRIBUTION,
        },
        limit_note,
    )


def find_repeat_sites(
    bbox: Annotated[
        list[float] | None,
        _arg_desc(
            "Pool filter: WGS84 [min_lon, min_lat, max_lon, max_lat]. When set, place is not "
            "geocoded."
        ),
    ] = None,
    intersects: Annotated[
        dict[str, Any] | None,
        _arg_desc(
            "Pool filter: one GeoJSON Point, or a Polygon or MultiPolygon, optionally wrapped "
            "in a Feature, FeatureCollection, or GeometryCollection. A point matches footprints "
            "that contain it. Cannot be combined with bbox or place."
        ),
    ] = None,
    place: Annotated[
        str | None,
        _arg_desc("Pool filter: free-text place. Geocoded to a bbox only when bbox is omitted."),
    ] = None,
    area: Annotated[
        str | None,
        _arg_desc("Pool filter: task-name substring. With fuzzy, a loose token match."),
    ] = None,
    fuzzy: Annotated[
        bool,
        _arg_desc("Loose task-name match for area. No model call."),
    ] = False,
    start: Annotated[
        str | None,
        _arg_desc(
            "Pool filter: inclusive lower bound on which passes are counted. "
            "ISO date, bare year or month, or a relative expression."
        ),
    ] = None,
    end: Annotated[
        str | None,
        _arg_desc(
            "Pool filter: inclusive upper bound on which passes are counted. Same grammar as start."
        ),
    ] = None,
    products: Annotated[
        list[str] | None,
        _arg_desc(
            "Pool filter: keep passes that expose one of these types "
            "(GEC, CSI, SIDD, SICD, CPHD). Case-insensitive."
        ),
    ] = None,
    polarizations: Annotated[
        list[str] | None,
        _arg_desc(
            "Pool filter: keep passes exposing any listed polarization. Missing values are dropped."
        ),
    ] = None,
    min_incidence: Annotated[
        float | None,
        _arg_desc(
            "Pool filter: minimum incidence angle in degrees. Passes missing it are dropped."
        ),
    ] = None,
    max_incidence: Annotated[
        float | None,
        _arg_desc(
            "Pool filter: maximum incidence angle in degrees. Passes missing it are dropped."
        ),
    ] = None,
    max_resolution: Annotated[
        float | None,
        _arg_desc(
            "Pool filter: keep passes this fine or finer, in metres. Missing resolution is dropped."
        ),
    ] = None,
    limit: Annotated[
        int,
        _arg_desc(
            "How many acquisitions to pull for a live or Canopy ranking. Default 500. "
            "Ignored on a local index, which groups the whole index."
        ),
    ] = 500,
    top: Annotated[
        int,
        _arg_desc("Maximum sites to return, best first. Default 20."),
    ] = 20,
    min_passes: Annotated[
        int,
        _arg_desc(
            "Minimum series depth. Default 2. Counts comparable passes when "
            "rank_by is comparable, otherwise raw dated passes."
        ),
    ] = 2,
    rank_by: Annotated[
        str,
        _arg_desc(
            "passes (default), comparable, recency, span, or cadence. "
            "Unknown values raise ValueError."
        ),
    ] = "passes",
    active_since: Annotated[
        str | None,
        _arg_desc("Keep sites whose newest pass is on or after this date. Same grammar as start."),
    ] = None,
    active_before: Annotated[
        str | None,
        _arg_desc(
            "Keep sites whose newest pass is on or before this date. "
            "A bare year or month includes that whole period."
        ),
    ] = None,
    first_since: Annotated[
        str | None,
        _arg_desc(
            "Keep sites whose earliest pass is on or after this date. Same grammar as start."
        ),
    ] = None,
    first_before: Annotated[
        str | None,
        _arg_desc(
            "Keep sites whose earliest pass is on or before this date. "
            "A bare year or month includes that whole period."
        ),
    ] = None,
    max_revisit_days: Annotated[
        float | None,
        _arg_desc(
            "Drop sites whose worst revisit gap exceeds this many days. "
            "Non-positive raises. Uses the comparable series when rank_by is comparable."
        ),
    ] = None,
    median_revisit_days: Annotated[
        float | None,
        _arg_desc(
            "Drop sites whose median revisit gap exceeds this many days. "
            "Non-positive raises. Uses the comparable series when rank_by is comparable."
        ),
    ] = None,
    min_span_days: Annotated[
        float | None,
        _arg_desc(
            "Drop sites whose first-to-last span is shorter than this many days. "
            "Non-positive raises. Uses the comparable series when rank_by is comparable."
        ),
    ] = None,
    max_span_days: Annotated[
        float | None,
        _arg_desc(
            "Drop sites whose first-to-last span is longer than this many days. "
            "Non-positive raises. Uses the comparable series when rank_by is comparable."
        ),
    ] = None,
    local: Annotated[
        bool | None,
        _arg_desc(
            "True uses only the local index; False forces a live walk. Omit to "
            "use the index when present. True cannot be combined with a Canopy token."
        ),
    ] = None,
) -> dict[str, Any]:
    """Rank sites imaged more than once, before a change tool is pointed at one.

    Use this when the site is not already known. Each site's ``hrefs`` are that
    site's STAC URLs, oldest first, ready for ``pick_change_interval``,
    ``change_composite``, or ``stack_stats``. Prefer this, or
    ``search_catalog(area=..., fuzzy=True)``, over ``semantic=True`` when the
    name shares words with a task. No model is called.

    Argument reference (ranking, recency, cadence, baseline, return fields):
    https://umbra-py.space/find-repeat-sites/

    Filters bound which acquisitions are ranked. ``bbox`` is WGS84
    ``[min_lon, min_lat, max_lon, max_lat]``. ``intersects`` is one GeoJSON
    Point, or a Polygon or MultiPolygon (or a Feature, FeatureCollection, or
    GeometryCollection wrapping those). A point matches footprints that
    contain it. It cannot be combined with ``bbox`` or ``place``. ``place`` is geocoded
    only when ``bbox`` is omitted. ``area`` is a task-name substring;
    ``fuzzy=True`` is a deterministic loose match. ``start`` / ``end`` bound
    which passes enter the pool (ISO date, bare year or month, or a relative
    expression). ``products`` is any of GEC, CSI, SIDD, SICD, CPHD.
    ``polarizations``, ``min_incidence``, ``max_incidence``, and
    ``max_resolution`` (metres) drop passes missing that field.

    ``top`` (default 20) is how many sites to return. ``min_passes`` (default
    2) is the minimum depth: comparable passes when ``rank_by`` is
    ``comparable``, otherwise raw dated passes. ``rank_by`` is ``passes``
    (default), ``comparable`` (largest same-polarization subset), ``recency``
    (newest pass), ``span`` (first to last), or ``cadence`` (tightest median
    gap). For the three temporal orders, ``min_passes`` still counts raw
    passes.

    ``active_since`` / ``active_before`` select sites by newest pass;
    ``first_since`` / ``first_before`` select by earliest pass. Those keep
    each survivor's full history, unlike ``start`` / ``end``, which truncate
    the pool. ``max_revisit_days`` caps the worst gap and
    ``median_revisit_days`` caps the median gap. ``min_span_days`` /
    ``max_span_days`` bound the first-to-last span. A non-positive cadence or
    span raises ``ValueError``. With ``rank_by="comparable"`` the cadence and
    span filters use that subset. A bare year or month on a ``*_before``
    bound includes the whole period.

    ``limit`` (default 500) sizes only a live or Canopy pool. A local index
    ranks each task over the whole index and ignores ``limit``. ``local``
    matches ``search_catalog``: unset uses the index when present. A Canopy
    token (``$UMBRA_CANOPY_TOKEN``) draws the pool from the commercial
    archive, and ``local=True`` cannot be combined with it.
    """
    from .coverage import (
        _check_max_revisit,
        _check_max_span,
        _check_median_revisit,
        _check_min_span,
        _check_ranking,
        rank_site_coverage,
        site_query_echo,
    )

    _check_ranking(rank_by)
    _check_max_revisit(max_revisit_days)
    _check_median_revisit(median_revisit_days)
    _check_min_span(min_span_days)
    _check_max_span(max_span_days)
    if intersects is not None and (bbox or place):
        raise ValueError("pass intersects or bbox/place, not both")

    resolved_bbox = cast("tuple[float, float, float, float] | None", tuple(bbox) if bbox else None)
    resolved_place = None
    if place and resolved_bbox is None:
        with _public_net_slot():
            resolved_bbox, resolved_place = _geocode_place(place)
    geometry = _parse_geometry(intersects) if intersects is not None else None

    token = _canopy_token()
    source, is_index = _search_source(local, token=token)
    label = _source_label(is_index, bool(token))
    try:
        if isinstance(source, CatalogIndex):
            # The index can answer a site's *whole-archive* depth as a GROUP BY
            # task, so route through rank_sites rather than re-listing a
            # limit-capped pool -- a deeply-imaged site is ranked by all its passes
            # rather than by whatever the first `limit` rows admitted (the
            # whole-archive drop-in, the same one `umbra sites --local` and
            # `GET /sites` already use). `limit` sizes only the live/token pool.
            sites = source.rank_sites(
                bbox=resolved_bbox,
                intersects=geometry,
                start=start,
                end=end,
                product_types=list(products) if products else None,
                area=area,
                fuzzy=fuzzy,
                polarizations=list(polarizations) if polarizations else None,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
                top=top,
                min_passes=min_passes,
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
        else:
            with _public_net_slot(long=True):
                pool = list(
                    source.search(
                        bbox=resolved_bbox,
                        intersects=geometry,
                        start=start,
                        end=end,
                        product_types=list(products) if products else None,
                        area=area,
                        fuzzy=fuzzy,
                        polarizations=list(polarizations) if polarizations else None,
                        min_incidence=min_incidence,
                        max_incidence=max_incidence,
                        max_resolution=max_resolution,
                        limit=limit,
                    )
                )
            sites = rank_site_coverage(
                pool,
                top=top,
                min_passes=min_passes,
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
    finally:
        if isinstance(source, CatalogIndex):
            source.close()

    return {
        "count": len(sites),
        "source": label,
        "resolved_place": resolved_place,
        "resolved_bbox": list(resolved_bbox) if resolved_bbox else None,
        "resolved_area": area,
        # Echo how the answer was ranked and selected (single-sourced with
        # GET/POST /sites), so a chain reads it from the result rather than its
        # own request. Dates are the raw expressions the caller passed.
        "query": site_query_echo(
            rank_by=rank_by,
            top=top,
            min_passes=min_passes,
            active_since=active_since,
            active_before=active_before,
            first_since=first_since,
            first_before=first_before,
            max_revisit_days=max_revisit_days,
            median_revisit_days=median_revisit_days,
            min_span_days=min_span_days,
            max_span_days=max_span_days,
        ),
        "sites": [site.to_dict() for site in sites],
        "attribution": ATTRIBUTION,
    }


def get_item(
    url: Annotated[
        str,
        _arg_desc(
            "STAC JSON URL, product filename, acquisition stem, or — when a "
            "Canopy token is set — a bare commercial-archive id."
        ),
    ],
) -> dict[str, Any]:
    """Return the full context card for one item.

    ``url`` is the JSON URL of an open-data STAC sidecar, a GEC/SICD (or other
    product) filename such as ``2025-05-17-17-43-46_UMBRA-08_GEC.tif``, or the
    acquisition stem ``2025-05-17-17-43-46_UMBRA-08``. A filename or stem is
    resolved through the local catalog index: the stem is matched against
    asset hrefs, which is what distinguishes ``sar-data/tasks/``,
    ``sar-data/task-data/``, and ``open-data/`` — the name itself does not.
    Zero matches is an error. More than one match lists the candidate STAC
    URLs; pass one of those. The hosted server resolves a filename or stem
    with the catalog index it already loaded. A local server does the same
    after ``umbra index fetch``.

    When the index contains a STAC URL, the indexed snapshot is returned —
    the same document ``search_catalog`` already answered from — rather than
    a live re-fetch that can disagree on asset filenames or product types.
    When the server is configured with a token (``$UMBRA_CANOPY_TOKEN``) and
    ``url`` is a bare acquisition id (no ``://``, and not a filename or stem),
    it is instead looked up in Umbra's authenticated commercial archive by a
    keyed STAC search — the retrieval complement to ``search_catalog`` over
    the paid catalog, since archive cards carry an ``id`` (not a fetchable
    sidecar URL) as their hand-off pointer. A filename or stem still resolves
    from the local open-data index when a token is set.

    On the public host a URL is fetched only when it is https on Umbra's
    public open-data bucket. Any other URL is refused before a request.
    A bare id that is not in the local index is refused the same way.

    The card (:meth:`UmbraItem.to_llm_context`) carries the id, ISO datetime,
    place, bbox, resolution, polarization *with the change-detection caveat*,
    the per-product-type explanations and asset URLs, and the mandatory
    attribution line — everything a model needs to reason about the scene.
    """
    if is_asset_name(url):
        try:
            return resolve_asset_name(url, readonly=_public_host()).to_llm_context()
        except CatalogError as exc:
            raise ValueError(asset_name_error_text(exc)) from exc
    token = _canopy_token()
    if token and "://" not in url:
        item = UmbraCatalog(token=token).get_item(url)
        if item is None:
            raise ValueError(f"No item {url!r} in the Canopy commercial archive.")
        return item.to_llm_context()
    return _fetch_item(url).to_llm_context()


def geocode_place(
    query: Annotated[str, _arg_desc("Free-text place name to resolve with Nominatim.")],
) -> dict[str, Any]:
    """Resolve a free-text place name to a bounding box via Nominatim.

    Returns ``{"bbox": [min_lon, min_lat, max_lon, max_lat], "display_name":
    ...}`` so the agent can inspect the box before searching, or pass it
    straight to ``search_catalog``'s ``bbox``.
    """
    with _public_net_slot():
        box, display = _geocode_place(query)
    return {"bbox": list(box), "display_name": display}


def index_stats() -> dict[str, Any]:
    """Report on the local catalog index, if one has been built or fetched.

    Lets a long-running agent decide whether searches will be instant (index
    present) or pay a live S3 walk, and how stale the snapshot is. ``end`` is
    the newest acquisition that is not ``datetime_suspect``.
    ``datetime_suspect_count`` is how many kept rows carry that flag.
    ``quarantined_future_count`` is how many source rows the last build
    dropped for a datetime more than a day ahead (they are not in the index).
    ``unique_captures`` is how many rows are the canonical copy of their
    collect; it equals ``items`` until republished copies have been linked.
    ``path`` is the index file locally and ``null`` on the public host, which
    does not reveal the server filesystem.
    """
    path = default_index_path()
    reported = None if _public_host() else str(path)
    if not path.exists():
        return {
            "available": False,
            "path": reported,
            "hint": (
                "No local index. Fetch the published weekly snapshot with "
                "'umbra index fetch' for instant whole-catalog search."
            ),
        }
    index = _open_index(path)
    try:
        stats = dict(index.stats())
    finally:
        index.close()
    stats["available"] = True
    stats["path"] = reported
    return stats


def _try_baked_quicklook(item: UmbraItem, *, asset: str) -> bytes | None:
    """Return PNG bytes if the index has a preview of this product."""
    lookup = _baked_previews()
    if lookup is None:
        return None
    baked = lookup(item.id)
    if baked is None:
        return None
    from .describe import BAKED_PREVIEW_ASSET

    recorded = (baked.asset or "").upper()
    assumed = recorded or BAKED_PREVIEW_ASSET
    if asset.upper() != assumed:
        return None
    return baked.png


def _baked_preview_missing(item_id: str) -> str:
    return (
        f"No baked preview for {item_id} in the local catalog index. Fetch the "
        "published sidecar with 'umbra index fetch-thumbnails' (or bake with "
        "'umbra index bake-thumbnails'). On the public community host, that "
        "sidecar is what lets quicklook return a picture without proxying Umbra "
        "COGs; a local `uvx --from 'umbra-py[mcp]' umbra-mcp` server can also "
        "stream a fresh render."
    )


def _image_record(
    png: bytes,
    *,
    source: str,
    asset: str,
    requested_max_size: int,
    db: bool,
) -> dict[str, Any]:
    """Machine-readable record of which picture was actually returned."""
    from .describe import _png_size

    size = _png_size(png)
    width, height = size if size else (None, None)
    substituted = source == "baked" and (width is None or width < requested_max_size)
    reason = None
    if source == "baked":
        reason = (
            "cached catalog thumbnail; this host does not stream Umbra COGs"
            if not _cog_streaming_allowed()
            else "cached catalog thumbnail preferred over a fresh COG stream"
        )
    return {
        "source": source,
        "asset": asset.upper(),
        "width": width,
        "height": height,
        "requested_max_size": requested_max_size,
        "db": db,
        "substituted": bool(substituted),
        "reason": reason,
    }


def _preview_caption(
    item: UmbraItem,
    record: dict[str, Any],
    *,
    extra: str | None = None,
) -> str:
    """Human caption plus a JSON sidecar so an agent can see a substitution."""
    lines: list[str] = []
    if record["source"] == "baked":
        dim = f"{record['width']}x{record['height']} px " if record.get("width") else ""
        asked = record["requested_max_size"]
        lines.append(f"BAKED PREVIEW: {dim}dB {record['asset']} of {item.id}.")
        if record.get("substituted"):
            lines.append(
                f"max_size={asked} was ignored — the cached thumbnail is smaller "
                "than requested. Do not claim structure finer than these pixels."
            )
        lines.append(
            "For a higher-resolution render (typically 1024 px, or any "
            "max_size), run a local server that streams the GeoTIFF: "
            "uvx --from 'umbra-py[mcp]' umbra-mcp."
        )
        lines.append("Or download the GEC href from get_item and open it locally (QGIS, rasterio).")
    else:
        lines.append(f"Rendered quicklook of {item.id} ({record['asset']}).")
    if extra:
        lines.append(extra)
    lines.append(ATTRIBUTION)
    lines.append(json.dumps({"image": record}, separators=(",", ":")))
    return "\n".join(lines)


def _quicklook_png(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    db: bool = False,
    max_size: int = 1024,
    preview: str = "auto",
) -> tuple[bytes, str]:
    """PNG bytes and caption for one scene, preferring a baked preview.

    Shared by the MCP image block and the LangChain / LlamaIndex render tools
    so all three front doors get the public-host baked path. ``preview`` is
    ``"auto"`` (baked if present, else stream), ``"baked"`` (refuse if missing)
    or ``"render"`` (always stream). A baked preview is always the decibel
    stretch; ``max_size`` applies only to a fresh render and is named in the
    caption when it was ignored.
    """
    from .describe import PREVIEW_SOURCES

    if preview not in PREVIEW_SOURCES:
        raise ValueError(
            f"Unknown preview source {preview!r}. Choose one of {', '.join(PREVIEW_SOURCES)}."
        )
    if preview != "render":
        png = _try_baked_quicklook(item, asset=asset)
        if png is not None:
            extra = (
                None if db else "Baked previews are always the decibel stretch (requested linear)."
            )
            record = _image_record(
                png, source="baked", asset=asset, requested_max_size=max_size, db=True
            )
            return png, _preview_caption(item, record, extra=extra)
        if preview == "baked":
            raise ValueError(_baked_preview_missing(item.id))
    _require_cog_streaming("quicklook")
    from . import viz

    image = viz.quicklook(item, asset=asset, db=db, max_size=max_size)
    png = _png_bytes(image)
    record = _image_record(png, source="rendered", asset=asset, requested_max_size=max_size, db=db)
    return png, _preview_caption(item, record)


def quicklook(
    url: Annotated[
        str,
        _arg_desc(
            "STAC JSON URL or index item id. The public host also accepts a "
            "product filename or acquisition stem that is in the index."
        ),
    ],
    asset: Annotated[
        str,
        _arg_desc(
            "Product to show. Default GEC. A baked preview is used only when its "
            "recorded product matches, or when it recorded none and this is GEC."
        ),
    ] = "GEC",
    db: Annotated[
        bool,
        _arg_desc(
            "Decibel stretch for a fresh render. Default false. A baked preview is always decibel."
        ),
    ] = False,
    max_size: Annotated[
        int,
        _arg_desc(
            "Longest side in pixels for a fresh render. Default 1024. Ignored when a smaller baked "
            "preview is returned."
        ),
    ] = 1024,
    preview: Annotated[
        str,
        _arg_desc(
            "auto (default) uses a baked preview when present, else renders. "
            "baked refuses if none. render always streams a COG and is refused on the public host."
        ),
    ] = "auto",
) -> list[Any]:
    """Return a scene's quicklook PNG as an image block.

    Prefers a baked catalog preview (``preview="auto"``, the default) so a
    public host can show the radar scene without proxying Umbra COGs.
    **A baked preview is typically 512 px** (the published weekly sidecar).
    ``max_size`` applies only to a fresh ``preview="render"`` stream; when a
    cached thumbnail is smaller than requested, the caption says
    ``max_size`` was ignored *and* includes a JSON
    ``{"image": {source, width, height, requested_max_size, substituted}}``
    sidecar. For a higher-resolution picture (typically 1024 px), run a
    local ``uvx --from 'umbra-py[mcp]' umbra-mcp`` server, or download the
    GEC href from ``get_item``. ``preview="baked"`` refuses rather than
    stream; ``preview="render"`` always streams (needs ``[viz]``, refused
    on ``umbra serve --public``).
    """
    _, Image = _require_mcp()
    item = _fetch_item(url)
    png, caption = _quicklook_png(item, asset=asset, db=db, max_size=max_size, preview=preview)
    return [Image(data=png, format="png"), caption]


def change_composite(
    urls: Annotated[
        list[str],
        _arg_desc(
            "STAC JSON URLs or item ids of 2 or 3 passes, in time order. Mixed polarizations are "
            "refused."
        ),
    ],
    asset: Annotated[str, _arg_desc("Product to composite. Default GEC.")] = "GEC",
    db: Annotated[
        bool,
        _arg_desc("Decibel stretch instead of a linear percentile stretch. Default false."),
    ] = False,
    max_size: Annotated[
        int,
        _arg_desc("Overview size cap in pixels for each pass. Default 1024."),
    ] = 1024,
) -> list[Any]:
    """Composite 2-3 passes of one site into a change image.

    Pass the STAC URLs of two or three acquisitions of the *same* site in time
    order. Colors encode change: bright green = signal appeared after the first
    date, magenta = signal vanished, grey = unchanged. Refuses to mix
    polarizations (HH vs VV are not comparable). Returns the image block plus a
    caption naming the color semantics and attribution -- and, when a pass carries
    no polarization metadata so same-polarization could not be *verified*, a
    structured caution text block naming that gap.
    """
    _require_cog_streaming("change_composite")
    _, Image = _require_mcp()
    from . import viz

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("change_composite needs at least two item URLs.")
    _require_same_polarization(items)
    image = viz.change_composite(items, asset=asset, db=db, max_size=max_size)
    caption = (
        "Change composite (green = appeared, magenta = vanished, grey = "
        f"unchanged) over {len(items)} passes. {ATTRIBUTION}"
    )
    blocks: list[Any] = [Image(data=_png_bytes(image), format="png")]
    advisory = _polarization_advisory(items)
    if advisory:
        blocks.append(advisory)
    blocks.append(caption)
    return blocks


def timescan(
    urls: Annotated[
        list[str],
        _arg_desc("STAC JSON URLs or item ids of at least two passes of one site."),
    ],
    asset: Annotated[str, _arg_desc("Product to scan. Default GEC.")] = "GEC",
    db: Annotated[
        bool,
        _arg_desc("Decibel stretch instead of a linear percentile stretch. Default false."),
    ] = False,
    max_size: Annotated[
        int,
        _arg_desc("Overview size cap in pixels for each pass. Default 1024."),
    ] = 1024,
) -> list[Any]:
    """Summarize a whole time series into one activity image.

    Pass the STAC URLs of a site's passes; the timescan encodes per-pixel
    temporal statistics so bright/colored areas are *where activity happened*
    over the series. Returns the image block plus attribution.
    """
    _require_cog_streaming("timescan")
    _, Image = _require_mcp()
    from . import viz

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("timescan needs at least two item URLs.")
    image = viz.timescan_composite(items, asset=asset, db=db, max_size=max_size)
    caption = f"Timescan over {len(items)} passes (color = temporal activity). {ATTRIBUTION}"
    return [Image(data=_png_bytes(image), format="png"), caption]


#: The tools whose answer *is* the picture -- they return MCP content blocks
#: rather than JSON, so :func:`build_server` registers them without structured
#: output (see the comment there). ``describe_scene`` / ``narrate_change`` are
#: added there too: they are defined later, and the no-key kit path returns an
#: image block.
_IMAGE_TOOLS = (quicklook, change_composite, timescan)


def stack_stats(
    urls: Annotated[
        list[str],
        _arg_desc(
            "STAC JSON URLs or item ids of at least two passes. Any order. Mixed polarizations are "
            "refused."
        ),
    ],
    asset: Annotated[str, _arg_desc("Product to measure. Default GEC.")] = "GEC",
    bbox: Annotated[
        list[float] | None,
        _arg_desc("Optional WGS84 [min_lon, min_lat, max_lon, max_lat] sub-area inside the scene."),
    ] = None,
    max_size: Annotated[
        int,
        _arg_desc("Overview size cap in pixels for the co-registered grid. Default 512."),
    ] = 512,
    extent: Annotated[
        str,
        _arg_desc(
            "intersection (default) keeps ground every pass covers. union keeps the full footprint."
        ),
    ] = "intersection",
    crs: Annotated[
        str,
        _arg_desc(
            "utm (default) grids in the site UTM zone so areas are km². EPSG:4326 returns areas as "
            "null."
        ),
    ] = "utm",
    change_threshold_db: Annotated[
        float,
        _arg_desc("Absolute decibel change that counts as moved ground. Default 3."),
    ] = 3.0,
    blocks: Annotated[
        int,
        _arg_desc(
            "N cuts the scene into an N by N grid of where change sits. 0 (default) skips it."
        ),
    ] = 0,
    block_series: Annotated[
        bool,
        _arg_desc(
            "With blocks, return every pass-to-pass record per block, not only the peak. Default "
            "false."
        ),
    ] = False,
    speckle_filter: Annotated[
        str | None,
        _arg_desc(
            "boxcar or lee, to average speckle before measuring. Omit to leave pixels unfiltered."
        ),
    ] = None,
    speckle_window: Annotated[
        int,
        _arg_desc(
            "Cell window for speckle_filter. Default 5. When a filter is set, "
            "an even value or one below 3 raises."
        ),
    ] = SPECKLE_WINDOW_DEFAULT,
) -> dict[str, Any]:
    """Measure how much a site changed across its passes, as numbers.

    The quantitative counterpart to ``change_composite`` / ``timescan``: those
    return a picture of *where* change is, this returns the *how much*. Pass the
    STAC URLs of a site's passes (2 or more, any order); they are co-registered
    onto one shared grid and reduced to a per-pass statistics series plus a net
    baseline-to-latest record — mean/median/spread of each pass, the signed
    decibel change against the pass before it, the fraction of ground that moved
    past ``change_threshold_db``, and that fraction as an **area in km²**.

    Change is always reported in decibels (a ratio of backscatter is a difference
    on the log scale). The default ``crs="utm"`` puts the grid in the site's UTM
    zone so every cell covers the same ground and the area figures mean
    something; pass ``crs="EPSG:4326"`` for a lon/lat grid, and the areas come
    back ``None`` rather than wrong. ``extent="intersection"`` (the default)
    compares only ground every pass covers; ``"union"`` keeps all of it and
    compares per cell where two passes overlap. ``bbox`` (lon/lat
    ``[min_lon, min_lat, max_lon, max_lat]``) narrows the measurement to a
    sub-area, e.g. one facility inside the scene.

    ``blocks=N`` adds the *where* to the *how much*: the scene is cut into an
    N x N grid (6 is a good default; 0, the default, skips it) and the extra
    ``spatial`` key reports each block's own net change, a compass label and
    lon/lat centre to locate it by, and the consecutive pair of passes that
    block moved most between — plus ``peak_block`` and an ASCII ``grid_text``
    heat-map of the net change. Ask for it whenever the question is *where on
    this site* something happened, or when a scene-wide mean looks small but the
    picture from ``change_composite`` shows an obvious hot spot: a change
    confined to one corner is averaged away scene-wide and stands out per block.

    ``block_series=True`` (which needs ``blocks``) keeps each block's *whole*
    pass-to-pass sequence rather than only its peak interval, so a block that
    drifted a little every pass reads differently from one that jumped once and
    held. It is the largest payload this tool emits — ``blocks`` × ``blocks`` ×
    (passes − 1) records — so ask for it only when the *shape* of the history is
    the question, not the size of the change.

    ``speckle_filter="boxcar"`` or ``"lee"`` averages **speckle** down before
    anything is measured. Speckle is not sensor noise: it is the interference
    pattern coherent illumination makes on rough ground, and on single-look
    imagery it scatters as widely as its own mean — so on a quiet site an
    unfiltered per-cell decibel delta is mostly interference rather than change.
    ``"boxcar"`` averages every window (most variance removed, blind to the edges
    it averages across); ``"lee"`` averages only where a window is no more
    variable than speckle alone explains, so edges and bright points survive.
    Reach for it when the change you are looking for is small, or when the
    numbers look noisy against a picture that looks quiet; leave it off when the
    question is about *fine* structure, because what it spends is resolution — a
    ``speckle_window``-cell window (odd, 5 by default) makes each cell report
    ground that many cells across. The returned ``caveats`` say so, so a quoted
    figure carries the trade with it.

    Refuses to mix polarizations (HH and VV are not comparable). **No model is
    called** — this is arithmetic over streamed COG overviews, so the numbers are
    reproducible and safe to quote. Read the ``caveats`` before interpreting
    them: Umbra's open products are not radiometrically calibrated, so a decibel
    value is relative to the same ground on another date, and look geometry moves
    backscatter too.
    """
    _require_cog_streaming("stack_stats")
    from .load import stack_stats as _stack_stats
    from .load import to_stack

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("stack_stats needs at least two item URLs.")
    _require_same_polarization(items)
    resolved_bbox = cast("tuple[float, float, float, float] | None", tuple(bbox) if bbox else None)
    cube = to_stack(
        items,
        asset=asset,
        bbox=resolved_bbox,
        max_size=max_size,
        db=True,
        extent=extent,
        crs=crs,
        speckle_filter=speckle_filter,
        speckle_window=speckle_window,
    )
    return _stack_stats(
        cube,
        change_threshold_db=change_threshold_db,
        blocks=blocks,
        block_series=block_series,
    )


def stack_provenance(
    urls: Annotated[
        list[str],
        _arg_desc(
            "STAC JSON URLs or item ids of at least two passes whose raster headers are compared."
        ),
    ],
    asset: Annotated[str, _arg_desc("Product whose raster header is read. Default GEC.")] = "GEC",
) -> dict[str, Any]:
    """Check whether a site's passes are one measurement, before measuring them.

    ``stack_stats`` refuses a selection whose rasters were made by *different*
    ``umbra convert`` settings — a calibrated pass differenced against an
    uncalibrated one puts the difference between two conversions on the time
    axis and reports it as change on the ground. This is the question that
    refusal answers, asked ahead of it: it reads each acquisition's ``UMBRA_*``
    conversion record straight from its raster header (kilobytes per pass, no
    pixels, no co-registration) and groups the selection by what its pixel
    values *are*.

    Returns ``agrees`` (whether ``stack_stats`` would accept the selection),
    ``groups`` largest-first — each with the conversion ``record`` its members
    share, their ``item_ids`` and the ``hrefs`` to re-run on — plus ``shared``
    (the record they all agree on) when they agree, ``refusal`` (verbatim the
    error ``stack_stats`` would raise) when they do not, and ``unreadable`` for
    any source that could not be opened at all, which is an incomplete answer
    rather than a mixed one.

    Reach for it in two situations. **After** a ``stack_stats`` call fails with
    a provenance refusal: ``groups[0]["hrefs"]`` is the largest set of passes
    that *do* agree, so re-running ``stack_stats`` on those is a measurement
    rather than a guess — the refusal itself cannot name that subset. And
    **before** measuring a long series you did not assemble yourself, since the
    check costs a header read per pass and saves the whole cube when the answer
    is no.

    A series of Umbra's published products as they ship (the common case) agrees
    trivially: every pass reads as ``"(unrecorded)"``, which is one value, so
    ``agrees`` is true and ``shared`` is empty. **No model is called.**
    """
    _require_cog_streaming("stack_provenance")
    from .load import stack_provenance as _stack_provenance

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("stack_provenance needs at least two item URLs.")
    _require_same_polarization(items)
    return _stack_provenance(items, asset=asset).to_dict()


def pick_change_interval(
    urls: Annotated[
        list[str],
        _arg_desc(
            "STAC JSON URLs or item ids of at least two passes. Any order. Mixed polarizations are "
            "refused."
        ),
    ],
    asset: Annotated[str, _arg_desc("Product to measure. Default GEC.")] = "GEC",
    max_size: Annotated[
        int,
        _arg_desc("Overview size cap in pixels for the co-registered grid. Default 512."),
    ] = 512,
    change_threshold_db: Annotated[
        float,
        _arg_desc("Absolute decibel change that counts as moved ground. Default 3."),
    ] = 3.0,
    extent: Annotated[
        str,
        _arg_desc(
            "intersection (default) keeps ground every pass covers. union keeps the full footprint."
        ),
    ] = "intersection",
    crs: Annotated[
        str,
        _arg_desc(
            "utm (default) grids in the site UTM zone so change fractions weigh equal ground."
        ),
    ] = "utm",
) -> dict[str, Any]:
    """Scan a site's whole series and name the two passes worth narrating.

    The deterministic first half of the "watch a site over time" story, and the
    step that turns a wall of passes into an answer. Given the STAC URLs of a
    site's passes (2 or more, any order), it co-registers them into a
    ``to_stack`` cube, reduces it with ``stack_stats``, and returns the *one*
    consecutive pass-pair whose measured change stands **furthest clear of the
    speckle detection floor** — the pair a reader should look at first. No model
    is called: a number picks the frames (the determinism boundary applied to
    frame selection), so the choice is reproducible and safe
    to quote.

    Why this exists as its own tool: ``narrate_change`` reads a *pair* of passes,
    but a site has many, and "which two?" is a measurement question, not a model
    one — a picture past three dates encodes nothing a model can separate, so the
    pair has to be chosen before the model is called. This answers it. The
    returned ``urls`` are the two chosen passes in time order, ready to hand
    straight to ``narrate_change`` (or ``change_composite`` to see them): the
    two tools chain into scan → narrate with no server and no model in the
    selection.

    Returns ``selected_interval`` (the interval's ``earlier_id`` / ``later_id``,
    their datetimes, the ``changed_fraction`` and signed ``mean_delta_db``, the
    cube's ``false_alarm_fraction`` speckle floor, the ``excess`` of the change
    over that floor, and ``stands_clear`` — whether the change cleared the floor
    by a usable margin), ``urls`` (the earlier then the later pass's STAC URL),
    and the license/attribution. When ``stands_clear`` is false the series' own
    largest change is still inside the speckle, so the pair is offered but the
    reader is warned the difference may be interference rather than change.

    ``selected_interval`` is ``None`` (and ``urls`` empty) when the series has
    fewer than two comparable passes or no interval reported any change — there
    is no pair to narrate. ``crs="utm"`` (the default) grids the cube in the
    site's UTM zone so the change fractions weigh equal ground; ``max_size`` and
    ``change_threshold_db`` match ``stack_stats``. Refuses to mix polarizations
    (HH vs VV are not comparable). Requires the ``load`` extra.
    """
    _require_cog_streaming("pick_change_interval")
    from .load import best_change_interval

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("pick_change_interval needs at least two item URLs.")
    _require_same_polarization(items)
    picked = best_change_interval(
        items,
        asset=asset,
        max_size=max_size,
        change_threshold_db=change_threshold_db,
        extent=extent,
        crs=crs,
    )
    if picked is None:
        return {
            "selected_interval": None,
            "urls": [],
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
        }
    earlier, later = picked["pair"]
    return {
        "selected_interval": picked["selection"],
        "urls": [earlier.href, later.href],
        "license": DATA_LICENSE,
        "attribution": ATTRIBUTION,
    }


def download_asset(
    url: Annotated[
        str,
        _arg_desc(
            "STAC JSON URL, product filename, or acquisition stem. A filename or stem is resolved "
            "through the catalog index."
        ),
    ],
    asset: Annotated[
        str,
        _arg_desc(
            "File to fetch: a product type (GEC), a schema v2 key (gec, gec_2), "
            "or the original filename. Default GEC."
        ),
    ] = "GEC",
    dest_dir: Annotated[
        str,
        _arg_desc(
            "Local download directory. Default '.'. Must stay inside "
            "UMBRA_MCP_DOWNLOAD_ROOT when that is set. On the public host it is only named in the "
            "link plan."
        ),
    ] = ".",
    confirm: Annotated[
        bool,
        _arg_desc(
            "False (default) returns the byte size and writes nothing. True downloads "
            "on a local server, or returns a link plan on the public host."
        ),
    ] = False,
) -> dict[str, Any]:
    """Download one asset of an item to local disk, gated by a size check.

    SAR products can be multiple GB, so this is a two-step handshake: call it
    first with ``confirm=False`` (the default) to get the byte size without
    downloading, then again with ``confirm=True`` to fetch it. On a
    local or stdio server, ``confirm=True`` returns ``{asset, path, bytes}``.
    ``confirm=False`` returns a size-and-hint object and does not write a file.

    On a public host (``umbra serve --public``, COG streaming off),
    ``confirm=True`` does not transfer the file. It returns a link plan:
    ``mode`` ``"link"``, ``downloaded`` false, ``asset``, ``filename`` (a
    basename), the direct public ``href``, ``bytes``, and ``how_to_fetch``.
    When the href is https on the public open-data bucket, ``how_to_fetch``
    is ``curl -L -C - -o <filename> <href>`` and
    ``umbra download ITEM --asset ASSET --dest DIR``. ``bytes`` is the STAC
    ``file:size`` or ``size`` when that is a non-negative integer; otherwise
    the host HEADs that href for ``Content-Length``, with no redirects and
    no retries. A href that is not on that bucket is not requested:
    ``how_to_fetch.curl`` and ``how_to_fetch.umbra`` are null, ``bytes`` is
    null, and ``note`` says the commands were omitted. A 404 or 410 from
    an allowlisted HEAD raises. Any other HEAD failure, including a full
    quick-request slot, leaves ``bytes`` null and still returns the plan.
    The hosted server never transfers the file. A local
    ``uvx --from 'umbra-py[mcp]' umbra-mcp`` server downloads to your own
    disk. ``confirm=False`` on this host returns the same size-and-hint
    object and does not write a file. It HEADs for ``Content-Length`` only
    when the href is https on the public open-data bucket, with no retries,
    and it does not follow redirects. A 404 or 410 raises, and so does any
    other non-redirect HEAD error. A redirect leaves ``bytes`` null. A href
    outside the bucket is not requested; ``bytes`` is the STAC ``file:size``
    or ``size`` when that is a non-negative integer, otherwise null. A local
    server still HEADs the href and follows redirects.

    On a local server, ``dest_dir`` is unchanged unless ``UMBRA_MCP_DOWNLOAD_ROOT``
    is set. When that variable is set, ``dest_dir`` must resolve inside it.
    The argument itself is unchanged.

    ``url`` is a STAC JSON URL, or a product filename
    (``2025-05-17-17-43-46_UMBRA-08_GEC.tif``) or acquisition stem
    (``2025-05-17-17-43-46_UMBRA-08``). A filename or stem is resolved through
    the catalog index the same way as ``get_item``. The hosted server already
    has that index. A local server needs ``umbra index fetch``.

    ``asset`` selects the file. All of these work:

    - a product type, any case: ``GEC``, ``gec``, ``SICD``
    - a schema v2 key, including a collision suffix: ``gec``, ``gec_2``,
      ``csi``, ``sidd``, ``csi_sidd``, ``sicd``, ``cphd``, ``metadata``,
      ``stac_metadata``, ``di_gif``, ``file``, ``file_tif``
    - the original filename (the asset's ``umbra-py:filename``)

    ``GEC`` and ``gec`` are the primary file. ``gec_2`` is the second GEC,
    not the primary. A name that matches nothing raises with the item's v2
    keys listed (``gec, sicd, cphd``, …), not the uppercase product types.
    """
    from ._http import DEFAULT_TIMEOUT, default_session
    from .download import confine_download_dir
    from .download import download_asset as _download_asset

    if not isinstance(url, str) or not url.strip():
        raise ValueError(
            "An item id is required (an item id or url from search_catalog); "
            "call search_catalog first."
        )
    if is_asset_name(url):
        try:
            item = resolve_asset_name(url, readonly=_public_host())
        except CatalogError as exc:
            raise ValueError(asset_name_error_text(exc)) from exc
    else:
        item = _fetch_item(url)
    href = item.asset_href(asset)

    if not confirm:
        # Local servers still HEAD whatever href the item carries and follow
        # redirects. The public host must not: a caller-supplied STAC item can
        # point the preview at loopback or the platform's private network.
        if _cog_streaming_allowed():
            session = default_session()
            head = session.head(href, allow_redirects=True, timeout=DEFAULT_TIMEOUT)
            head.raise_for_status()
            length = head.headers.get("Content-Length")
            size = int(length) if length is not None else None
        else:
            size = _public_confirm_preview_size(item, asset, href)
        return {
            "confirm_required": True,
            "asset": asset,
            "url": href,
            "bytes": size,
            "hint": (
                f"This asset is {size / 1e6:.1f} MB. " if size is not None else "Size unknown. "
            )
            + "Call again with confirm=true to download it.",
        }

    if not _cog_streaming_allowed():
        return _public_host_confirmed_download(item, asset, url, dest_dir, href)

    path = _download_asset(item, asset, confine_download_dir(dest_dir))
    return {"asset": asset, "path": str(path), "bytes": path.stat().st_size}


#: How many context cards a public ``watch_site`` returns. The full match
#: count is ``new_total``. A global bbox is tens of thousands of cards.
_PUBLIC_WATCH_ITEM_CAP = 50
#: Caller-controlled watch names become the ``watch`` field (and, locally,
#: a meta key). The public host refuses to echo an unbounded one.
_PUBLIC_WATCH_NAME_MAX = 128
_PUBLIC_WATCH_NOTE = (
    "This public host does not store watch state. Matches are reported as "
    "new and nothing is written, so a later call is not a delta. "
    f"new_items is capped at {_PUBLIC_WATCH_ITEM_CAP}; new_total is the full match count."
)


class _StatelessWatchStore:
    """Public-host watch store: every load misses, and save writes nothing.

    A process-wide in-memory map would still grow with caller-chosen names.
    Discarding the seen-set at the end of the call does not.
    """

    def load(self, name: str) -> set[str] | None:
        return None

    def save(self, name: str, keys: set[str]) -> None:
        return None


def _bounded_public_watch_name(name: str) -> str:
    """Cap a public watch name at :data:`_PUBLIC_WATCH_NAME_MAX` characters."""
    return name[:_PUBLIC_WATCH_NAME_MAX]


def watch_site(
    place: Annotated[
        str | None,
        _arg_desc("Free-text place. Geocoded to a bbox only when bbox is omitted."),
    ] = None,
    area: Annotated[
        str | None,
        _arg_desc("Task-name substring. With fuzzy, a loose token match."),
    ] = None,
    bbox: Annotated[
        list[float] | None,
        _arg_desc("WGS84 [min_lon, min_lat, max_lon, max_lat]. When set, place is not geocoded."),
    ] = None,
    fuzzy: Annotated[
        bool,
        _arg_desc("Loose task-name match for area. No model call."),
    ] = False,
    start: Annotated[
        str | None,
        _arg_desc("Inclusive acquisition-date lower bound. Same grammar as search_catalog start."),
    ] = None,
    end: Annotated[
        str | None,
        _arg_desc("Inclusive acquisition-date upper bound. Same grammar as search_catalog end."),
    ] = None,
    products: Annotated[
        list[str] | None,
        _arg_desc("Keep items that expose one of these types (GEC, CSI, SIDD, SICD, CPHD)."),
    ] = None,
    name: Annotated[
        str | None,
        _arg_desc(
            "Stable watch id. Omit to derive one from the filters. The public host keeps at most "
            "128 characters."
        ),
    ] = None,
    reset: Annotated[
        bool,
        _arg_desc(
            "True discards the stored baseline and reports every current match as new. Default "
            "false."
        ),
    ] = False,
    local: Annotated[
        bool | None,
        _arg_desc(
            "True uses only the local index; False forces a live walk. Omit to "
            "use the index when present. True cannot be combined with a Canopy token."
        ),
    ] = None,
) -> dict[str, Any]:
    """Report only the acquisitions **new** since the last check of this site.

    SAR's value for monitoring is its cadence, so the natural workflow is
    *standing*: run the same search each time and act only on what changed. This
    tool packages that idempotent delta — call it in one turn, come back a day
    (or a session) later and call it again, and it returns just the passes
    published in between, not the whole list again. The scheduler is you: ask the
    agent to check the site, and it reports the delta.

    Define the site with the same filters as ``search_catalog`` — ``place``
    (geocoded to a bbox), ``area`` (task/site-name substring, loosened by
    ``fuzzy``), ``bbox``, ``products``, and ``start``/``end`` date bounds. The
    watch is identified by a stable ``name`` derived from those filters (pass an
    explicit ``name`` to run several independent watches over the same site);
    ``reset=True`` re-establishes the baseline, reporting everything as new.

    State persists in the local catalog index's ``meta`` table (created on first
    use), so a watch survives across sessions with no extra setup. ``local``
    selects the search backend exactly as in ``search_catalog`` — leave it unset
    to use the on-disk index when present and a live S3 walk otherwise; for true
    monitoring a live walk (``local=false``) catches freshly published passes.
    **No model is called** — this is pure set arithmetic over the deterministic
    search — and the returned ``new_items`` are context cards ready to hand
    straight to ``change_composite`` / ``timescan``, closing the standing-analyst
    loop (new pass → composite → describe) without leaving the conversation.

    On the public host (``umbra serve --public``, COG streaming off) nothing
    is stored. Every call reports the current matches as new, says so in
    ``note``, and caps ``new_items`` at 50 (``new_total`` is the full count).
    The watch name is capped at 128 characters. A local server still persists
    the baseline in the index.
    """
    # bbox is a 4-element [minx, miny, maxx, maxy]; cast the length-erased
    # ``tuple(...)`` back to the fixed-arity bbox type watch_key expects.
    resolved_bbox = cast("tuple[float, float, float, float] | None", tuple(bbox) if bbox else None)
    resolved_place = None
    if place and resolved_bbox is None:
        with _public_net_slot():
            resolved_bbox, resolved_place = _geocode_place(place)

    token = _canopy_token()
    source, is_index = _search_source(local, token=token)
    public = _public_host()
    if public:
        # Do not open a writable index for the store. A live search has no
        # index connection; a local search's connection is ``mode=ro``.
        store_index = source if isinstance(source, CatalogIndex) else None
        store: Any = _StatelessWatchStore()
    else:
        # Watch state lives in the local index's meta table. Reuse the index
        # connection when the search already opened one; otherwise open the
        # index just for the store (created on first use).
        store_index = (
            source if isinstance(source, CatalogIndex) else CatalogIndex(default_index_path())
        )
        store = MetaWatchStore(store_index)

    resolved_products = list(products) if products else None
    watch_name = name or watch_key(
        area=area,
        place=place,
        bbox=resolved_bbox,
        product_types=resolved_products,
        start=start,
        end=end,
        fuzzy=fuzzy,
    )
    if public:
        watch_name = _bounded_public_watch_name(watch_name)
    try:
        # Both backends satisfy SearchSource at runtime (runtime_checkable); the
        # cast bridges mypy's stricter view of the loose ``**kwargs`` protocol.
        # A live walk holds one long-pool permit for the whole search.
        # An index-backed watch does not: it never leaves the machine.
        with _public_net_slot(long=True) if not is_index else nullcontext():
            result = watch(
                cast(SearchSource, source),
                name=watch_name,
                store=store,
                reset=reset,
                bbox=resolved_bbox,
                area=area,
                fuzzy=fuzzy,
                product_types=resolved_products,
                start=start,
                end=end,
            )
            new_total = len(result.new_items)
            if public and new_total > _PUBLIC_WATCH_ITEM_CAP:
                result.new_items = result.new_items[:_PUBLIC_WATCH_ITEM_CAP]
            payload = result.to_dict()
    finally:
        if store_index is not None:
            store_index.close()

    if public:
        payload["new_total"] = new_total
        payload["note"] = _PUBLIC_WATCH_NOTE
    payload["source"] = _source_label(is_index, bool(token))
    payload["resolved_place"] = resolved_place
    payload["resolved_bbox"] = list(resolved_bbox) if resolved_bbox else None
    return payload


def _scene_matches_payload(
    matches: list[Any], stored_model: str | None, query: dict[str, Any]
) -> dict[str, Any]:
    """Shape a list of :class:`~umbra_py.embed.SceneMatch` into a tool result.

    Each match is a pointer back to a real acquisition (id, task, datetime, STAC
    ``href``), never a model-authored fact — so the agent can hand a match's
    ``href`` straight to ``get_item`` / ``quicklook`` / ``change_composite`` to
    keep working. ``model`` records which embedding model the index was built
    with, and the attribution line rides along as with every other tool.
    """
    return {
        "query": query,
        "count": len(matches),
        "model": stored_model,
        "matches": [m.to_dict() for m in matches],
        "attribution": ATTRIBUTION,
    }


def find_similar(
    url: Annotated[
        str,
        _arg_desc(
            "STAC JSON URL or index item id of the scene to match. The public host also accepts a "
            "filename or stem in the index."
        ),
    ],
    asset: Annotated[
        str,
        _arg_desc(
            "Product whose quicklook is embedded. Default GEC. Match how the scene index was built."
        ),
    ] = "GEC",
    top_k: Annotated[int, _arg_desc("Maximum matches to return. Default 10.")] = 10,
    min_score: Annotated[
        float,
        _arg_desc("Minimum cosine similarity. Default 0. Matches below it are dropped."),
    ] = 0.0,
    model: Annotated[
        str | None,
        _arg_desc("Embedding model id. Must match the scene index. Omit for the server default."),
    ] = None,
) -> dict[str, Any]:
    """Find archived scenes that **look like** the acquisition at ``url``.

    This is the one search that lives in the pixels, not the metadata: where
    ``search_catalog`` filters by date/bbox/task-name, this renders the query
    item's quicklook, embeds it, and ranks the pre-embedded scene index by visual
    (cosine) similarity — "find scenes that look like this flooded field / crowded
    berth / runway". The query item is excluded from its own results.

    Requires a scene-embedding index built ahead of time with ``umbra embed
    build`` (a sidecar ``catalog.embed.db`` beside the catalog index) and a
    user-supplied multimodal embedding key (``OPENAI_API_KEY``, optionally
    ``OPENAI_BASE_URL`` / ``UMBRA_SCENE_EMBED_MODEL``) — the only model call is
    turning the query image into a vector; ranking is deterministic. ``asset``
    picks which product's quicklook to embed (default ``GEC``; match how the index
    was built); ``model`` must match the index's embedding model. Returns the
    ranked matches as compact records (each with the acquisition's ``href``, so
    hand it to ``get_item`` / ``quicklook`` / ``change_composite`` next) plus the
    attribution line.
    """
    _require_cog_streaming("find_similar")
    from . import embed as emb

    path = emb.default_scene_embed_path()
    if not path.exists():
        raise FileNotFoundError(
            _with_optional_path(
                "No scene-embedding index",
                path,
                "Build one first with "
                "'umbra embed build' (image-to-image similarity search needs it).",
            )
        )
    item = _fetch_item(url)
    embedder = emb.default_image_embedder(model=model)

    def render(it: UmbraItem) -> bytes:
        return emb._render_quicklook_asset(it, asset=asset)

    with emb.SceneEmbeddingIndex(path) as index:
        matches = index.similar_to_item(
            item, embedder=embedder, render=render, top_k=top_k, min_score=min_score
        )
        stored_model = index.stored_model()
    return _scene_matches_payload(
        matches, stored_model, {"kind": "image", "item_id": item.id, "asset": asset}
    )


def find_similar_text(
    query: Annotated[
        str,
        _arg_desc(
            "Plain-language description of the imagery to find, embedded in the same space as the "
            "scene index."
        ),
    ],
    top_k: Annotated[int, _arg_desc("Maximum matches to return. Default 10.")] = 10,
    min_score: Annotated[
        float,
        _arg_desc("Minimum cosine similarity. Default 0. Matches below it are dropped."),
    ] = 0.0,
    model: Annotated[
        str | None,
        _arg_desc(
            "Joint text-and-image model id. Must match the scene index. A dimension mismatch is an "
            "error."
        ),
    ] = None,
) -> dict[str, Any]:
    """Find archived scenes matching a plain-language ``query`` over their imagery.

    Text-to-scene search: embeds ``query`` ("ships at a berth", "a flooded field")
    and ranks the stored *image* vectors by cosine similarity. It only works when
    the index was built and this query is embedded with the **same** joint
    CLIP-family model whose text and image encoders share a vector space (a
    dimension mismatch is reported as an error; ``model`` selects it).

    Requires the same prebuilt ``catalog.embed.db`` (``umbra embed build``) and
    embedding key as ``find_similar``; the only model call is turning the text
    query into a vector. Returns the ranked matches as compact records (each with
    the acquisition's ``href`` for ``get_item`` / ``quicklook``) plus attribution.
    """
    from . import embed as emb

    path = emb.default_scene_embed_path()
    if not path.exists():
        raise FileNotFoundError(
            _with_optional_path(
                "No scene-embedding index",
                path,
                "Build one first with 'umbra embed build' (text-to-scene search needs it).",
            )
        )
    with _public_net_slot(long=True):
        text_embedder = emb.default_text_embedder(model=model)
        with emb.SceneEmbeddingIndex(path) as index:
            matches = index.similar_to_text(query, text_embedder, top_k=top_k, min_score=min_score)
            stored_model = index.stored_model()
    return _scene_matches_payload(matches, stored_model, {"kind": "text", "text": query})


def _baked_previews() -> Callable[[str], BakedPreview | None] | None:
    """A reader over the local index's baked quicklooks, or ``None`` if absent.

    The server's own :data:`~umbra_py.describe.BakedPreviews`, shaped like the
    CLI's ``_shared._baked_previews``: it is what lets ``describe_scene`` read a
    picture this machine already has instead of streaming a cloud-optimized
    overview per call, and it returns the index's record of *what* was baked so
    that path can tell a smaller version of the requested picture from a
    different one. ``None`` means "no index here", which the describe path turns
    into a refusal naming the fetch that would fix it.
    """
    path = default_index_path()
    if not path.exists():
        return None

    def lookup(item_id: str) -> BakedPreview | None:
        with _open_index(path) as index:
            return index.get_preview(item_id)

    return lookup


def _describe_kit_text(item: UmbraItem, record: dict[str, Any]) -> str:
    """Instruction block for a client-side reading when this host holds no vision key."""
    from .describe import _SAR_PRIMER

    card = item.to_llm_context()
    card.setdefault("polarization_caveat", POLARIZATION_CAVEAT)
    dim = f"{record['width']}x{record['height']} px " if record.get("width") else ""
    asked = record.get("requested_max_size")
    size_note = ""
    if record.get("substituted"):
        size_note = (
            f" This is a {dim}baked thumbnail, not a {asked} px render; "
            "max_size was ignored. Do not claim structure finer than these pixels."
        )
    return (
        "Reading kit — this host has no vision API key, so it does not call a "
        "model. The attached image plus the SAR rules below are the evidence; "
        "your next step is to produce the JSON and call stamp_description, "
        "which is the validation boundary. Until you do, any prose you write "
        "is your unvalidated look, not pipeline output.\n\n"
        f"Picture: {dim}{record['source']} {record['asset']} of {item.id}."
        f"{size_note}\n\n"
        "Produce ONE JSON object with keys summary (string), observed_features "
        "(array of strings), confidence (low|medium|high), caveats (array of "
        "strings). Do not invent coordinates, dates, or measurements — the "
        "metadata card is ground truth. Then call stamp_description("
        f"reading=<that object>, item_id={item.id!r}, asset={record['asset']!r}, "
        "image=<the image record below>) so the library can validate the shape "
        "and stamp CC-BY + AI provenance.\n\n"
        f"## SAR rules\n{_SAR_PRIMER}\n"
        "## Metadata card (this server's catalog snapshot — do not contradict)\n"
        f"{json.dumps(card, indent=2)}\n\n"
        "## Image record (pass to stamp_description)\n"
        f"{json.dumps(record, indent=2)}\n\n"
        f"{ATTRIBUTION}\n{AI_PROVENANCE}"
    )


def _narrate_kit_text(items: list[UmbraItem], stats: Any, asset: str) -> str:
    """Instruction block for a client-side change narration (no server vision key)."""
    from .describe import _SAR_PRIMER
    from .narrate import _change_card

    card = _change_card(items, asset)
    ids = [it.id for it in items]
    grid = stats.to_dict() if hasattr(stats, "to_dict") else stats
    return (
        "Reading kit — this host has no vision API key, so it does not call a "
        "model. The attached change composite and the dB grid below are the "
        "evidence. Produce the JSON, then call stamp_narration to validate it "
        "and stamp CC-BY + AI provenance. Until you do, any prose is your "
        "unvalidated look, not pipeline output.\n\n"
        f"Picture: change composite of {', '.join(ids)} "
        "(green = appeared, magenta = vanished, grey = unchanged).\n\n"
        "Narrate ONLY change the per-block dB grid supports. Produce ONE JSON "
        "object with keys summary (string), changes (array of strings), "
        "confidence (low|medium|high), caveats (array of strings). Then call "
        f"stamp_narration(reading=<that object>, item_ids={ids!r}, "
        f"asset={asset!r}, change_stats=<the grid below>).\n\n"
        f"## SAR rules\n{_SAR_PRIMER}\n"
        "## Acquisitions (ground truth)\n"
        f"{json.dumps(card, indent=2)}\n\n"
        "## Measured change (ground truth — do not invent numbers)\n"
        f"{json.dumps(grid, indent=2)}\n\n"
        f"{ATTRIBUTION}\n{AI_PROVENANCE}"
    )


def describe_scene(
    url: Annotated[
        str,
        _arg_desc(
            "STAC JSON URL or index item id. The public host also accepts a "
            "product filename or acquisition stem that is in the index."
        ),
    ],
    asset: Annotated[str, _arg_desc("Product to picture. Default GEC.")] = "GEC",
    db: Annotated[
        bool,
        _arg_desc(
            "Decibel stretch for a fresh render. Default true. A baked preview is always decibel."
        ),
    ] = True,
    max_size: Annotated[
        int,
        _arg_desc(
            "Longest side in pixels for a fresh render. Default 1024. A baked preview may be "
            "smaller; the kit says when this was ignored."
        ),
    ] = 1024,
    model: Annotated[
        str | None,
        _arg_desc(
            "Vision model id when this server holds an API key. Omit for the default. Unused on "
            "the no-key kit path."
        ),
    ] = None,
    preview: Annotated[
        str,
        _arg_desc(
            "auto (default) uses a baked preview when present, else renders. "
            "baked refuses if none. render always streams a COG and is refused on the public host."
        ),
    ] = "auto",
) -> dict[str, Any] | list[Any]:
    """Return a SAR-literate reading of the scene at ``url``.

    The picture and the metadata card are produced deterministically; a model
    only interprets them. How that model is reached depends on whether this
    process holds a vision key:

    - **No key** (the public community host): this tool does **not** call a
      vision model. It returns the PNG plus a reading kit (SAR rules, the
      metadata card, the JSON schema). Produce the JSON, then call
      :func:`stamp_description` — that is the validation boundary
      (provenance-stamped ``{summary, observed_features, confidence,
      caveats}``). Until you stamp it, your prose is not pipeline output.
      A baked preview is typically 512 px; the kit names the size and
      that a local server can stream a higher-resolution GeoTIFF.
    - **Key configured** (local stdio): sends the picture to that vision
      model, validates through ``parse_description``, and returns the
      stamped object directly.

    The packaged SAR rules are the value over just looking at ``quicklook``:
    brightness is backscatter not sunlight, speckle is not structure, a dark
    patch may be radar shadow not water, one frame is not change.

    ``preview`` defaults to ``"auto"``: a baked catalog preview if this server
    has one, else a fresh render. ``"render"`` is refused on the public host.
    """
    # Import the function directly: ``umbra_py.describe`` the attribute is the
    # re-exported function, not the submodule, so ``from . import describe`` would
    # bind the function. Its body still resolves ``default_describer`` / the render
    # from its own module globals, so the [ai]-key gating and offline stubbing work.
    from .describe import describe as _describe_scene
    from .describe import render_quicklook_png

    item = _fetch_item(url)
    previews = _baked_previews() if preview != "render" else None

    def _render(it: UmbraItem) -> bytes:
        _require_cog_streaming("describe_scene")
        return render_quicklook_png(it, asset=asset, max_size=max_size, db=db)

    if _model_key_configured():
        description = _describe_scene(
            item,
            model=model,
            asset=asset,
            max_size=max_size,
            db=db,
            preview=preview,
            previews=previews,
            render=_render,
        )
        return description.to_dict()

    from .describe import _obtain_image

    png, scene_image = _obtain_image(
        item,
        preview=preview,
        previews=previews,
        render=_render,
        asset=asset,
        max_size=max_size,
        db=db,
    )
    record = _image_record(
        png,
        source=scene_image.source,
        asset=scene_image.asset,
        requested_max_size=max_size,
        db=scene_image.db,
    )
    _, Image = _require_mcp()
    return [Image(data=png, format="png"), _describe_kit_text(item, record)]


def narrate_change(
    urls: Annotated[
        list[str],
        _arg_desc(
            "STAC JSON URLs or item ids of at least two passes, in time order. "
            "Mixed polarizations are refused. The public host refuses this tool."
        ),
    ],
    asset: Annotated[str, _arg_desc("Product to composite and measure. Default GEC.")] = "GEC",
    db: Annotated[
        bool,
        _arg_desc("Decibel stretch for the change picture. Default false."),
    ] = False,
    max_size: Annotated[
        int,
        _arg_desc("Overview size cap in pixels for the composite. Default 1024."),
    ] = 1024,
    model: Annotated[
        str | None,
        _arg_desc(
            "Vision model id when this server holds an API key. Omit for the default. Unused on "
            "the no-key kit path."
        ),
    ] = None,
) -> dict[str, Any] | list[Any]:
    """Return a grounded reading of *what changed* between passes.

    Sibling of :func:`describe_scene`. The composite and the per-block dB grid
    are produced deterministically; a model only narrates change those numbers
    support. Pass the STAC URLs in time order; mixed polarizations are refused.

    This tool **streams Umbra COGs** to build the composite, so a public
    community host refuses it (use a local ``uvx --from 'umbra-py[mcp]'
    umbra-mcp`` server, or describe each pass with :func:`describe_scene` /
    :func:`quicklook` on baked previews). On a local server:

    - **No vision key:** returns the composite as an MCP image block plus a
      reading kit (SAR change primer, the dB grid, the JSON schema). **You**
      (the client's model) narrate, grounded in the numbers.
    - **Key configured:** sends picture + grid to that vision model, validates
      through ``parse_narration``, and returns a provenance-stamped
      ``{summary, changes[], confidence, caveats[], change_stats}`` object.
    """
    _require_cog_streaming("narrate_change")
    # Import the function directly (as ``describe_scene`` does): the re-exported
    # ``umbra_py.narrate`` attribute is the function, so ``from .narrate import
    # narrate`` binds it while its body still resolves ``default_narrator`` / the
    # render from its own module globals, keeping the [ai]-key gating and the
    # offline stubbing intact.
    from .narrate import narrate as _narrate_change

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("narrate_change needs at least two item URLs.")
    _require_same_polarization(items)

    if _model_key_configured():
        narration = _narrate_change(items, model=model, asset=asset, max_size=max_size, db=db)
        return narration.to_dict()

    from .narrate import render_change_png

    png, stats = render_change_png(items, asset=asset, max_size=max_size, db=db)
    _, Image = _require_mcp()
    caption = _narrate_kit_text(items, stats, asset)
    return [Image(data=png, format="png"), caption]


def stamp_description(
    reading: Annotated[
        dict[str, Any],
        _arg_desc("Client JSON with summary, observed_features, confidence, and caveats."),
    ],
    item_id: Annotated[
        str | None,
        _arg_desc("Acquisition id to record on the stamped reading. Omit to leave it unset."),
    ] = None,
    asset: Annotated[str, _arg_desc("Product the reading describes. Default GEC.")] = "GEC",
    model: Annotated[
        str | None,
        _arg_desc("Model name to record. Default client-kit when omitted."),
    ] = None,
    image: Annotated[
        dict[str, Any] | None,
        _arg_desc(
            "Image record from the describe_scene kit. A smaller baked preview appends a caveat."
        ),
    ] = None,
) -> dict[str, Any]:
    """Validate a client-produced scene reading and stamp provenance.

    This is the interpretation boundary for the no-key kit path: you read the
    picture ``describe_scene`` returned, produce
    ``{summary, observed_features, confidence, caveats}``, and pass it here.
    The library checks the shape, stamps CC-BY and the AI-interpretation
    note (the model never sets those), and — when ``image`` is the record
    from the kit — appends a deterministic caveat if the picture was a
    smaller baked preview. Until this returns, the reading is unvalidated
    prose, not pipeline output.
    """
    from .describe import SceneImage, _baked_preview_caveat, parse_description

    description = parse_description(
        reading,
        item_id=item_id,
        model=model or "client-kit",
        asset=asset,
    )
    if image:
        scene = SceneImage(
            source=str(image.get("source") or "baked"),
            asset=str(image.get("asset") or asset),
            max_size=int(image.get("requested_max_size") or image.get("max_size") or 1024),
            db=bool(image.get("db", True)),
            width=int(image["width"]) if image.get("width") is not None else None,
            height=int(image["height"]) if image.get("height") is not None else None,
        )
        description.image = scene
        if scene.source == "baked" and (scene.width is None or scene.width < scene.max_size):
            caveat = _baked_preview_caveat(scene)
            if caveat not in description.caveats:
                description.caveats.append(caveat)
    return description.to_dict()


def stamp_narration(
    reading: Annotated[
        dict[str, Any],
        _arg_desc("Client JSON with summary, changes, confidence, and caveats."),
    ],
    item_ids: Annotated[
        list[str] | None,
        _arg_desc("Acquisition ids the narration covers."),
    ] = None,
    asset: Annotated[str, _arg_desc("Product the narration describes. Default GEC.")] = "GEC",
    model: Annotated[
        str | None,
        _arg_desc("Model name to record. Default client-kit when omitted."),
    ] = None,
    change_stats: Annotated[
        dict[str, Any] | None,
        _arg_desc(
            "Per-block decibel grid from the narrate_change kit, stored on the stamped object."
        ),
    ] = None,
) -> dict[str, Any]:
    """Validate a client-produced change narration and stamp provenance.

    Sibling of :func:`stamp_description` for the no-key ``narrate_change``
    kit. Pass the JSON you produced plus the ``change_stats`` grid from the
    kit so the stamped object carries the numbers the prose was grounded in.
    """
    from .narrate import parse_narration

    return parse_narration(
        reading,
        item_ids=item_ids,
        change_stats=change_stats,
        model=model or "client-kit",
        asset=asset,
    ).to_dict()


def run(
    *,
    http: bool = False,
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
) -> None:
    """Start the MCP server.

    stdio (the default) is what ``umbra-mcp`` / Claude Desktop speak. ``http``
    serves Streamable HTTP so a remote client can POST to ``path``. Stateless
    HTTP is on for the hosted case (Railway has no sticky sessions).
    """
    server = build_server()
    if not http:
        server.run()
        return
    from ._mcp_log import quiet_stateless_session_teardown

    # Same stateless transport as ``umbra serve --public``. The library logs
    # "Terminating session: None" after every request; that is DEBUG.
    quiet_stateless_session_teardown()
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
        stateless_http=True,
    )


def main() -> None:
    """Entry point for the ``umbra-mcp`` console script (stdio, no argv)."""
    run()


#: Tools that only read. Clients use ``readOnlyHint`` to skip an approval
#: prompt. ``download_asset`` writes a file when ``confirm`` is true on a
#: local server, and can truncate or delete its own ``.part`` / ``.etag``
#: sidecar. ``watch_site`` replaces the ``watch:<name>`` document in the
#: catalog index meta table; ``reset=True`` discards the previous baseline.
#: The public host writes neither, but the hints describe the tool.
_READ_ONLY_TOOLS = frozenset(
    {
        "search_catalog",
        "find_repeat_sites",
        "get_item",
        "geocode_place",
        "index_stats",
        "quicklook",
        "change_composite",
        "timescan",
        "stack_stats",
        "stack_provenance",
        "pick_change_interval",
        "find_similar",
        "find_similar_text",
        "describe_scene",
        "narrate_change",
        "stamp_description",
        "stamp_narration",
    }
)


def build_server() -> MCPServer:
    """Construct and return the configured ``MCPServer``.

    Kept as a factory (rather than a module-level singleton) so tests can build
    and introspect it without side effects, and so the ``mcp`` import stays
    lazy — importing this module does not require the extra until you build.
    """
    MCPServer, _Image = _require_mcp()
    from mcp_types import ToolAnnotations

    from umbra_py import __version__

    read_only = ToolAnnotations(read_only_hint=True)
    # destructive_hint is true because each can replace existing local data:
    # download_asset unlinks or truncates a ``.part`` / ``.etag``, and
    # watch_site overwrites the stored baseline (reset drops it outright).
    # A finished download is not overwritten (overwrite stays false).
    # idempotent_hint stays unset: a resumed partial download writes more
    # bytes, and a later watch stores a larger seen-set when the archive grows.
    writes = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
    )

    archive_note = (
        " This server is configured with a Canopy token, so search_catalog and "
        "get_item query Umbra's authenticated COMMERCIAL archive (source: "
        "'canopy-archive'); get_item takes an acquisition id, a STAC JSON URL, "
        "or a GEC/SICD filename / acquisition stem (the filename is resolved "
        "from the local open-data index)."
        if _canopy_token()
        else ""
    )
    server = MCPServer(
        "umbra",
        version=__version__,
        instructions=(
            "Search and visualize Umbra's open SAR archive. Start by reading "
            "the 'umbra://context' resource for product-type and search "
            "semantics. Use search_catalog to find acquisitions (compact cards; "
            "canonical=True keeps one copy of a republished collect), "
            "find_repeat_sites to rank the most repeat-imaged sites when you do "
            "not yet know which site to analyse (it returns each site's passes "
            "oldest-first, ready for pick_change_interval), "
            "get_item for one item's full metadata, and quicklook / "
            "change_composite / timescan to see the radar imagery. stack_stats "
            "answers the same change question in numbers (per-pass decibel "
            "statistics and how much ground moved, in km²; with blocks=N it "
            "also says which part of the site moved and when), and "
            "stack_provenance says whether a selection is one measurement "
            "before stack_stats is spent on it — and names the largest subset "
            "that is, when it is not. pick_change_interval scans a whole series "
            "and names the two passes whose change stands clearest of the speckle "
            "floor, so a long series can feed narrate_change without a model "
            "choosing the frames. describe_scene returns a SAR-literate reading "
            "of a scene: with no server vision key it returns a reading kit "
            "(picture + SAR rules); call stamp_description on the JSON you "
            "produce so the reading is validated and provenance-stamped — "
            "unvalidated prose is not pipeline output. With a key it calls a "
            "vision model. A baked quicklook is typically 512 px; the caption "
            "says when max_size was ignored and that a local "
            "`uvx --from 'umbra-py[mcp]' umbra-mcp` server can stream a "
            "higher-resolution GeoTIFF. narrate_change does the same for "
            "change (stamp_narration), grounded in a per-block decibel grid — "
            "and on a public host it is refused, because it would proxy Umbra "
            "COGs. Prefer search_catalog(area=..., fuzzy=True) "
            "or find_repeat_sites before semantic=True. All data "
            f"is {DATA_LICENSE}; keep the attribution line with any derived product." + archive_note
        ),
    )

    @server.custom_route("/healthz", methods=["GET"], name="healthz")
    async def _healthz(request: Any) -> Any:
        # Public, unauthenticated. Railway / Docker HEALTHCHECK hit this;
        # it is not an MCP tool. Starlette arrives with the mcp extra.
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True})

    for fn in (
        search_catalog,
        find_repeat_sites,
        get_item,
        geocode_place,
        index_stats,
        quicklook,
        change_composite,
        timescan,
        stack_stats,
        stack_provenance,
        pick_change_interval,
        download_asset,
        watch_site,
        find_similar,
        find_similar_text,
        describe_scene,
        narrate_change,
        stamp_description,
        stamp_narration,
    ):
        # Tools that answer with a picture return MCP content blocks (an
        # `Image` plus a caption), not JSON. The SDK derives a structured-
        # output schema from the return annotation and then serialises the
        # result against it -- which cannot represent an `Image`, so every call
        # raised `ToolError: Unable to serialize unknown type`. Opting them out
        # is what lets the radar scene reach the client at all; the JSON tools
        # keep structured output, which is worth having for them.
        image_tool = fn in _IMAGE_TOOLS or fn in (describe_scene, narrate_change)
        server.add_tool(
            _as_mcp_tool(fn),
            structured_output=False if image_tool else None,
            annotations=read_only if fn.__name__ in _READ_ONLY_TOOLS else writes,
        )

    @server.resource(
        "umbra://context",
        name="umbra-llm-context",
        description="Product-type table, search semantics and license rules for driving umbra-py.",
        mime_type="application/json",
    )
    def _context_resource() -> str:
        return json.dumps(llm_context(), indent=2)

    @server.resource(
        "umbra://index/stats",
        name="umbra-index-stats",
        description="Status of the local catalog index (built/fetched, item count, staleness).",
        mime_type="application/json",
    )
    def _index_resource() -> str:
        return json.dumps(index_stats(), indent=2)

    @server.prompt(
        name="monitor-site",
        description="Workflow: find a site's passes and composite the latest change.",
    )
    def monitor_site(place: str, start: str | None = None, end: str | None = None) -> str:
        window = f" between {start} and {end}" if start or end else ""
        return (
            f"Monitor '{place}' for change{window} using the umbra tools:\n"
            f"1. search_catalog(place='{place}'"
            f"{f', start={start!r}' if start else ''}"
            f"{f', end={end!r}' if end else ''}) to list the site's passes.\n"
            "2. Pick the two or three most recent passes of the same "
            "polarization (see each card's polarization_caveat). Skip a pass "
            "whose datetime_suspect is true; that datetime is not a latest "
            "acquisition.\n"
            "3. change_composite(urls=[...]) on their stac_href URLs.\n"
            "4. Describe what the green/magenta regions imply about activity, "
            "citing the acquisition dates and keeping the attribution line."
        )

    @server.prompt(
        name="watch-site",
        description=(
            "Standing workflow: report current matches; this host does not store watch state."
            if _public_host()
            else "Standing workflow: report only passes new since last check, then composite."
        ),
    )
    def watch_site_prompt(place: str, start: str | None = None, end: str | None = None) -> str:
        window = f" between {start} and {end}" if start or end else ""
        args = f"place='{place}', local=False"
        if start:
            args += f", start={start!r}"
        if end:
            args += f", end={end!r}"
        if _public_host():
            step = (
                f"1. watch_site({args}) — this public host does not store watch state, "
                "so the call reports the current matches as new (capped; see note and "
                "new_total) and writes nothing.\n"
            )
        else:
            step = (
                f"1. watch_site({args}) — it returns only the acquisitions new since the "
                "last check (all of them on the first run), persisting state so a later "
                "re-check reports just the delta.\n"
            )
        return (
            f"Check '{place}' for newly published passes{window} using the umbra tools:\n"
            f"{step}"
            "2. If new_count is 0, report that nothing new has arrived and stop.\n"
            "3. Otherwise pick two or three recent passes of the same polarization "
            "(see each new item's polarization_caveat) and change_composite(urls=[...]) "
            "on their stac_href URLs.\n"
            "4. Describe what the green/magenta regions imply about activity since the "
            "last check, citing the acquisition dates and keeping the attribution line."
        )

    @server.prompt(
        name="quantify-change",
        description="Workflow: measure how much a site changed, in decibels and km².",
    )
    def quantify_change(place: str, start: str | None = None, end: str | None = None) -> str:
        window = f" between {start} and {end}" if start or end else ""
        return (
            f"Quantify how much '{place}' changed{window} using the umbra tools:\n"
            f"1. search_catalog(place='{place}'"
            f"{f', start={start!r}' if start else ''}"
            f"{f', end={end!r}' if end else ''}) to list the site's passes.\n"
            "2. Keep the passes of a single polarization (see each card's "
            "polarization_caveat) — a polarization difference would read as change.\n"
            "3. stack_stats(urls=[...], blocks=6) on their stac_href URLs — it "
            "co-registers the series and returns per-pass decibel statistics, the "
            "signed change between consecutive passes, and net_change from the first "
            "pass to the last, including how much ground moved as changed_area_km2. "
            "blocks=6 adds the spatial breakdown: which part of the site moved, and "
            "which pair of passes it moved between (spatial.peak_block, and "
            "spatial.grid_text as a north-up map of the net change). Add "
            "block_series=True only if the question is the shape of a block's "
            "history — a steady drift versus one step — since it returns every "
            "pass-to-pass record rather than just each block's peak.\n"
            "4. timescan(urls=[...]) on the same URLs so you can also see *where* the "
            "activity sits, and check the picture agrees with the block grid.\n"
            "5. Report the trend with its numbers: name the pass-to-pass deltas that "
            "stand out, the net change and the changed area, and locate the change by "
            "the peak block's compass label and center_lonlat rather than by eye. Then "
            "read the caveats aloud — the products are not radiometrically calibrated, "
            "so the decibels are relative to the same ground on another date. Keep the "
            "attribution line."
        )

    @server.prompt(
        name="find-similar-scenes",
        description="Workflow: find archived scenes that look like an acquisition, then view them.",
    )
    def find_similar_scenes(url: str) -> str:
        return (
            f"Find archived scenes that look like the acquisition at {url} using the "
            "umbra tools (needs a scene index built with 'umbra embed build'):\n"
            f"1. find_similar(url='{url}') — ranks the pre-embedded archive by visual "
            "similarity to this scene's quicklook and returns the closest matches.\n"
            "2. If the result's count is 0, report that nothing cleared the similarity "
            "threshold (or that the index has not been built) and stop.\n"
            "3. Otherwise quicklook(url=<match.href>) on the top one or two matches so "
            "you can see them, and summarize what the matched scenes have in common, "
            "citing their acquisition dates and keeping the attribution line."
        )

    @server.prompt(
        name="describe-scene",
        description="Workflow: view a scene's quicklook and read it with SAR literacy.",
    )
    def describe_scene_prompt(url: str) -> str:
        return (
            f"Read the SAR acquisition at {url} using the umbra tools:\n"
            f"1. quicklook(url='{url}') so you can see the radar scene. If the "
            "caption says BAKED PREVIEW, that is a cached catalog thumbnail "
            "(typically 512 px) and a local "
            "`uvx --from 'umbra-py[mcp]' umbra-mcp` server can stream a "
            "higher-resolution GeoTIFF. Do not claim finer structure than "
            "the pixels you were shown.\n"
            f"2. describe_scene(url='{url}'). With a server vision key it returns "
            "a stamped {{summary, observed_features, confidence, caveats}} object. "
            "Without one it returns a reading kit: produce that JSON, then call "
            "stamp_description(reading=..., item_id=..., image=<kit image record>) "
            "before presenting it. Unstamped prose is not pipeline output.\n"
            "3. Present the stamped reading, noting it is an AI interpretation of "
            "radar (not a measurement) and keeping the attribution line. Remember "
            "SAR rules: bright is backscatter not sunlight, a dark patch may be "
            "shadow not water, and one frame shows no change over time."
        )

    @server.prompt(
        name="narrate-change",
        description="Workflow: composite two passes and read what changed, grounded in numbers.",
    )
    def narrate_change_prompt(place: str, start: str | None = None, end: str | None = None) -> str:
        window = f" between {start} and {end}" if start or end else ""
        return (
            f"Report what changed at '{place}'{window} using the umbra tools:\n"
            f"1. search_catalog(place='{place}'"
            f"{f', start={start!r}' if start else ''}"
            f"{f', end={end!r}' if end else ''}) to list the site's passes.\n"
            "2. Pick the two or three most recent passes of the same polarization "
            "(see each card's polarization_caveat). Skip a pass whose "
            "datetime_suspect is true; that datetime is not a latest "
            "acquisition.\n"
            "3. On a local server, change_composite(urls=[...]) on their stac_href "
            "URLs so you can see the green (appeared) / magenta (vanished) change, "
            "then narrate_change(urls=[...]) — with no server vision key that "
            "returns the composite plus the dB grid and you narrate; with a key "
            "it returns a stamped {{summary, changes, change_stats}} object. On "
            "the public host those two tools are refused (they would proxy Umbra "
            "COGs): quicklook / describe_scene each pass instead, using baked "
            "previews, and say what you can from the pictures without claiming "
            "a measured change.\n"
            "4. Present the narration as an AI interpretation of radar change "
            "(not a measurement), citing any change_stats blocks, and keeping "
            "the attribution line."
        )

    @server.prompt(
        name="survey-region",
        description="Workflow: survey what Umbra has imaged over a region.",
    )
    def survey_region(place: str) -> str:
        return (
            f"Survey Umbra's coverage of '{place}':\n"
            f"1. geocode_place('{place}') to get the bounding box.\n"
            "2. search_catalog(bbox=..., max_per_task=1) for a one-pin-per-site "
            "overview, then again without max_per_task for a promising site's "
            "full time series.\n"
            "3. quicklook(url=...) on a representative scene and summarize the "
            "products, dates and resolutions available, keeping attribution."
        )

    @server.prompt(
        name="search-by-description",
        description="Workflow: find a site by describing it when you don't know its name.",
    )
    def search_by_description(description: str) -> str:
        return (
            f"Find Umbra acquisitions of the site described as '{description}' when "
            "you don't know its exact task name:\n"
            f"1. If the description shares words with a likely task name (e.g. "
            "'beet piler'), try search_catalog(area='{description}', fuzzy=True) "
            "and/or find_repeat_sites(area='{description}', fuzzy=True) first — "
            "those are deterministic and need no embedding index or API key.\n"
            f"2. Only if that misses because the words do not appear in any task "
            "label, try search_catalog(area='{description}', semantic=True). That "
            "needs a semantic index ('umbra semantic build') and an embedding key, "
            "which a public host will not have.\n"
            "3. If semantic returns count 0, report that nothing matched (or that "
            "the index has not been built) rather than inventing a site.\n"
            "4. Summarize the acquisitions found (products, dates, resolutions), "
            "noting which task name the description resolved to and keeping the "
            "attribution line."
        )

    return server


if __name__ == "__main__":  # pragma: no cover
    main()
