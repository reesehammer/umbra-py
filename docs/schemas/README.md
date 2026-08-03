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
| [`stack-stats.schema.json`](stack-stats.schema.json) | The datacube measurement: per-pass distribution, pass-to-pass and net change, the optional spatial breakdown. | `umbra_py.stack_stats`; `umbra stack --stats --json` (as the manifest's `stats`); `POST /artifacts/stats`; the `stack_stats` agent tool. |
| [`stack-provenance.schema.json`](stack-provenance.schema.json) | What a selection's sources say their pixel values are, and whether the series stacks. | `umbra_py.stack_provenance`; `umbra stack --provenance --json`; `POST /artifacts/provenance`; the `stack_provenance` agent tool. |
| [`preflight.schema.json`](preflight.schema.json) | Which acquisitions can support a measurement, read from product metadata over the wire. | `umbra_py.preflight.preflight_items`; `umbra preflight --json`. |
| [`chip-dataset.schema.json`](chip-dataset.schema.json) | What a chipping run produced: the grid, the acquisitions in it, the conversion, and the noise / speckle / skipped / preflight roll-ups. | `umbra chips --json` (`umbra_py.chips.ChipDataset.to_dict`). |
| [`chip-record.schema.json`](chip-record.schema.json) | One training tile: where it is, what the acquisition was, and what the processing did to its pixels. | Every record of a chip run's manifest — one `.jsonl` line, one `.geojson` feature's `properties`, one `.parquet` row. |
| [`chip-skipped.schema.json`](chip-skipped.schema.json) | One acquisition a chip run could not include, in the product's own words. | Each line of the `skipped.jsonl` sidecar, and each entry of the dataset summary's `skipped` array. |

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

## They are checked, not just described

`tests/test_schemas.py` validates a payload produced by a real surface against
every schema here, with a real JSON Schema validator (`jsonschema`, a `[dev]`
dependency). Each schema is strict — `additionalProperties: false` — so a field
added to a payload and not to its contract fails the build rather than a
consumer. The suite also checks that every schema is valid draft 2020-12, that
its `$id` matches its filename (a cross-file `$ref` resolves against it), and
that the table above names every file and no file it does not.
