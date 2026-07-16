# JSON schemas

Machine-readable contracts for umbra-py's structured output, so an agent (or a
script) can depend on the shapes the CLI and library emit. These schemas are
**public API**: they follow the same backwards-compatibility rules as
everything in `umbra_py.__all__` — stable within a minor version, changed only
with a `CHANGELOG.md` entry.

| Schema | Describes | Produced by |
| --- | --- | --- |
| [`error.schema.json`](error.schema.json) | The JSON error object printed to stderr on failure. | `umbra_py.UmbraError.to_dict()`; the `cli.main` error path when `--json` / `UMBRA_JSON` is active. |

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
`docs/AI_INTEGRATION_IDEAS.md` §A1 for the rationale.
