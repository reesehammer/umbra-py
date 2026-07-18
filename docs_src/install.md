# Install

`umbra-py` keeps its core dependency footprint tiny (just `requests` and
`click`) and ships every heavy capability behind an optional **extra**. Install
only what you need.

```bash
pip install umbra-py              # core: search + download + metadata
pip install "umbra-py[load]"      # + analysis-ready xarray loading (xarray, rasterio)
pip install "umbra-py[convert]"   # + SICD → geocoded COG (sarpy, rasterio, numpy)
pip install "umbra-py[viz]"       # + plotting / footprint / map helpers
pip install "umbra-py[export]"    # + stac-geoparquet catalog export
pip install "umbra-py[serve]"     # + the `umbra serve` read-only STAC API
pip install "umbra-py[mcp]"       # + the `umbra-mcp` Model Context Protocol server
pip install "umbra-py[langchain]" # + the catalog as native LangChain / LangGraph tools
pip install "umbra-py[llamaindex]"# + the catalog as native LlamaIndex tools
pip install "umbra-py[ai]"        # + `umbra ask` / describe / embed: model-backed NL search
pip install "umbra-py[all]"       # convert + load + viz + export together
```

Requires **Python 3.10+**.

## Choosing extras

| I want to…                                   | Install                |
| -------------------------------------------- | ---------------------- |
| Search and download open data                | `umbra-py` (core)      |
| Open a scene as an array                      | `umbra-py[load]`       |
| Geocode a SICD into a GeoTIFF                 | `umbra-py[convert]`    |
| Make maps, galleries, quicklooks             | `umbra-py[viz]`        |
| Export the catalog to GeoParquet             | `umbra-py[export]`     |
| Serve a STAC API                             | `umbra-py[serve]`      |
| Drive the catalog from an LLM / agent        | `umbra-py[mcp]`, `[langchain]`, or `[llamaindex]` |
| Natural-language search / scene description  | `umbra-py[ai]`         |

## The determinism boundary

The AI features are opt-in and never implicit. A model is only ever called at
the *edge*: `umbra ask` has a model *plan* a search (the plan is re-validated
before it runs), `umbra describe` has a vision model *read* a rendered
quicklook (returned as a provenance-stamped description, never a filter), and
`umbra embed` turns a quicklook or a text query into a vector for visual
similarity search (only the embedding step calls a model; ranking is
deterministic). The `[ai]` extra pulls in nothing beyond the core `requests`
dependency — you supply an API key at runtime.
