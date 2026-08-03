"""The local sidecar databases: ``umbra index``, ``umbra semantic``,
``umbra embed``.

Three Click sub-groups over the three SQLite sidecars -- the catalog index
(plus its baked place labels and thumbnails), the semantic task-name index,
and the scene-embedding index -- each buildable locally or fetchable from the
published weekly snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .._spinner import OrbitSpinner
from ..constants import PRODUCT_ASSETS
from ..exceptions import UmbraError
from ..export import export_geoparquet
from ..index import (
    CatalogIndex,
    default_thumbs_path,
    fetch_prebuilt_thumbnails,
)
from ..models import UmbraItem
from . import _shared
from ._root import cli


@cli.group()
def index() -> None:
    """Build and inspect a local SQLite catalog index for fast offline search.

    Umbra has no STAC API, so a live search re-walks S3 every time. Index the
    archive once into a local database, then run 'umbra search --local' for
    near-instant repeat searches over the same data.
    """


@index.command("build")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Output index database (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Created if missing; existing rows are "
    "refreshed and new ones added (incremental).",
)
@click.option("--bbox", help="Scope the build to a footprint: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Scope the build to a geocoded place name (mutually exclusive with --bbox).",
)
@_shared._geometry_option
@click.option(
    "--start",
    help="Scope to acquisitions on/after this date. YYYY-MM-DD, a year/month, "
    "or relative ('3 months ago', 'last month').",
)
@click.option(
    "--end",
    help="Scope to acquisitions on/before this date (same formats as --start).",
)
@click.option(
    "--area",
    default=None,
    help="Scope to one Umbra task/site by name (e.g. 'Centerfield'). Much "
    "faster than a full walk -- it lists just that task.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap how many acquisitions to index this run (default: no cap -- index "
    "everything in scope).",
)
@_shared._fuzzy_option
def index_build(db_path, bbox, place, intersects, start, end, area, fuzzy, limit) -> None:
    """Walk Umbra's archive and persist matching acquisitions into the index.

    With no scope flags this indexes the whole bucket, which lists every task
    and takes a while; pass --area/--bbox/--place/--intersects/--start/--end to
    index just the slice you care about.
    """
    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    path = _shared._index_path(db_path)
    in_scope = any((search_bbox, search_geometry, start, end, area))
    scope = "matching acquisitions" if in_scope else "Umbra archive"
    with OrbitSpinner(f"Indexing {scope}") as spinner:
        # A full-bucket build runs for a while, so show a live tally instead of
        # an inscrutable spinner. The spinner repaints its label each frame.
        def tally(n: int) -> None:
            spinner.label = f"Indexing {scope} ({n} so far)"

        with CatalogIndex(path) as idx:
            written = idx.build(
                progress=tally,
                bbox=search_bbox,
                intersects=search_geometry,
                start=start,
                end=end,
                area=area,
                fuzzy=fuzzy,
                limit=limit,
            )
            total = len(idx)
    click.echo(f"Indexed {written} acquisition(s); index now holds {total}. ({path})")


@index.command("update")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index to refresh (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/"
    "catalog.db). Must already exist -- create one with 'index fetch'/'build'.",
)
@click.option(
    "--overlap-days",
    type=int,
    default=1,
    show_default=True,
    help="Re-scan this many days before the newest indexed acquisition to catch "
    "near-real-time publish lag. Widen it (or run 'index build') if back-dated "
    "late arrivals matter.",
)
@click.option(
    "--since",
    default=None,
    help="Force the acquisition-date lower bound (YYYY-MM-DD, a year/month, or "
    "relative like '2 weeks ago') instead of deriving it from the index.",
)
@click.option(
    "--bbox",
    help="Scope the refresh to a footprint 'min_lon,min_lat,max_lon,max_lat' "
    "(match the scope the index was built with).",
)
@click.option(
    "--place",
    default=None,
    help="Scope the refresh to a geocoded place name (mutually exclusive with --bbox).",
)
@_shared._geometry_option
@click.option(
    "--area",
    default=None,
    help="Scope the refresh to one Umbra task/site by name (e.g. 'Centerfield').",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap how many acquisitions to add this run (default: no cap).",
)
@_shared._fuzzy_option
def index_update(db_path, overlap_days, since, bbox, place, intersects, area, fuzzy, limit) -> None:
    """Cheaply refresh an existing index by re-walking only recent acquisitions.

    'umbra index build' fetches a sidecar for every acquisition in scope; on a
    snapshot only days old that re-reads mostly-unchanged data. 'update' instead
    derives a start date from the newest acquisition already indexed (minus
    --overlap-days) and walks only from there, so a weekly refresh reads just
    the new passes. Bootstrap the index first with 'umbra index fetch' or
    'umbra index build'.
    """
    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(
            f"No index at {path}. Create one first with 'umbra index fetch' or 'umbra index build'."
        )
    with OrbitSpinner("Refreshing index") as spinner:

        def tally(n: int) -> None:
            spinner.label = f"Refreshing index ({n} scanned)"

        with CatalogIndex(path) as idx:
            result = idx.update(
                overlap_days=overlap_days,
                since=since,
                progress=tally,
                bbox=search_bbox,
                intersects=search_geometry,
                area=area,
                fuzzy=fuzzy,
                limit=limit,
            )
            total = len(idx)
    frm = result.start.isoformat() if result.start else "the whole catalog"
    click.echo(
        f"Refreshed from {frm}: {result.added} new, {result.refreshed} refreshed; "
        f"index now holds {total}. ({path})"
    )


@index.command("bake")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index to label (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/"
    "catalog.db). Must already exist -- create one with 'index fetch'/'build'.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap how many geocode lookups to make this run (default: no cap). "
    "Reverse geocoding is throttled to ~1/sec, so use this to bake a large "
    "catalog in bounded batches -- re-run to continue where it left off.",
)
@click.option(
    "--by-site",
    is_flag=True,
    help="Geocode once per site instead of once per acquisition: passes sharing "
    "a task and a ~11 km cell take one label resolved from their mean centroid. "
    "Cuts the throttled lookups by the average passes-per-site, which is what "
    "makes labelling a whole catalog practical.",
)
@click.option(
    "--zoom",
    type=int,
    default=10,
    show_default=True,
    help="Nominatim address granularity: 3 = country, 8 = county, 10 = city, "
    "14 = suburb, 18 = building.",
)
def index_bake(db_path, limit, by_site, zoom) -> None:
    """Reverse-geocode indexed acquisitions and cache their place labels.

    Turns each acquisition's footprint into a human place name ("Reykjavik,
    Iceland") once and stores it in the index, so 'umbra demo', maps and
    galleries built with --local show real place labels instantly instead of
    re-geocoding at render time (OpenStreetMap Nominatim caps traffic at ~1
    request/sec, so labelling thousands of items live is impractical).

    Umbra files every pass over a site under one task, so --by-site resolves a
    site once and labels all of its passes -- the mode to use for a whole
    catalog, where most acquisitions are repeat passes over ground already
    geocoded.

    Idempotent: only items without a label yet are geocoded, so re-running
    labels just what was added since. Bootstrap the index first with 'umbra
    index fetch' or 'umbra index build'.
    """
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(
            f"No index at {path}. Create one first with 'umbra index fetch' or 'umbra index build'."
        )
    with OrbitSpinner("Baking place labels") as spinner:

        def tally(n: int) -> None:
            spinner.label = f"Baking place labels ({n} lookup(s))"

        with CatalogIndex(path) as idx:
            labelled = idx.bake_places(limit=limit, zoom=zoom, by_site=by_site, progress=tally)
            s = idx.stats()
    click.echo(
        f"Baked {labelled} new place label(s); {s['labeled']} of {s['items']} "
        f"acquisition(s) now labelled. ({path})"
    )


@index.command("bake-thumbnails")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index to bake into (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/"
    "catalog.db). Must already exist -- create one with 'index fetch'/'build'.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap how many acquisitions to render this run (default: no cap). Each "
    "thumbnail streams a scene's overview from S3, so use this to bake a large "
    "catalog in bounded batches -- re-run to continue where it left off.",
)
@click.option(
    "--size",
    type=int,
    default=256,
    show_default=True,
    help="Longest edge of the baked PNG, in pixels.",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    help="Which asset to render the preview from (the geocoded GEC by default).",
)
@click.option(
    "--newest-first",
    is_flag=True,
    help="Bake the most recently acquired scenes first instead of in catalog "
    "order, so a --limit run spends its budget on the freshest passes -- the "
    "ones a demo or a monitoring view opens on.",
)
def index_bake_thumbnails(db_path, limit, size, asset, newest_first) -> None:
    """Render a small SAR quicklook per acquisition and cache it in the index.

    Bakes a downsampled PNG preview for every indexed acquisition once, so
    'umbra serve's GET /artifacts/thumbnail/{id}.png -- and any demo/gallery
    reading it -- shows a scene instantly from local bytes instead of
    re-streaming its cloud-optimized GeoTIFF from S3 at render time.

    Idempotent: only acquisitions without a baked thumbnail yet are rendered, so
    re-running bakes just what was added since. A scene that can't be rendered is
    skipped and retried next run. Needs the viz extra
    (``pip install "umbra-py[viz]"``); bootstrap the index first with 'umbra
    index fetch' or 'umbra index build'.

    Baking is the one derived artifact worth moving rather than recomputing (it
    costs an overview stream per scene), so 'umbra index fetch-thumbnails' pulls
    the published bake instead and 'umbra index export-thumbnails' writes yours
    out to share.
    """
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(
            f"No index at {path}. Create one first with 'umbra index fetch' or 'umbra index build'."
        )
    with OrbitSpinner("Baking thumbnails") as spinner:

        def tally(n: int) -> None:
            spinner.label = f"Baking thumbnails ({n} processed)"

        with CatalogIndex(path) as idx:
            baked = idx.bake_thumbnails(
                asset=asset,
                max_size=size,
                limit=limit,
                newest_first=newest_first,
                progress=tally,
            )
            s = idx.stats()
    click.echo(
        f"Baked {baked} new thumbnail(s); {s['thumbnailed']} of {s['items']} "
        f"acquisition(s) now have one. ({path})"
    )


@index.command("export-thumbnails")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index to export from (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/catalog.db).",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Sidecar file to write (default: catalog.thumbs.db beside the index).",
)
def index_export_thumbnails(db_path, out_path) -> None:
    """Write the index's baked thumbnails to a shareable sidecar database.

    A baked quicklook costs a cloud-optimized GeoTIFF overview streamed per
    scene, so it is the one derived artifact worth moving rather than
    recomputing. This writes the PNGs already baked ('umbra index
    bake-thumbnails') to a standalone catalog.thumbs.db that any other index can
    merge with 'umbra index fetch-thumbnails --from'.

    The sidecar is a separate file rather than a column of the published
    catalog.db on purpose: the pixels dwarf the metadata, so every 'umbra index
    fetch' would otherwise pay for previews most callers never open.
    """
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(f"No index at {path}. Build one with 'umbra index build'.")
    dest = Path(out_path) if out_path else default_thumbs_path(path)
    with CatalogIndex(path) as idx:
        with OrbitSpinner("Exporting baked thumbnails"):
            written = idx.export_thumbnails(dest)
    size_mb = dest.stat().st_size / 1e6
    click.echo(f"Exported {written} thumbnail(s) to {dest} ({size_mb:.1f} MB).")


@index.command("fetch-thumbnails")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index to merge the thumbnails into (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Must already exist.",
)
@click.option(
    "--from",
    "src_path",
    default=None,
    help="Merge a local sidecar file instead of downloading the published one.",
)
@click.option(
    "--url",
    default=None,
    help="Override the release asset URL (advanced -- e.g. to pull from a fork).",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace thumbnails already baked locally (default: keep them).",
)
def index_fetch_thumbnails(db_path, src_path, url, overwrite) -> None:
    """Download the published SAR thumbnails and merge them into the index.

    Every preview otherwise streams a scene's cloud-optimized GeoTIFF overview
    from S3 at render time. The weekly workflow bakes them once and publishes
    catalog.thumbs.db on the rolling catalog-index release; this fetches that
    sidecar and fills the index's thumbnail column, so 'umbra serve's
    GET /artifacts/thumbnail/{id}.png, the 'umbra demo' preview and a --local
    gallery all read local bytes with no range read at all.

    Bootstrap the index first with 'umbra index fetch' or 'umbra index build'.
    Acquisitions the sidecar doesn't cover are left alone, so re-run after an
    'umbra index update' to pick up newly published previews.
    """
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(
            f"No index at {path}. Create one first with 'umbra index fetch' or 'umbra index build'."
        )
    if src_path:
        source = Path(src_path)
    else:
        with OrbitSpinner("Fetching baked thumbnails") as spinner:

            def tally(done: int, total: int | None) -> None:
                if total:
                    spinner.label = f"Fetching baked thumbnails ({done / total:.0%})"
                else:
                    spinner.label = f"Fetching baked thumbnails ({done / 1e6:.0f} MB)"

            source = fetch_prebuilt_thumbnails(default_thumbs_path(path), url=url, progress=tally)
    with CatalogIndex(path) as idx:
        with OrbitSpinner("Merging thumbnails into the index"):
            applied = idx.import_thumbnails(source, overwrite=overwrite)
        s = idx.stats()
    click.echo(
        f"Merged {applied} thumbnail(s) from {source}; {s['thumbnailed']} of "
        f"{s['items']} acquisition(s) now have one. ({path})"
    )


@index.command("fetch")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Where to write the index (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Overwritten if it already exists.",
)
@click.option(
    "--url",
    default=None,
    help="Override the release asset URL (advanced -- e.g. to pull from a fork).",
)
def index_fetch(db_path, url) -> None:
    """Download the published prebuilt catalog index for instant local search.

    Umbra has no STAC API, so the first 'umbra index build' crawls the whole
    bucket (minutes). This instead fetches the weekly-rebuilt snapshot from the
    project's rolling catalog-index GitHub release, so 'umbra search --local'
    works immediately -- no crawl. Re-run any time to refresh.
    """
    path = _shared._index_path(db_path)

    with OrbitSpinner("Fetching prebuilt catalog index") as spinner:

        def tally(done: int, total: int | None) -> None:
            if total:
                spinner.label = f"Fetching prebuilt catalog index ({done / total:.0%})"
            else:
                spinner.label = f"Fetching prebuilt catalog index ({done / 1e6:.0f} MB)"

        with CatalogIndex.from_release(path, url=url, progress=tally) as idx:
            s = idx.stats()
    built = s["built_at"]
    built_note = f", built {built}" if built else ""
    click.echo(
        f"Fetched prebuilt index: {s['items']} acquisition(s){built_note}. ({path})\n"
        "Search it now with 'umbra search --local'."
    )


@index.command("export")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index database to export (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/catalog.db).",
)
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output stac-geoparquet file (e.g. umbra-open-data.parquet).",
)
def index_export(db_path, out_path) -> None:
    """Export a local index to stac-geoparquet for serverless catalog search.

    Writes every indexed acquisition as one row of a stac-geoparquet file —
    the whole catalog searchable in seconds with DuckDB, geopandas or pyarrow,
    no server and no crawl. Each row carries the full STAC item plus a 'self'
    link back to its sidecar JSON, so results lead straight to the data files.
    Build the index first with 'umbra index build'. Requires the export extra
    (``pip install "umbra-py[export]"``).
    """
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(f"No index at {path}. Build one with 'umbra index build'.")
    with CatalogIndex(path) as idx:
        total = len(idx)
        with OrbitSpinner(f"Exporting {total} item(s) to geoparquet"):
            written = export_geoparquet(idx.search(), out_path)
    skipped = total - written
    note = f" ({skipped} without a footprint skipped)" if skipped else ""
    click.echo(f"Exported {written} of {total} item(s) to {out_path}{note}.")


@index.command("info")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index database to inspect (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/catalog.db).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the index stats as a JSON object on stdout "
    "(see docs/schemas/index-info.schema.json) instead of the human summary.",
)
def index_info(db_path, as_json) -> None:
    """Show what a local index holds: item count, date span and task count.

    ``--json`` emits the stats as a machine-readable object
    (``docs/schemas/index-info.schema.json``): ``path``, ``size_bytes``,
    ``items``, ``start``, ``end``, ``tasks``, ``labeled``, ``thumbnailed`` and
    ``built_at``.
    """
    path = _shared._index_path(db_path)
    if not path.exists():
        raise click.ClickException(f"No index at {path}. Build one with 'umbra index build'.")
    with CatalogIndex(path) as idx:
        s = idx.stats()
    size_bytes = path.stat().st_size
    if as_json:
        click.echo(json.dumps({"path": str(path), "size_bytes": size_bytes, **s}, indent=2))
        return
    size_mb = size_bytes / 1e6
    click.echo(f"Index: {path}")
    click.echo(f"  items : {s['items']}")
    click.echo(f"  dates : {s['start'] or '?'} -> {s['end'] or '?'}")
    click.echo(f"  tasks : {s['tasks']}")
    click.echo(f"  places: {s['labeled']} of {s['items']} labelled")
    click.echo(f"  thumbs: {s['thumbnailed']} of {s['items']} baked")
    click.echo(f"  size  : {size_mb:.1f} MB")
    click.echo(f"  built : {_shared._built_note(s['built_at'])}")


@cli.group()
def semantic() -> None:
    """Semantic (embedding-based) task-name search -- the model-backed layer of
    natural-language search.

    The deterministic matchers ('umbra search --area' and '--fuzzy') match by the
    words in a task label. Some queries share no word with the label they mean --
    "grain storage north dakota" means "Beet Piler - ND" -- and only a model that
    has read about the world can bridge that. This embeds the task names once
    ('umbra semantic build') so 'umbra semantic search' can rank them by meaning.

    Requires the ``ai`` extra (``pip install 'umbra-py[ai]'``) and an embedding
    API key: set OPENAI_API_KEY (optionally OPENAI_BASE_URL for a compatible
    endpoint, UMBRA_EMBED_MODEL to pick the model). Embeddings only rank task
    names; the resolved search still runs deterministically.
    """


def _semantic_path(sem_db: str | None, db_path: str | None):
    """Resolve the semantic vector DB path: an explicit ``--semantic-db``, else
    the sibling of the (explicit or default) catalog index."""
    from ..semantic import default_semantic_path

    if sem_db:
        return Path(sem_db)
    return default_semantic_path(db_path)


@semantic.command("build")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Catalog index to read task names from (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Build or fetch it first with 'umbra index "
    "build' / 'umbra index fetch'.",
)
@click.option(
    "--semantic-db",
    "sem_db",
    default=None,
    help="Where to write the embedding index (default: the catalog index's "
    "sibling, e.g. catalog.semantic.db).",
)
@click.option(
    "--model",
    default=None,
    help="Embedding model (default: $UMBRA_EMBED_MODEL, else "
    "text-embedding-3-small). The provider is an OpenAI-compatible /embeddings "
    "endpoint chosen by OPENAI_API_KEY / OPENAI_BASE_URL.",
)
def semantic_build(db_path, sem_db, model) -> None:
    """Embed the index's task names into a semantic search index.

    Reads the distinct task/site names from the catalog index and stores an
    embedding vector for each (idempotent -- a rebuild only embeds names not seen
    before). One embedding call per batch of names; nothing else touches a model.
    """
    from .. import semantic as sem
    from ..exceptions import MissingDependencyError

    path = _semantic_path(sem_db, db_path)
    try:
        model_name = sem.resolve_embed_model(model)
        embedder = sem.default_embedder(model=model_name)
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        with OrbitSpinner("Embedding task names") as spinner:

            def tally(done: int, total: int) -> None:
                spinner.label = f"Embedding task names ({done}/{total})"

            with sem.SemanticTaskIndex(path) as index:
                written = index.build(
                    embedder=embedder,
                    index_path=db_path,
                    model=model_name,
                    progress=tally,
                )
                total = len(index)
    except (sem.SemanticError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Embedded {written} new task name(s); semantic index now holds {total} "
        f"({model_name}). ({path})"
    )


@semantic.command("search")
@click.argument("query")
@click.option(
    "--semantic-db",
    "sem_db",
    default=None,
    help="Embedding index to query (default: the catalog index's sibling, e.g. "
    "catalog.semantic.db). Build it first with 'umbra semantic build'.",
)
@click.option(
    "--top-k", type=int, default=5, show_default=True, help="How many ranked matches to show."
)
@click.option(
    "--min-score",
    type=float,
    default=0.0,
    show_default=True,
    help="Drop matches below this cosine score (0=unrelated, 1=identical).",
)
@click.option(
    "--model",
    default=None,
    help="Embedding model for the query -- must match the model the index was "
    "built with (default: $UMBRA_EMBED_MODEL, else text-embedding-3-small).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the ranked matches as JSON (see docs/schemas/task-matches.schema.json).",
)
@click.option(
    "--run",
    "-r",
    is_flag=True,
    help="Run 'umbra search --area <best match>' for the top result. The command "
    "is always shown first, so you see exactly what will run.",
)
@click.option("--limit", type=int, default=None, help="Cap results when --run executes the search.")
@click.option(
    "--local",
    is_flag=True,
    help="With --run, search a prebuilt local index instead of walking S3 live.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Catalog index for --run --local (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Implies --local.",
)
def semantic_search(query, sem_db, top_k, min_score, model, as_json, run, limit, local, db_path):
    """Rank Umbra task/site names by how well they match a plain-language QUERY.

    Embeds the query and scores it against the stored task embeddings, printing
    the closest names -- the semantic answer to a site you can describe but can't
    name. Prints the exact 'umbra search --area ...' command for the best match;
    pass --run to execute it (you audit the command first, as with 'umbra ask').
    """
    import shlex

    from .. import semantic as sem
    from ..exceptions import MissingDependencyError

    path = _semantic_path(sem_db, db_path)
    if not path.exists():
        raise click.ClickException(
            f"No semantic index at {path}. Build one first with 'umbra semantic build'."
        )
    try:
        embedder = sem.default_embedder(model=model)
        with sem.SemanticTaskIndex(path) as index:
            matches = index.matching_tasks(query, embedder, top_k=top_k, min_score=min_score)
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (sem.SemanticError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        payload = {
            "query": query,
            "matches": [{"task": m.task, "score": round(m.score, 6)} for m in matches],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        if not matches:
            click.echo("No task names cleared the score threshold.")
        for m in matches:
            click.echo(f"  {m.score:.3f}  {m.task}")

    if not matches:
        return

    best = matches[0].task
    command = f"umbra search --area {shlex.quote(best)}"
    if not as_json:
        click.echo(f"\nBest match: {best}")
        click.echo(command)

    if not run:
        if not as_json:
            click.echo("\nRe-run with --run to search the best match.")
        return

    if not as_json:
        click.echo("")
    found = _shared._gather_items(local=local, db_path=db_path, area=best, limit=limit)
    for item in found:
        if as_json:
            click.echo(json.dumps(item.raw))
        else:
            click.echo(item.summary())
            if item.href:
                click.echo(f"  url      : {item.href}")
            click.echo("")
    if not as_json:
        click.echo(f"{len(found)} item(s).")


@semantic.command("info")
@click.option(
    "--semantic-db",
    "sem_db",
    default=None,
    help="Embedding index to inspect (default: the catalog index's sibling).",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Catalog index whose sibling semantic index to inspect (default path).",
)
def semantic_info(sem_db, db_path) -> None:
    """Show what a semantic index holds: task-vector count, model and dimension."""
    from .. import semantic as sem

    path = _semantic_path(sem_db, db_path)
    if not path.exists():
        raise click.ClickException(
            f"No semantic index at {path}. Build one with 'umbra semantic build'."
        )
    with sem.SemanticTaskIndex(path) as index:
        s = index.stats()
    size_mb = path.stat().st_size / 1e6
    click.echo(f"Semantic index: {path}")
    click.echo(f"  tasks : {s['tasks']}")
    click.echo(f"  model : {s['model'] or '?'}")
    click.echo(f"  dim   : {s['dim'] or '?'}")
    click.echo(f"  size  : {size_mb:.1f} MB")


@cli.group()
def embed() -> None:
    """Visual similarity search over the archive (embedding-based, C5).

    Every other search matches metadata -- a date, a bbox, a task name. This
    matches *appearance*: it embeds each acquisition's rendered quicklook into a
    vector once ('umbra embed build'), then ranks scenes by cosine similarity, so
    'umbra embed similar <url>' finds acquisitions that *look like* a given one and
    'umbra embed search "a flooded field"' finds them from a text description (with
    a joint CLIP-family model).

    Requires the ``ai`` extra for the model call and ``viz`` to render the
    quicklooks (``pip install 'umbra-py[ai,viz]'``) plus a multimodal embedding API
    key: set OPENAI_API_KEY (optionally OPENAI_BASE_URL for a CLIP-family
    /embeddings endpoint, UMBRA_SCENE_EMBED_MODEL to pick the model). The ranking
    is deterministic; only turning an image or query into a vector calls a model.
    """


def _embed_path(embed_db: str | None, db_path: str | None):
    """Resolve the scene-embedding DB path: an explicit ``--embed-db``, else the
    sibling of the (explicit or default) catalog index."""
    from ..embed import default_scene_embed_path

    if embed_db:
        return Path(embed_db)
    return default_scene_embed_path(db_path)


@embed.command("build")
@click.argument("item_urls", nargs=-1)
@click.option("--area", default=None, help="Search mode: name of an Umbra site to embed.")
@click.option("--bbox", help="Search mode: footprint filter 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Search mode: geocode a place name to a bounding box (mutually exclusive with --bbox).",
)
@_shared._geometry_option
@click.option("--start", help="Search mode: earliest acquisition date (YYYY-MM-DD or relative).")
@click.option("--end", help="Search mode: latest acquisition date (same formats as --start).")
@click.option(
    "--limit",
    type=int,
    default=200,
    show_default=True,
    help="Search mode: cap how many acquisitions to embed.",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product's quicklook to embed. GEC (the geocoded GeoTIFF) is the "
    "sensible default; the complex SICD/CPHD products aren't amplitude rasters.",
)
@click.option(
    "--model",
    default=None,
    help="Embedding model label (default: $UMBRA_SCENE_EMBED_MODEL, else clip). "
    "The provider is an OpenAI-compatible multimodal /embeddings endpoint chosen "
    "by OPENAI_API_KEY / OPENAI_BASE_URL.",
)
@click.option(
    "--embed-db",
    default=None,
    help="Where to write the scene index (default: the catalog index's sibling, "
    "e.g. catalog.embed.db).",
)
@_shared._local_index_options
@_shared._fuzzy_option
def embed_build(
    item_urls,
    area,
    bbox,
    place,
    intersects,
    start,
    end,
    limit,
    asset,
    model,
    embed_db,
    local,
    db_path,
    fuzzy,
) -> None:
    """Render and embed acquisition quicklooks into a scene-similarity index.

    Two ways to choose what to embed:

    \b
    - Pass STAC JSON URLs directly.
    - Or search: give --area (or --bbox / --place) with --start/--end and the
      command gathers the acquisitions automatically.

    Each item's quicklook is rendered once (only downsampled overviews stream over
    HTTP -- no full download) and embedded; an item already in the index is skipped
    so a rebuild only embeds what is new. Requires the ``ai`` + ``viz`` extras and
    an embedding API key.
    """
    from .. import embed as emb
    from ..exceptions import MissingDependencyError

    search_mode = any(v for v in (area, bbox, place, intersects, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria "
            "(--area/--bbox/--place/--intersects/--start/--end), not both."
        )

    if item_urls:
        items = [_shared._item_from_url(url) for url in item_urls]
    else:
        if not (area or bbox or place or intersects):
            raise click.UsageError(
                "Give --area, --bbox, --place or --intersects (optionally with "
                "--start/--end) to search, or pass item URLs directly."
            )
        search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
        items = _shared._gather_items(
            local=local,
            db_path=db_path,
            bbox=search_bbox,
            intersects=search_geometry,
            start=start,
            end=end,
            area=area,
            fuzzy=fuzzy,
            product_types=[asset],
            limit=limit,
        )
    if not items:
        raise click.ClickException("No acquisitions to embed; widen the search or pass URLs.")

    path = _embed_path(embed_db, db_path)
    try:
        model_name = emb.resolve_scene_model(model)
        embedder = emb.default_image_embedder(model=model_name)
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc

    render = lambda it: emb._render_quicklook_asset(it, asset=asset)  # noqa: E731
    skipped: list[str] = []

    def note_error(item: UmbraItem, exc: Exception) -> None:
        skipped.append(f"{item.id}: {exc}")

    try:
        with OrbitSpinner(f"Embedding {len(items)} quicklook(s)") as spinner:

            def tally(done: int, total: int) -> None:
                spinner.label = f"Embedding quicklooks ({done}/{total})"

            with emb.SceneEmbeddingIndex(path) as index:
                written = index.build(
                    items,
                    embedder=embedder,
                    render=render,
                    model=model_name,
                    progress=tally,
                    on_error=note_error,
                )
                total = len(index)
    except (emb.EmbedError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc

    for note in skipped:
        click.echo(f"warning: skipped {note}", err=True)
    click.echo(
        f"Embedded {written} new scene(s); scene index now holds {total} ({model_name}). ({path})"
    )


def _print_scene_matches(matches, as_json: bool, query_label: str) -> None:
    if as_json:
        click.echo(
            json.dumps({"query": query_label, "matches": [m.to_dict() for m in matches]}, indent=2)
        )
        return
    if not matches:
        click.echo("No scenes cleared the score threshold.")
        return
    for m in matches:
        when = m.datetime or "?"
        where = m.task or "?"
        click.echo(f"  {m.score:.3f}  {m.item_id}  [{where} @ {when}]")
        if m.href:
            click.echo(f"           {m.href}")


@embed.command("similar")
@click.argument("item_url")
@click.option("--embed-db", default=None, help="Scene index to query (default: the sibling).")
@click.option("--top-k", type=int, default=10, show_default=True, help="How many matches to show.")
@click.option(
    "--min-score",
    type=float,
    default=0.0,
    show_default=True,
    help="Drop matches below this cosine score (0=unrelated, 1=identical).",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product's quicklook of the query item to embed (match how the index was built).",
)
@click.option(
    "--model",
    default=None,
    help="Embedding model for the query -- must match the model the index was "
    "built with (default: $UMBRA_SCENE_EMBED_MODEL, else clip).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the ranked matches as JSON (see docs/schemas/scene-matches.schema.json).",
)
def embed_similar(item_url, embed_db, top_k, min_score, asset, model, as_json) -> None:
    """Find archived scenes that look like the acquisition at ITEM_URL.

    Renders and embeds the query item's quicklook, then ranks the stored scene
    vectors by cosine similarity (the query item is excluded from its own results).
    "Find scenes that look like this flooded field" -- a search over pixels, not
    metadata. Build the index first with 'umbra embed build'.
    """
    from .. import embed as emb
    from ..exceptions import MissingDependencyError

    path = _embed_path(embed_db, None)
    if not path.exists():
        raise click.ClickException(
            f"No scene index at {path}. Build one first with 'umbra embed build'."
        )
    item = _shared._item_from_url(item_url)
    try:
        embedder = emb.default_image_embedder(model=model)
        render = lambda it: emb._render_quicklook_asset(it, asset=asset)  # noqa: E731
        with OrbitSpinner(f"Embedding {item.id}"):
            with emb.SceneEmbeddingIndex(path) as index:
                matches = index.similar_to_item(
                    item, embedder=embedder, render=render, top_k=top_k, min_score=min_score
                )
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (emb.EmbedError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc

    _print_scene_matches(matches, as_json, item.id)


@embed.command("search")
@click.argument("query")
@click.option("--embed-db", default=None, help="Scene index to query (default: the sibling).")
@click.option("--top-k", type=int, default=10, show_default=True, help="How many matches to show.")
@click.option(
    "--min-score",
    type=float,
    default=0.0,
    show_default=True,
    help="Drop matches below this cosine score (0=unrelated, 1=identical).",
)
@click.option(
    "--model",
    default=None,
    help="Joint (CLIP-family) model to embed the text query -- must share a vector "
    "space with the model the index was built with (default: $UMBRA_SCENE_EMBED_MODEL, "
    "else clip).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the ranked matches as JSON (see docs/schemas/scene-matches.schema.json).",
)
def embed_search(query, embed_db, top_k, min_score, model, as_json) -> None:
    """Find archived scenes matching a plain-language QUERY ("ships at a berth").

    Embeds the text query and ranks the stored *image* vectors by cosine
    similarity. This needs a joint CLIP-family model whose text and image encoders
    share a space -- build the index and run this query with the same model.
    """
    from .. import embed as emb
    from ..exceptions import MissingDependencyError

    path = _embed_path(embed_db, None)
    if not path.exists():
        raise click.ClickException(
            f"No scene index at {path}. Build one first with 'umbra embed build'."
        )
    try:
        text_embedder = emb.default_text_embedder(model=model)
        with emb.SceneEmbeddingIndex(path) as index:
            matches = index.similar_to_text(query, text_embedder, top_k=top_k, min_score=min_score)
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc
    except (emb.EmbedError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc

    _print_scene_matches(matches, as_json, query)


@embed.command("info")
@click.option("--embed-db", default=None, help="Scene index to inspect (default: the sibling).")
def embed_info(embed_db) -> None:
    """Show what a scene index holds: scene-vector count, model and dimension."""
    from .. import embed as emb

    path = _embed_path(embed_db, None)
    if not path.exists():
        raise click.ClickException(f"No scene index at {path}. Build one with 'umbra embed build'.")
    with emb.SceneEmbeddingIndex(path) as index:
        s = index.stats()
    size_mb = path.stat().st_size / 1e6
    click.echo(f"Scene index: {path}")
    click.echo(f"  scenes : {s['scenes']}")
    click.echo(f"  model  : {s['model'] or '?'}")
    click.echo(f"  dim    : {s['dim'] or '?'}")
    click.echo(f"  size   : {size_mb:.1f} MB")


@embed.command("fetch")
@click.option(
    "--embed-db",
    default=None,
    help="Where to write the scene index (default: the catalog index's sibling, "
    "e.g. catalog.embed.db). Overwritten if it already exists.",
)
@click.option(
    "--url",
    default=None,
    help="Override the release asset URL (advanced -- e.g. to pull from a fork).",
)
def embed_fetch(embed_db, url) -> None:
    """Download the published scene-embedding index for instant similarity search.

    Building the index embeds every quicklook in the archive -- the one expensive,
    model-backed step. This instead fetches the prebuilt 'catalog.embed.db' from
    the project's rolling catalog-index GitHub release, so 'umbra embed similar' /
    'umbra embed search' work with no rebuild (only the query still needs an
    embedding key). Re-run any time to refresh. Note the published vectors are
    model-specific: query with the model the index reports ('umbra embed info').
    """
    from .. import embed as emb

    path = _embed_path(embed_db, None)

    with OrbitSpinner("Fetching prebuilt scene index") as spinner:

        def tally(done: int, total: int | None) -> None:
            if total:
                spinner.label = f"Fetching prebuilt scene index ({done / total:.0%})"
            else:
                spinner.label = f"Fetching prebuilt scene index ({done / 1e6:.0f} MB)"

        with emb.SceneEmbeddingIndex.from_release(path, url=url, progress=tally) as index:
            s = index.stats()
    click.echo(
        f"Fetched scene index: {s['scenes']} scene vector(s), model {s['model'] or '?'}. "
        f"({path})\nQuery it now with 'umbra embed similar <url>' or "
        "'umbra embed search \"…\"'."
    )
