# Security Policy

## Reporting a vulnerability

Please report security issues **privately** so they can be fixed before public
disclosure. Do **not** open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting: go to the repository's
[**Security** tab](https://github.com/reesehammer/umbra-py/security) and choose
**Report a vulnerability**. This opens a private advisory visible only to you
and the maintainers.

Please include, as far as you can:

- the version (or commit) of `umbra-py` affected;
- a description of the issue and its impact;
- steps to reproduce, or a minimal proof of concept.

We aim to acknowledge a report within a few days and will keep you updated on
the fix and any coordinated disclosure timeline.

## Supported versions

`umbra-py` is early-alpha (`0.x`). Fixes land on `main` and ship in the next
release; there are no long-term support branches yet.

| Version | Supported          |
| ------- | ------------------ |
| `main` / latest release | :white_check_mark: |
| older `0.x` releases    | :x:                |

## Security posture

A few properties of the library are worth stating, because they shape what a
vulnerability here can and cannot do:

- **No credentials, no auth surface.** Access to Umbra's open data is anonymous
  HTTPS to a public bucket. The library stores no secrets and has no login,
  server-side session, or token handling on the open-data path. The optional
  Canopy commercial-archive backend and the `[ai]` features take a
  user-supplied token/API key at runtime and send it **only** to the endpoint
  it authenticates (the Canopy API or the configured model provider) — never to
  the open bucket.
- **The trust boundary is remote content and generated files.** The realistic
  attack surface is (a) STAC/XML metadata parsed from a bucket or a
  user-supplied URL, (b) files the library writes to disk, and (c) the static
  HTML/JS artifacts it emits (maps, galleries, swipe/change pages, the
  `umbra demo` explorer). Generated HTML routes every remote-derived string
  through `html.escape()` and every clickable remote link through a scheme
  allowlist (`_html.safe_href`), so a hostile STAC document cannot inject script
  into an artifact you open locally.
- **Optional AI features are opt-in and never implicit.** Anything that calls a
  model lives behind the `[ai]` extra and runs only when you invoke it
  explicitly; model output is re-validated by the deterministic core and never
  becomes a filter, URL, or coordinate on its own.

If you find a way to violate any of the above, that is exactly the kind of
report we want to receive.
