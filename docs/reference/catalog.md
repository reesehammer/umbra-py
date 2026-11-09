# Catalog search

The `UmbraCatalog` is the entry point for discovery: it searches Umbra's static
STAC catalog by area, date range, product type, place name, or polygon, and can
read through a [local index](index.md) for fast, repeatable, offline queries.

A full scan of the open archive should not walk the bucket and should not
page the hosted `/search`. Download the weekly snapshot with
`umbra index fetch` (flags `--db`, `--url`, `--if-changed`, and `--force`;
`--force` only with `--if-changed`) from
[`catalog.db`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.db),
then search it with `umbra search --local`. The [local index](index.md)
page says what each flag does.
[`catalog.sha256`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.sha256)
is the GNU `sha256sum` manifest the weekly release will carry once
[PR #315](https://github.com/reesehammer/umbra-py/pull/315) is in the weekly
publish. It is not on the release yet, and `umbra index fetch` does not check it.

::: umbra_py.UmbraCatalog
