"""HTML/SVG rendering of items for Jupyter ``_repr_html_``.

This module is deliberately dependency-free: it builds strings only, so the
rich notebook representation of an :class:`~umbra_py.models.UmbraItem` works in
the *core* install with no extras. The SAR pixel thumbnails (which do need
``rasterio`` and a network read) are produced elsewhere and passed in as
ready-made data URIs; here we only lay them out.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

from .constants import ATTRIBUTION
from .exceptions import AssetNotFoundError

if TYPE_CHECKING:
    from .models import UmbraItem

# A compact, scoped stylesheet. Re-emitting it per card is harmless (identical
# rules) and keeps each rendering a self-contained fragment.
_STYLE = """
<style>
.umbra-card{display:inline-flex;gap:12px;align-items:flex-start;border:1px solid #d0d7de;
border-radius:8px;padding:10px;margin:4px;font-family:-apple-system,Segoe UI,sans-serif;
font-size:12px;background:#fff;color:#1f2328;max-width:520px;vertical-align:top}
.umbra-card .umbra-pic{flex:0 0 auto;width:128px;height:128px;display:flex;
align-items:center;justify-content:center;background:#0d1117;border-radius:6px;overflow:hidden}
.umbra-card .umbra-pic img{width:128px;height:128px;object-fit:cover}
.umbra-card table{border-collapse:collapse}
.umbra-card td{padding:1px 6px 1px 0;vertical-align:top}
.umbra-card td.k{color:#656d76;white-space:nowrap}
.umbra-card .umbra-id{font-weight:600;word-break:break-all}
.umbra-card a{color:#0969da;text-decoration:none}
.umbra-gallery{display:flex;flex-wrap:wrap;gap:0}
</style>
"""


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value))


# Schemes we are willing to emit as a clickable link. Umbra STAC hrefs are
# absolute ``https`` URLs; anything else (notably ``javascript:``/``data:``, or
# a value that would break out of the quoted attribute) is refused so a hostile
# STAC document can't turn a generated map/gallery into a script-injection or a
# clickable exfiltration link.
_SAFE_HREF_SCHEMES = ("http://", "https://")


def safe_href(url: Any) -> str | None:
    """Return ``url`` escaped for an HTML attribute, or ``None`` if unsafe.

    Only ``http(s)`` URLs pass; the result is escaped with ``quote=True`` so it
    is safe inside either single- or double-quoted ``href`` attributes. Callers
    treat ``None`` as "omit the link" rather than emitting an untrusted scheme.
    These hrefs come from remote STAC JSON, so they are not trusted.
    """
    if not isinstance(url, str) or not url.startswith(_SAFE_HREF_SCHEMES):
        return None
    return escape(url, quote=True)


def _rings(coords: Any):
    """Yield each polygon ring as a list of ``(lon, lat)`` tuples.

    Handles Polygon and MultiPolygon coordinate nestings by recursing until a
    node looks like a ring (a list whose first element is a position).
    """
    if not isinstance(coords, (list, tuple)) or not coords:
        return
    first = coords[0]
    if (
        isinstance(first, (list, tuple))
        and len(first) >= 2
        and all(isinstance(v, (int, float)) for v in first[:2])
    ):
        yield [(float(p[0]), float(p[1])) for p in coords]
    else:
        for child in coords:
            yield from _rings(child)


def footprint_svg(item: UmbraItem, *, size: int = 128) -> str | None:
    """Draw an item's footprint polygon as a small standalone SVG.

    Returns ``None`` when the item has no usable geometry/bbox. The drawing is
    a schematic of the acquisition's ground footprint in lon/lat (north up),
    not a map — it gives instant spatial context with no tiles and no network.
    """
    geom = item.geometry
    bbox = item.bbox
    if not geom or bbox is None:
        return None
    min_lon, min_lat, max_lon, max_lat = bbox
    span_lon = max_lon - min_lon
    span_lat = max_lat - min_lat
    if span_lon <= 0 or span_lat <= 0:
        return None

    pad = 8
    inner = size - 2 * pad
    polys: list[str] = []
    for ring in _rings(geom.get("coordinates")):
        pts = []
        for lon, lat in ring:
            x = pad + (lon - min_lon) / span_lon * inner
            # SVG y grows downward; flip so north is up.
            y = pad + (max_lat - lat) / span_lat * inner
            pts.append(f"{x:.1f},{y:.1f}")
        if pts:
            polys.append(f'<polygon points="{" ".join(pts)}" />')
    if not polys:
        return None

    return (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        'xmlns="http://www.w3.org/2000/svg" '
        'style="background:#0d1117">'
        '<g fill="rgba(88,166,255,0.35)" stroke="#58a6ff" stroke-width="1.5" '
        'stroke-linejoin="round">' + "".join(polys) + "</g></svg>"
    )


def _metadata_rows(item: UmbraItem) -> str:
    info = item.metadata_summary()
    rng = info["resolution_range_m"]
    azi = info["resolution_azimuth_m"]

    def res(v: Any) -> str:
        return f"{v:.2f} m" if isinstance(v, (int, float)) else "?"

    inc = info["incidence_angle_deg"]
    inc_str = f"{inc:.1f}°" if isinstance(inc, (int, float)) else "—"
    rows = [
        ("acquired", info["datetime"] or "—"),
        ("platform", f"{info['platform'] or '—'} ({info['instrument_mode'] or '—'})"),
        ("product", info["product_type"] or "—"),
        ("pol", ", ".join(info["polarizations"]) or "—"),
        ("incidence", inc_str),
        ("resolution", f"{res(rng)} × {res(azi)}"),
        ("assets", ", ".join(info["available_assets"]) or "none"),
    ]
    out = "".join(f'<tr><td class="k">{_esc(k)}</td><td>{_esc(v)}</td></tr>' for k, v in rows)
    href = safe_href(item.href)
    if href:
        out += (
            '<tr><td class="k">item</td>'
            f'<td><a href="{href}" target="_blank" rel="noopener">STAC JSON</a></td></tr>'
        )
    return out


def item_card_html(item: UmbraItem, *, thumbnail: str | None = None) -> str:
    """Render one item as an HTML card (thumbnail/footprint + metadata table).

    ``thumbnail`` is an optional ``data:image/png;base64,...`` URI of a SAR
    quicklook; when absent the card falls back to the footprint SVG (or, if
    the item has no geometry, an empty pane).
    """
    if thumbnail:
        pic = f'<img src="{_esc(thumbnail)}" alt="SAR quicklook" />'
    else:
        pic = footprint_svg(item) or ""
    return (
        f'{_STYLE}<div class="umbra-card">'
        f'<div class="umbra-pic">{pic}</div>'
        f'<table><tr><td class="k">id</td>'
        f'<td class="umbra-id">{_esc(item.id)}</td></tr>'
        f"{_metadata_rows(item)}</table></div>"
    )


def gallery_html(items, *, thumbnails: dict[int, str | None] | None = None) -> str:
    """Render a sequence of items as a wrapping gallery of cards.

    ``thumbnails`` optionally maps each item's index to a data-URI thumbnail.
    """
    thumbnails = thumbnails or {}
    cards = "".join(
        item_card_html(item, thumbnail=thumbnails.get(i)) for i, item in enumerate(items)
    )
    count = len(items)
    plural = "" if count == 1 else "s"
    header = (
        '<div style="font-family:sans-serif;font-size:12px;color:#656d76">'
        f"{count} item{plural}</div>"
    )
    return f'{_STYLE}{header}<div class="umbra-gallery">{cards}</div>'


# A standalone dark "contact sheet": a thumbnail-dominant grid, distinct from
# the metadata-card layout above (which is tuned for an inline notebook repr).
_GALLERY_STYLE = """<style>
:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#e6edf3;
font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:18px 22px;border-bottom:1px solid #21262d}
header h1{margin:0;font-size:18px;font-weight:600}
header .sub{margin:4px 0 0;color:#8b949e;font-size:13px}
main.umbra-grid{display:grid;gap:14px;padding:18px 22px;
grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
main.umbra-groups{padding:18px 22px;display:flex;flex-direction:column;gap:26px}
.umbra-group{display:flex;flex-direction:column;gap:12px}
.umbra-group .group-title{margin:0;padding-bottom:8px;border-bottom:1px solid #21262d;
font-size:15px;font-weight:600;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.umbra-group .group-count{color:#8b949e;font-size:12px;font-weight:400}
.umbra-group .umbra-grid{padding:0}
.umbra-tile{background:#161b22;border:1px solid #21262d;border-radius:8px;
overflow:hidden;display:flex;flex-direction:column}
.umbra-tile .thumb{display:flex;align-items:center;justify-content:center;
aspect-ratio:1/1;background:#010409}
.umbra-tile .thumb img{width:100%;height:100%;object-fit:cover;display:block}
.umbra-tile .thumb svg{width:60%;height:60%}
.umbra-tile .empty{color:#484f58;font-size:12px}
.umbra-tile figcaption{padding:8px 10px;display:flex;gap:8px;align-items:flex-start}
.umbra-tile .fp{flex:0 0 auto;line-height:0}
.umbra-tile .meta{min-width:0;flex:1;display:flex;flex-direction:column;gap:1px}
.umbra-tile .tid{font-weight:600;font-size:12px;word-break:break-all}
.umbra-tile .when{color:#8b949e;font-size:11px}
.umbra-tile a{color:#58a6ff;text-decoration:none}
.umbra-tile a:hover{text-decoration:underline}
.umbra-tile details.urls{margin-top:4px;font-size:11px}
.umbra-tile details.urls summary{cursor:pointer;color:#58a6ff}
.umbra-tile .urow{margin-top:4px;display:flex;flex-direction:column;gap:1px}
.umbra-tile .ulbl{color:#6e7681;font-size:10px;text-transform:uppercase;letter-spacing:.04em}
.umbra-tile code.u{display:block;background:#0d1117;border:1px solid #21262d;border-radius:4px;
padding:3px 5px;color:#c9d1d9;word-break:break-all;user-select:all;
font:11px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace}
footer{padding:14px 22px;color:#6e7681;font-size:12px;border-top:1px solid #21262d}
</style>"""


def _asset_url(item: UmbraItem, asset: str) -> str | None:
    """The item's public URL for ``asset`` (e.g. the GEC GeoTIFF), or None.

    :meth:`UmbraItem.asset_href` raises when the product isn't present and can
    return an empty string when no public URL is derivable; collapse both to
    ``None`` so a tile simply omits the row rather than showing a dead value.
    """
    try:
        url = item.asset_href(asset)
    except AssetNotFoundError:
        return None
    return url or None


def _url_panel_html(item: UmbraItem, asset: str) -> str:
    """A collapsible panel of copyable URLs for feeding into other commands.

    Shows the rendered asset's direct download URL (e.g. the GEC GeoTIFF) and
    the STAC item URL, each in a click-to-select code box (``user-select:all``,
    so a single click grabs the whole string — no JavaScript). The STAC item
    URL is what ``umbra info | download | quicklook | load`` consume; the asset
    URL is the direct file for ``curl`` / GDAL ``/vsicurl`` / rasterio. Rows for
    URLs that can't be resolved are simply omitted.
    """
    rows: list[tuple[str, str]] = []
    asset_url = _asset_url(item, asset)
    if asset_url:
        rows.append((asset, asset_url))
    if item.href:
        rows.append(("STAC item", item.href))
    if not rows:
        return ""
    body = "".join(
        f'<div class="urow"><span class="ulbl">{_esc(label)}</span>'
        f'<code class="u">{_esc(url)}</code></div>'
        for label, url in rows
    )
    return f'<details class="urls"><summary>URLs</summary>{body}</details>'


def _gallery_tile_html(item: UmbraItem, *, thumbnail: str | None = None, asset: str = "GEC") -> str:
    """Render one gallery tile: SAR thumbnail (or footprint fallback) + caption.

    The thumbnail links to the item's STAC JSON. When a SAR preview is present
    a small footprint sketch is shown beside the caption for spatial
    orientation (the cover-cropped thumbnail loses the footprint's true shape);
    when no preview could be fetched, the footprint sketch fills the picture
    pane instead, so every tile still shows *something* placeable. A
    collapsible panel exposes the asset + STAC URLs for use in other commands
    (see :func:`_url_panel_html`); ``asset`` selects which product's URL to show.
    """
    info = item.metadata_summary()
    when = _esc(info["datetime"] or "—")
    plat = info["platform"] or "—"
    mode = info["instrument_mode"]
    plat_line = _esc(f"{plat} · {mode}" if mode else plat)
    href = safe_href(item.href)

    if thumbnail:
        pic = f'<img src="{_esc(thumbnail)}" loading="lazy" alt="SAR quicklook" />'
        badge = footprint_svg(item, size=40)
    else:
        pic = footprint_svg(item) or '<span class="empty">no preview</span>'
        badge = None  # the picture pane already shows the footprint

    if href:
        thumb = f'<a class="thumb" href="{href}" target="_blank" rel="noopener">{pic}</a>'
        link = f'<a href="{href}" target="_blank" rel="noopener">STAC item ↗</a>'
    else:
        thumb = f'<span class="thumb">{pic}</span>'
        link = ""

    badge_html = f'<span class="fp" title="footprint (north up)">{badge}</span>' if badge else ""
    return (
        '<figure class="umbra-tile">'
        f"{thumb}"
        "<figcaption>"
        f"{badge_html}"
        '<span class="meta">'
        f'<span class="tid">{_esc(item.id)}</span>'
        f'<span class="when">{when}</span>'
        f'<span class="when">{plat_line}</span>'
        f"{link}"
        f"{_url_panel_html(item, asset)}"
        "</span>"
        "</figcaption>"
        "</figure>"
    )


def standalone_gallery_html(
    items,
    *,
    thumbnails: dict[int, str | None] | None = None,
    title: str = "Umbra SAR gallery",
    subtitle: str | None = None,
    asset: str = "GEC",
) -> str:
    """Render items as a self-contained HTML contact-sheet page.

    Unlike :func:`gallery_html` (an inline fragment for a notebook repr), this
    returns a *full* HTML document: a thumbnail grid you can open straight off
    disk. ``thumbnails`` maps each item's index to a ``data:image/png`` URI;
    tiles without one fall back to their footprint sketch. ``asset`` selects
    which product's download URL each tile exposes in its URL panel.

    When the items span more than one Umbra task, they're laid out as labelled
    per-task sections so repeat passes of the same site sit next to each other;
    a single-task gallery stays a flat grid.
    """
    items = list(items)
    thumbnails = thumbnails or {}

    # Group acquisitions by their Umbra task so repeat passes of one site sit
    # together under a labelled heading -- the natural way to compare the same
    # place over time. Tasks keep first-seen order (the catalog already streams
    # task-by-task), as do the items within each task. When the items span only
    # a single task there's nothing to separate, so we fall back to the flat
    # grid rather than wrap everything in a redundant section.
    groups: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        groups.setdefault(item.task or "", []).append(i)

    def _tiles(indices) -> str:
        return "".join(
            _gallery_tile_html(items[i], thumbnail=thumbnails.get(i), asset=asset) for i in indices
        )

    if len(groups) > 1:
        sections = []
        for label, indices in groups.items():
            n = len(indices)
            tail = "" if n == 1 else "s"
            heading = label or "Other acquisitions"
            sections.append(
                '<section class="umbra-group">'
                f'<h2 class="group-title">{_esc(heading)}'
                f'<span class="group-count">{n} acquisition{tail}</span></h2>'
                f'<div class="umbra-grid">{_tiles(indices)}</div>'
                "</section>"
            )
        body = f'<main class="umbra-groups">{"".join(sections)}</main>'
    else:
        body = f'<main class="umbra-grid">{_tiles(range(len(items)))}</main>'

    count = len(items)
    plural = "" if count == 1 else "s"
    n_thumbs = sum(1 for v in thumbnails.values() if v)
    sub = f"{_esc(subtitle)} · " if subtitle else ""
    head_sub = f"{sub}{count} acquisition{plural}"
    if len(groups) > 1:
        head_sub += f" · {len(groups)} tasks"
    if n_thumbs < count:
        head_sub += f" · {n_thumbs} with SAR preview"

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"{_GALLERY_STYLE}\n</head>\n<body>\n"
        f'<header><h1>{_esc(title)}</h1><p class="sub">{head_sub}</p></header>\n'
        f"{body}\n"
        f"<footer>{_esc(ATTRIBUTION)} Generated by umbra-py.</footer>\n"
        "</body>\n</html>\n"
    )
