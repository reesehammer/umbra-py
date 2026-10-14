# Download

Resume-safe HTTPS downloads with integrity verification: a short read or a
mid-stream break keeps the `.part` for resume, a changed object restarts
cleanly, and the finished file is hashed against the object's ETag.

## By filename or acquisition stem

`umbra info` and `umbra download` take a sidecar URL (`*.stac.v2.json`, or a
legacy `*_METADATA.json` for 2023 to 2024 collects). They also take a
GEC/SICD filename or the acquisition stem once a local catalog index is
present (`umbra index fetch`):

```bash
umbra index fetch
umbra info 2025-05-17-17-43-46_UMBRA-08_GEC.tif
umbra info 2025-05-17-17-43-46_UMBRA-08
umbra download 2025-05-17-17-43-46_UMBRA-08 --asset GEC --dest ./downloads
```

The stem is `YYYY-MM-DD-HH-MM-SS_UMBRA-NN`. A product file is that stem plus
a suffix such as `_GEC.tif` or `_SICD.nitf`. The same string is published
under `sar-data/tasks/`, `sar-data/task-data/`, or `open-data/`, and the
filename does not say which, so the lookup matches the stem against asset
hrefs in the index. Nothing matched is an error. More than one match lists
the candidate STAC URLs; pass one of those to either command.

The MCP `get_item` tool accepts the same filename or stem in its `url`
argument. `umbra quicklook`, `umbra view`, and `umbra load` still take a
sidecar URL, not a filename.

::: umbra_py.download_item

::: umbra_py.download_asset

::: umbra_py.download_url
