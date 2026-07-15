"""Self-describing context for language-model consumers.

*Agents are the new first-time users.* The friction in using Umbra's open data
is interpretive, not computational — knowing *what to ask for* (which product
type, why two polarizations can't be compared, what a dB stretch is) is the
hard part, and it's exactly what a language model answers well *if* it has the
domain facts in context.

This module packages those facts — the product-type table, the search
parameter semantics, and the license/attribution rules — as one plain,
JSON-serialisable dict an agent (or an MCP tool, or a `--help-json` caller) can
pull into its context at runtime. It is deterministic and stdlib-only: it
describes the library, it never calls a model. Keep it in sync with the CLI and
:mod:`umbra_py.constants` when the surface changes; treat it as a product
surface, reviewed like code.
"""

from __future__ import annotations

from typing import Any

from .constants import (
    ATTRIBUTION,
    DATA_LICENSE,
    POLARIZATION_CAVEAT,
    PRODUCT_ASSETS,
    PRODUCT_TYPE_EXPLANATIONS,
)

#: Semantics of the search filters, mirroring the ``umbra search`` CLI options
#: and :meth:`umbra_py.UmbraCatalog.search` / :meth:`umbra_py.CatalogIndex.search`
#: keyword arguments. An agent reads this to build a valid query in one shot.
_SEARCH_PARAMETERS: dict[str, str] = {
    "bbox": (
        "Footprint filter as (min_lon, min_lat, max_lon, max_lat) in WGS84 "
        "degrees. Items whose footprint overlaps the box are returned."
    ),
    "place": (
        "Free-text place name (e.g. 'Port of Long Beach'). Geocoded to a bbox "
        "via Nominatim; resolve it yourself with geocode_place if you want to "
        "inspect the box first. Mutually informative with bbox, not required."
    ),
    "area": (
        "Substring match against the Umbra task (AOI campaign) name, e.g. "
        "'Centerfield, Utah'. Tasks group every pass of one site over time."
    ),
    "start": "Earliest acquisition date, inclusive, as YYYY-MM-DD.",
    "end": "Latest acquisition date, inclusive, as YYYY-MM-DD.",
    "products": (
        "Restrict to items exposing these product types (any of "
        f"{', '.join(PRODUCT_ASSETS)}). Omit to accept all."
    ),
    "limit": "Maximum number of items to return.",
    "max_per_task": (
        "Cap items per task. Use 1 for a 'one pin per site' world view; omit "
        "for the full time series of each site."
    ),
}


def llm_context() -> dict[str, Any]:
    """Return the domain knowledge an agent needs to drive umbra-py.

    A single JSON-serialisable dict: the product-type table (with one-line
    explanations), the search-parameter semantics, the polarization
    change-detection caveat, and the mandatory CC-BY license/attribution rules.
    Pull it into a model's context at the start of a session so it can pick the
    right product and build a valid search without a round trip.

    This is the *user* agent guide ("how to drive this library"), the
    counterpart to the repo's ``AGENTS.md`` *contributor* guide.
    """
    return {
        "library": "umbra-py",
        "summary": (
            "A Python-first toolkit for Umbra's open SAR data. It searches "
            "Umbra's static STAC catalog (there is no upstream STAC API), "
            "streams cloud-optimized products, and renders quicklooks, "
            "footprint maps, change composites and timescans."
        ),
        "product_types": {
            name: PRODUCT_TYPE_EXPLANATIONS[name]
            for name in PRODUCT_ASSETS
            if name in PRODUCT_TYPE_EXPLANATIONS
        },
        "product_type_order": (
            "Listed easiest-to-use first (GEC) to rawest (CPHD). Prefer GEC "
            "unless the task needs complex or phase data."
        ),
        "search_parameters": _SEARCH_PARAMETERS,
        "polarization_caveat": POLARIZATION_CAVEAT,
        "license": {
            "data_license": DATA_LICENSE,
            "attribution_required": True,
            "attribution": ATTRIBUTION,
            "note": (
                "Attribution must survive every derived product, including "
                "model-generated text describing the data."
            ),
        },
    }
