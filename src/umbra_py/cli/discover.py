"""Finding acquisitions: ``search``, ``watch``, ``info``, ``ask``, and context.

The verbs that answer *which* acquisitions exist, in metadata rather than
pixels -- including the natural-language front door (``ask``), whose model
output is re-validated by :mod:`umbra_py.planner` before it becomes a filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import click

from .._spinner import OrbitSpinner
from ..catalog import UmbraCatalog
from ..constants import CANOPY_TOKEN_ENV, DATA_LICENSE, PRODUCT_ASSETS
from ..context import llm_context
from ..exceptions import UmbraError
from ..index import (
    CatalogIndex,
)
from . import _shared
from ._root import cli


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding box "
    "and search within it, via OpenStreetMap Nominatim. Mutually exclusive "
    "with --bbox; the match is rectangular, so it can include nearby areas "
    "outside the named place.",
)
@_shared._geometry_option
@click.option(
    "--start",
    help="Earliest acquisition date. Accepts YYYY-MM-DD, a year or month "
    "(2024, 2024-03), or a relative expression ('today', 'yesterday', "
    "'3 months ago', 'last month').",
)
@click.option(
    "--end",
    help="Latest acquisition date (same formats as --start; a bare year, month "
    "or period like 'last month' snaps to that span's last day).",
)
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Keep items exposing this asset (repeatable).",
)
@click.option(
    "--area",
    default=None,
    help="Case-insensitive name of an Umbra task/site to search (e.g. "
    "'Centerfield'). Umbra files every pass of a site under one named "
    "directory, so this returns just that area's acquisitions -- and skips "
    "listing the rest, so it's much faster. The easy way to gather the "
    "co-located passes that 'umbra change' needs.",
)
@click.option(
    "--fuzzy",
    is_flag=True,
    help="Match --area loosely: word-order- and punctuation-independent, and "
    "tolerant of a small typo (so 'utah centerfield' or 'centrfield' still "
    "reach 'Centerfield, Utah'). Deterministic, no model call; a strict "
    "superset of the default substring match, so it never drops a result.",
)
@_shared._acquisition_filter_options
@click.option("--limit", type=int, default=20, show_default=True, help="Max results.")
@click.option(
    "--max-per-task",
    type=int,
    default=None,
    help="Cap items per Umbra task directory. Each task is repeated imaging "
    "of the same area, so '--max-per-task 1' returns one item per distinct "
    "site rather than every revisit.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit full STAC item JSON.")
@click.option(
    "--local",
    is_flag=True,
    help="Search a local SQLite index built with 'umbra index build' instead "
    "of walking S3 live -- near-instant for repeat searches. Only returns "
    "acquisitions already present in the index.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Path to the local index database (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Implies --local.",
)
@click.option(
    "--live",
    "read_through",
    is_flag=True,
    help="With --local, read through to the live bucket: answer from the index "
    "AND walk only acquisitions newer than the index's freshest pass, merging "
    "the two -- so a repeat search stays near-instant but also catches anything "
    "published since the index was built (which it caches for next time).",
)
@click.option(
    "--token",
    default=None,
    envvar=CANOPY_TOKEN_ENV,
    help="Canopy API token. When given, search Umbra's authenticated COMMERCIAL "
    "archive (a real STAC API) instead of the open bucket -- the same filters, "
    "the same results, over the paid catalog. Falls back to the "
    f"${CANOPY_TOKEN_ENV} environment variable. Mutually exclusive with --local.",
)
def search(
    bbox,
    place,
    intersects,
    start,
    end,
    products,
    area,
    fuzzy,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    limit,
    max_per_task,
    as_json,
    local,
    db_path,
    read_through,
    token,
):
    """Search the catalog by area, date and product type.

    Searches Umbra's open data by default. Pass --token (or set
    $UMBRA_CANOPY_TOKEN) to search Umbra's commercial Canopy archive instead --
    same query, same output, over the paid catalog.
    """
    if token and (local or db_path is not None):
        raise click.ClickException(
            "--token searches the live Canopy archive and cannot be combined "
            "with --local / --db (which read a local open-data index)."
        )
    if read_through and not (local or db_path is not None):
        raise click.ClickException(
            "--live reads through a local index to the bucket; it only applies "
            "with --local / --db. (A plain search already walks S3 live.)"
        )
    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    source, index = _shared._search_source(local, db_path, token)
    search_kwargs = dict(
        bbox=search_bbox,
        intersects=search_geometry,
        start=start,
        end=end,
        product_types=list(products) or None,
        area=area,
        fuzzy=fuzzy,
        **_shared._acquisition_filter_kwargs(
            polarizations, min_incidence, max_incidence, max_resolution
        ),
        limit=limit,
        max_per_task=max_per_task,
    )
    try:
        if index and read_through:
            results = source.search_live(**search_kwargs)
        else:
            results = source.search(**search_kwargs)
        found = 0
        if index and read_through:
            spinner_label = "Searching local index + live delta"
        elif index:
            spinner_label = "Searching local index"
        elif token:
            spinner_label = "Searching Canopy archive"
        else:
            spinner_label = "Searching Umbra archive"
        spinner = OrbitSpinner(spinner_label)
        spinner.__enter__()
        try:
            for item in results:
                # Stop the spinner the moment we have something to print so the
                # streaming output isn't fighting the animation's cursor moves.
                spinner.stop()
                found += 1
                if as_json:
                    click.echo(json.dumps(item.raw))
                else:
                    click.echo(item.summary())
                    if item.href:
                        click.echo(f"  url      : {item.href}")
                    click.echo("")
        finally:
            spinner.stop()
        if not as_json:
            click.echo(f"{found} item(s).")
    finally:
        if isinstance(source, CatalogIndex):
            source.close()


def _print_watch_result(result) -> None:
    """Human-readable rendering of a :class:`~umbra_py.watch.WatchResult`."""
    if result.new_count == 0:
        click.echo(f"No new acquisitions since last run for watch '{result.name}'.")
        click.echo(f"Tracking {result.total_seen} acquisition(s) total.")
        return
    if result.first_run:
        click.echo(
            f"First run for watch '{result.name}': "
            f"{result.new_count} acquisition(s) now tracked (baseline)."
        )
    else:
        click.echo(
            f"{result.new_count} new acquisition(s) since last run for watch '{result.name}':"
        )
    click.echo("")
    for item in result.new_items:
        click.echo(item.summary())
        if item.href:
            click.echo(f"  url      : {item.href}")
        click.echo("")
    click.echo(f"Tracking {result.total_seen} acquisition(s) total.")


@cli.command("watch")
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name to a bounding box to watch (mutually exclusive with --bbox).",
)
@_shared._geometry_option
@click.option(
    "--start",
    help="Earliest acquisition date (YYYY-MM-DD, a year/month, or a relative "
    "expression like '3 months ago'). Same formats as 'umbra search'.",
)
@click.option("--end", help="Latest acquisition date (same formats as --start).")
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Watch only acquisitions exposing this asset (repeatable).",
)
@click.option(
    "--area",
    default=None,
    help="Name of an Umbra task/site to watch (e.g. 'Centerfield'). The usual "
    "way to monitor one site -- it lists just that task, so a scheduled check is "
    "fast.",
)
@_shared._fuzzy_option
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap acquisitions inspected per run (default: no cap -- watch everything in scope).",
)
@click.option(
    "--name",
    default=None,
    help="Stable identifier for this watch's state. Defaults to a slug derived "
    "from the query, so repeat runs of the same search line up automatically; "
    "set it explicitly to run several distinct watches over overlapping areas.",
)
@click.option(
    "--state-db",
    "state_db",
    default=None,
    help="SQLite database that stores this watch's memory of already-reported "
    "acquisitions (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/catalog.db). "
    "Reuses the catalog index's metadata table; the acquisition rows are untouched.",
)
@click.option(
    "--local",
    is_flag=True,
    help="Search a prebuilt local index instead of walking S3 live -- e.g. to "
    "diff two index snapshots. The default (live) is usually what you want, "
    "since monitoring is about newly published acquisitions.",
)
@click.option(
    "--index-db",
    "index_db",
    default=None,
    help="Path to the local index to search when --local is set (default: the "
    "same catalog.db as --state-db). Implies --local.",
)
@click.option(
    "--reset",
    is_flag=True,
    help="Forget this watch's prior state and re-establish a baseline -- every "
    "acquisition found this run is reported as new.",
)
@click.option(
    "--exit-code",
    "use_exit_code",
    is_flag=True,
    help="Exit 10 when there are new acquisitions and 0 when there are none, so a "
    "scheduler's shell 'if' can branch without parsing output.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the delta as JSON (see docs/schemas/watch-delta.schema.json).",
)
def watch_cmd(
    bbox,
    place,
    intersects,
    start,
    end,
    products,
    area,
    fuzzy,
    limit,
    name,
    state_db,
    local,
    index_db,
    reset,
    use_exit_code,
    as_json,
) -> None:
    """Report only acquisitions new since the last run -- standing site monitoring.

    SAR re-images a site pass after pass, so the natural way to monitor one is to
    run the same search on a schedule and act only on what's new. This command is
    that primitive: it searches, compares against what previous runs already
    reported (state kept in a local SQLite database), prints only the new
    acquisitions, and remembers them. It is idempotent -- an immediate re-run with
    no newly published data reports nothing -- so cron, a GitHub Action, or an
    agent loop can supply the schedule and this supplies the delta.

    Pair it with 'umbra change --narrate' or 'umbra describe' for a standing
    analyst: new pass lands -> composite against the previous pass -> narration.
    """
    from ..watch import MetaWatchStore, SearchSource, watch, watch_key

    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    product_types = list(products) or None
    watch_name = name or watch_key(
        area=area,
        place=place,
        bbox=search_bbox,
        intersects=search_geometry,
        product_types=product_types,
        start=start,
        end=end,
        fuzzy=fuzzy,
    )

    state_path = _shared._index_path(state_db)
    source, is_index = _shared._search_source(local, index_db)
    reuse = isinstance(source, CatalogIndex) and Path(getattr(source, "path", "")) == state_path
    store_index = source if reuse and isinstance(source, CatalogIndex) else CatalogIndex(state_path)
    store = MetaWatchStore(store_index)

    try:
        label = "Checking local index" if is_index else "Checking Umbra archive"
        with OrbitSpinner(f"{label} for new acquisitions"):
            # Both backends satisfy SearchSource at runtime (runtime_checkable);
            # the cast bridges mypy's stricter view of the loose protocol.
            result = watch(
                cast(SearchSource, source),
                name=watch_name,
                store=store,
                reset=reset,
                bbox=search_bbox,
                intersects=search_geometry,
                start=start,
                end=end,
                product_types=product_types,
                area=area,
                fuzzy=fuzzy,
                limit=limit,
            )
    finally:
        if isinstance(source, CatalogIndex):
            source.close()
        if not reuse:
            store_index.close()

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
    else:
        _print_watch_result(result)
        click.echo(f"State: {state_path}")

    if use_exit_code and result.new_count > 0:
        raise SystemExit(10)


@cli.command()
@click.argument("item")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the item's LLM context card as JSON instead of a readable summary "
    "(see docs/schemas/item-context.schema.json).",
)
@click.option(
    "--token",
    default=None,
    envvar=CANOPY_TOKEN_ENV,
    help="Canopy API token. When given, ITEM is treated as an acquisition id and "
    "looked up in Umbra's authenticated COMMERCIAL archive by a keyed STAC search "
    "(the retrieval complement to 'umbra search --token'), instead of being read "
    f"as an open-data sidecar URL. Falls back to ${CANOPY_TOKEN_ENV}.",
)
def info(item, as_json, token) -> None:
    """Show a summary of a STAC item.

    Without ``--token`` (the default), ITEM is the JSON URL of an open-data
    sidecar, read directly. With ``--token`` (or ``$UMBRA_CANOPY_TOKEN``), ITEM
    is instead an acquisition id, looked up in Umbra's Canopy commercial archive
    by a keyed STAC search — the retrieval complement to ``umbra search
    --token``, over the paid catalog.

    ``--json`` emits the explanation-rich context card
    (:meth:`umbra_py.UmbraItem.to_llm_context`) — a compact object an agent
    can consume directly, with per-product explanations and the license line.
    """
    if token:
        found = UmbraCatalog(token=token).get_item(item)
        if found is None:
            raise click.ClickException(f"No item {item!r} in the Canopy commercial archive.")
        item_obj = found
    else:
        item_obj = _shared._item_from_url(item)
    if as_json:
        click.echo(json.dumps(item_obj.to_llm_context(), indent=2))
        return
    click.echo(item_obj.summary())
    click.echo(f"\nData license: {DATA_LICENSE} (attribution required).")


@cli.command()
def context() -> None:
    """Print the library's LLM context document as JSON.

    The product-type table, search-parameter semantics, and license rules an
    agent needs to drive umbra-py — see :func:`umbra_py.llm_context`. Pipe it
    into a model's context at the start of a session.
    """
    click.echo(json.dumps(llm_context(), indent=2))


@cli.command(name="llms-txt")
@click.option(
    "--full",
    is_flag=True,
    help="Emit the expanded llms-full.txt bundle (domain knowledge, the full CLI "
    "reference, the AI-native interfaces and a per-module map) instead of the "
    "concise llms.txt index.",
)
def llms_txt_cmd(full: bool) -> None:
    """Print the project's llms.txt context bundle to stdout.

    The `llms.txt convention <https://llmstxt.org/>`_ document — a Markdown guide
    a language model pulls in to learn how to *drive* umbra-py (the counterpart
    to the machine-readable ``umbra context`` JSON). ``--full`` emits the
    self-contained ``llms-full.txt``. The committed repo-root ``llms.txt`` /
    ``llms-full.txt`` are regenerated from this command::

        umbra llms-txt > llms.txt
        umbra llms-txt --full > llms-full.txt
    """
    from ..llms_txt import llms_full_txt, llms_txt

    click.echo(llms_full_txt() if full else llms_txt())


@cli.command()
@click.argument("question")
@click.option(
    "--run",
    "-r",
    is_flag=True,
    help="Execute the planned search instead of only printing it. The command "
    "is always shown first, so you see exactly what will run.",
)
@click.option(
    "--model",
    default=None,
    help="Override the planning model (default: $UMBRA_ASK_MODEL, else the "
    "provider default). The provider is chosen by which API key is set — "
    "ANTHROPIC_API_KEY or OPENAI_API_KEY (with optional OPENAI_BASE_URL).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap results, overriding whatever limit the model chose (only affects --run).",
)
@click.option(
    "--aoi",
    "aois",
    multiple=True,
    help="Offer the planner an area of interest you already have, as "
    "'[NAME=]PATH' — a .geojson file (or inline GeoJSON); repeat for several. "
    "The model may only *select* one by name; it can never write coordinates, "
    "so the polygon searched is always your file. Without a name, the file stem "
    "is used. A selected area becomes 'umbra search --intersects PATH'.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the resolved plan as JSON (see docs/schemas/search-plan.schema.json).",
)
@click.option(
    "--local",
    is_flag=True,
    help="Run the planned search against a prebuilt local index instead of a "
    "live S3 walk (see 'umbra index fetch'). Only affects --run.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index database for --run --local (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Implies --local.",
)
def ask(question, run, model, limit, aois, as_json, local, db_path) -> None:
    """Plan a catalog search from a plain-language question with a model.

    A configured model reads your sentence plus the library's domain context
    and returns the *search parameters* it maps to; the library then re-validates
    every one of them deterministically (dates, product types, bounding box) and
    prints the exact 'umbra search' command it resolves to. The LLM plans, the
    library executes, and you audit the command before it runs — nothing the
    model says becomes a filter without passing the deterministic layer.

    Pass --aoi to let it plan a *polygon* search too: the areas you name are
    listed in the prompt and the model may only pick one of them by name, so the
    shape searched is always your own file — it has no way to write coordinates.

    By default it only prints the plan; pass --run to execute it. Requires the
    ``ai`` extra (``pip install 'umbra-py[ai]'``) and a model API key: set
    ANTHROPIC_API_KEY, or OPENAI_API_KEY (optionally with OPENAI_BASE_URL for a
    compatible endpoint). Example::

        umbra ask "what did Umbra image at Centerfield, Utah last spring?"
        umbra ask "scenes over the delta since March" --aoi delta.geojson
    """
    from ..planner import AskError
    from ..planner import ask as plan_search

    areas = _shared._resolve_aois(aois)
    try:
        plan = plan_search(question, model=model, aois=areas)
    except (AskError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc

    if limit is not None:
        plan.limit = limit

    if as_json:
        click.echo(json.dumps(plan.to_dict(), indent=2))
    else:
        if plan.rationale:
            click.echo(f"Plan: {plan.rationale}")
        click.echo(plan.to_command())

    if not run:
        if not as_json:
            click.echo("\nRe-run with --run to execute this search.")
        return

    search_bbox = _shared._resolve_search_bbox(None, plan.place) if plan.place else plan.bbox
    if not as_json:
        click.echo("")
    found = _shared._gather_items(
        local=local,
        db_path=db_path,
        bbox=search_bbox,
        **plan.to_search_kwargs(),
    )
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
