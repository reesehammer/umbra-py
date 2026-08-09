# JSON schemas

Machine-readable contracts for umbra-py's structured output, so an agent (or a
script) can depend on the shapes the CLI and library emit. These schemas are
**public API**: they follow the same backwards-compatibility rules as
everything in `umbra_py.__all__` — stable within a minor version, changed only
with a `CHANGELOG.md` entry.

| Schema | Describes | Produced by |
| --- | --- | --- |
| [`error.schema.json`](error.schema.json) | The JSON error object printed to stderr on failure. | `umbra_py.UmbraError.to_dict()`; the `cli.main` error path when `--json` / `UMBRA_JSON` is active. |
| [`download.schema.json`](download.schema.json) | The `[{asset, path, bytes, sha256}, …]` array printed to stdout on success. | `umbra download --json`. |
| [`index-info.schema.json`](index-info.schema.json) | The index-stats object (path, size, item/task counts, date span, build date) printed to stdout. | `umbra index info --json`. |
| [`render-manifest.schema.json`](render-manifest.schema.json) | The `{output, items_used, parameters}` manifest printed to stdout on success. | `umbra change` / `timescan` / `swipe` / `gallery` / `map` / `stack`, each with `--json`. |
| [`render-job.schema.json`](render-job.schema.json) | One asynchronous artifact render on an `umbra serve` instance: how far it has got and where the result will be. | Any `POST /artifacts/…` with `"async": true`; `GET /jobs/{id}` (`umbra_py.serve.job_to_dict`). |
| [`stack-stats.schema.json`](stack-stats.schema.json) | The datacube measurement: per-pass distribution, pass-to-pass and net change, the optional spatial breakdown. | `umbra_py.stack_stats`; `umbra stack --stats --json` (as the manifest's `stats`); `POST /artifacts/stats`; the `stack_stats` agent tool. |
| [`stack-provenance.schema.json`](stack-provenance.schema.json) | What a selection's sources say their pixel values are, and whether the series stacks. | `umbra_py.stack_provenance`; `umbra stack --provenance --json`; `POST /artifacts/provenance`; the `stack_provenance` agent tool. |
| [`preflight.schema.json`](preflight.schema.json) | Which acquisitions can support a measurement, read from product metadata over the wire. | `umbra_py.preflight.preflight_items`; `umbra preflight --json`. |
| [`chip-dataset.schema.json`](chip-dataset.schema.json) | What a chipping run produced: the grid, the acquisitions in it, the conversion, and the noise / speckle / skipped / preflight roll-ups. | `umbra chips --json` (`umbra_py.chips.ChipDataset.to_dict`). |
| [`chip-record.schema.json`](chip-record.schema.json) | One training tile: where it is, what the acquisition was, and what the processing did to its pixels. | Every record of a chip run's manifest — one `.jsonl` line, one `.geojson` feature's `properties`, one `.parquet` row. |
| [`chip-skipped.schema.json`](chip-skipped.schema.json) | One acquisition a chip run could not include, in the product's own words. | Each line of the `skipped.jsonl` sidecar, and each entry of the dataset summary's `skipped` array. |
| [`item-context.schema.json`](item-context.schema.json) | One acquisition as a model reads it: metadata, the SAR literacy a reader needs spelled out, and the download URLs. | `umbra_py.UmbraItem.to_llm_context()`; `umbra info --json`; each entry of a watch delta's `new_items`; the `search_catalog` / `get_item` agent tools. |
| [`scene-description.schema.json`](scene-description.schema.json) | A vision model's reading of one rendered scene, with the picture it was shown and the provenance it cannot overwrite. | `umbra describe --json` (`umbra_py.describe.SceneDescription.to_dict`); the `describe_scene` agent tool. |
| [`search-plan.schema.json`](search-plan.schema.json) | The deterministic search a plain-language question resolved to, auditable before it is run. | `umbra ask --json` (`umbra_py.planner.SearchPlan.to_dict`); the `plan_search` agent tool. |
| [`watch-delta.schema.json`](watch-delta.schema.json) | What one run of a standing watch found new since the last run. | `umbra watch --json` (`umbra_py.watch.WatchResult.to_dict`). |
| [`task-matches.schema.json`](task-matches.schema.json) | Umbra task/site names ranked against a plain-language query. | `umbra semantic search --json`. |
| [`scene-matches.schema.json`](scene-matches.schema.json) | Acquisitions ranked by visual similarity to a scene or to a text query. | `umbra embed similar --json` / `umbra embed search --json`. |
| [`site-coverage.schema.json`](site-coverage.schema.json) | One repeat-imaged site's coverage — passes, date span, revisit cadence, footprint, and the pass URLs oldest-first. | `umbra sites --json` (one object per line); the `find_repeat_sites` agent tool (in a list under `sites`). |

## Structured success output

The error contract above is the failure side; each command that produces a
result also has a `--json` success shape, so an agent can depend on stdout being
a single machine-readable object (progress and warnings stay on stderr):

- **`umbra download --json`** emits one `{asset, path, bytes, sha256}` record per
  downloaded asset ([`download.schema.json`](download.schema.json)) — the caller
  can verify each file without re-hashing it.
- **`umbra index info --json`** emits the index summary
  ([`index-info.schema.json`](index-info.schema.json)).
- **The render commands** (`change`, `timescan`, `swipe`, `gallery`, `map`) and
  the datacube writer (`stack`) emit
  a `{output, items_used, parameters}` manifest
  ([`render-manifest.schema.json`](render-manifest.schema.json)) naming the file
  produced, the acquisitions it was built from, and the settings used. A command
  that also writes an auxiliary file (e.g. `umbra change --narrate`'s narration
  JSON) lists it under an optional `sidecars` object.

## Machine-readable errors

By default, a failed command prints a prose line to stderr:

```
$ umbra map ...            # without the [viz] extra installed
error: 'folium' is required for interactive maps. Install the extra with: pip install "umbra-py[viz]"
hint: pip install "umbra-py[viz]"
```

When the invocation asks for JSON — either it already passed `--json`, or the
environment sets `UMBRA_JSON=1` — the error is emitted as a single JSON object
matching [`error.schema.json`](error.schema.json) instead, so an agent can
branch on `error` and act on `hint` without parsing prose:

```
$ UMBRA_JSON=1 umbra map ...
{"error": "MissingDependencyError", "message": "'folium' is required for interactive maps. Install the extra with: pip install \"umbra-py[viz]\"", "hint": "pip install \"umbra-py[viz]\""}
```

The `hint` is `null` when no single recovery step applies. See
[`STRATEGY.md` §7](../STRATEGY.md#7-design-principles-to-hold-onto) for the
rationale ("agents are users; users are agents").

## The measurement documents

Three of the schemas describe a *measurement* rather than an artifact, and each
is emitted by more than one front door from a single `to_dict()` — the CLI's
`--json`, an `umbra serve` route, and the MCP / LangChain / LlamaIndex agent
tool of the same name:

| Document | CLI | HTTP | Agent tool |
| --- | --- | --- | --- |
| [`stack-stats`](stack-stats.schema.json) | `umbra stack --stats --json` | `POST /artifacts/stats` | `stack_stats` |
| [`stack-provenance`](stack-provenance.schema.json) | `umbra stack --provenance --json` | `POST /artifacts/provenance` | `stack_provenance` |
| [`preflight`](preflight.schema.json) | `umbra preflight --json` | — | — |

So a shell, an HTTP client and a model read one schema per question, not three.
`render-manifest.schema.json` references `stack-stats.schema.json` for its
inline `stats` key rather than restating it, which is the same rule applied
between two schemas.

## The chip-dataset trio

A chipping run has three consumers reading three different things, so it has
three contracts rather than one:

| Document | Read by | Where it lives |
| --- | --- | --- |
| [`chip-dataset`](chip-dataset.schema.json) | an agent or a script deciding what to train on | stdout, from `umbra chips --json` |
| [`chip-record`](chip-record.schema.json) | a training loader, line by line | the manifest (`.jsonl` / `.geojson` / `.parquet`) |
| [`chip-skipped`](chip-skipped.schema.json) | whoever opens the directory later | the `skipped.jsonl` sidecar |

The record is the contract rather than the manifest *file*, because all three
manifest formats carry the same record — a `.geojson` feature's `properties`
and a `.parquet` row are the `.jsonl` line. And the summary `$ref`s
`chip-skipped.schema.json` for its own `skipped` entries instead of restating
them, so the payload and the sidecar cannot describe one left-out pass
differently.

Five of the summary's keys are **present only when the run had something to say
with them** — `conversion` (a complex product was geocoded), `noise` (a floor
was subtracted), `speckle` (a filter ran), `skipped` / `skipped_count` /
`skipped_manifest` (a pass was left out) and `preflight` (the archive was asked
first). That is deliberate, and it is in the contract: a plain GEC run's payload
is unchanged by any of those features existing, so the *absence* of `skipped` is
the statement "every acquisition offered was chipped" rather than a default.

## The agent-facing documents

Five of the schemas describe surfaces whose reader is a model or a scheduler
rather than a person, which is what makes their shape a contract rather than a
formatting choice:

| Document | Read by | Emitted by |
| --- | --- | --- |
| [`item-context`](item-context.schema.json) | a model deciding which product to ask for | `umbra info --json`, the agent tools, and every `new_items` entry of a watch delta |
| [`scene-description`](scene-description.schema.json) | whoever quotes a model's reading of a scene | `umbra describe --json` |
| [`search-plan`](search-plan.schema.json) | a person or an agent auditing a plan before running it | `umbra ask --json` |
| [`watch-delta`](watch-delta.schema.json) | a cron job or an agent acting on new acquisitions | `umbra watch --json` |
| [`task-matches`](task-matches.schema.json) / [`scene-matches`](scene-matches.schema.json) | whoever turns a ranked list into a search | `umbra semantic search --json` / `umbra embed similar\|search --json` |
| [`site-coverage`](site-coverage.schema.json) | a model choosing *which* site to analyse before it asks *what changed* | `umbra sites --json`, the `find_repeat_sites` agent tool |

Two rules run through all five, and both are in the contracts rather than only
in the docstrings. **The deterministic fields are marked as deterministic**: a
scene description's `attribution` and `provenance` are stamped on by the
library and cannot be set by a reply, a search plan's `rationale` is the one
model-authored string and never becomes a filter, and a match's `score` is a
number a test can recompute. And **the licence travels**: `item-context`,
`watch-delta` and `scene-description` each carry the CC-BY line, because it has
to survive into anything derived from the data — including model-generated text
about it (design principle 4).

`watch-delta` `$ref`s `item-context` for its `new_items` rather than restating
the card, which is the same rule `render-manifest` follows for `stack-stats`
and `chip-dataset` for `chip-skipped`: one question, one schema, wherever it is
emitted from.

`umbra download`'s URL argument is the *source STAC item*, whose contract is
STAC's own rather than this project's — so nothing here describes it. The
context card is a different document: a compact, explained reading of that item
which this project does own.

## They are reachable from an install, not only from a clone

These files are the contract, and they are also *data an installed umbra-py can
read*. `umbra_py.schemas` loads them by name:

```python
>>> from umbra_py.schemas import load_schema, schema_names
>>> "stack-stats" in schema_names()
True
>>> load_schema("stack-stats")["title"]
'Datacube statistics summary'
```

They keep one home — this directory, which is the path every schema's own `$id`
names — and the wheel carries a *copy* of it as package data
(`umbra_py/_schemas/`, via the `force-include` in `pyproject.toml`), the same way
`py.typed` is a build artifact of a source-tree fact. So the accessor reads the
packaged copy first and falls back to the checkout it was imported from, which is
what makes an editable install resolve the same files a wheel ships. Loading is
stdlib only; validating against them is the consumer's choice of validator.

That reach is what lets the HTTP surface describe itself. `umbra serve`'s
generated OpenAPI document — the whole of what an OpenAPI-driven agent or a
client generator reads — used to describe `POST /artifacts/stats` as returning a
bare object while `stack-stats.schema.json` described it exactly. Now the three
contracts its routes actually emit are merged into the document as components
(`StackStats`, `StackProvenance`, `RenderJob`, each carrying the `$id` of the
file it is a copy of as `x-umbra-schema-id`) and each route's response `$ref`s
one. They are the committed files rather than a restatement of them, so the HTTP
surface cannot drift from the contract the CLI and the agent tools emit.

## They are checked, not just described

`tests/test_schemas.py` validates a payload produced by a real surface against
every schema here, with a real JSON Schema validator (`jsonschema`, a `[dev]`
dependency). Each schema is strict — `additionalProperties: false` — so a field
added to a payload and not to its contract fails the build rather than a
consumer. The suite also checks that every schema is valid draft 2020-12, that
its `$id` matches its filename (a cross-file `$ref` resolves against it), and
that the table above names every file and no file it does not.

The `examples` these schemas carry are checked too. `examples` is a JSON Schema
*annotation* — a validator never checks its members — so an example that drifts
from the shape it illustrates (an enum value renamed, a number turned string, a
field a strict schema no longer allows) would ship as valid-looking
documentation a consumer copying it would get wrong. So the suite validates
every `examples` entry against the subschema it sits on, at every depth and for
the whole-document examples several schemas now carry alike, resolving `$defs`
and cross-file `$ref`s through the same registry the payload checks use. A
schema's example is held to the same contract as the payloads it documents.
