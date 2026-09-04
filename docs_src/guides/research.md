# Used in research

Independent research has already used Umbra's open archive as an ISR-style
training source. This page points at that work and at the umbra-py path that
gets you from catalog filters to trainable chips — **discovery → arrays**, not
the model.

## ProSR

[ProSR](https://arxiv.org/abs/2609.02377) (Kim & Kim, KAIST-VICLab) curated
about **502 Umbra SLC** acquisitions into roughly **132k patches at 0.25 m** as
an ISR super-resolution benchmark. Code and data notes live at
[KAIST-VICLab/ProSR](https://github.com/KAIST-VICLab/ProSR).

umbra-py does **not** reimplement ProSR or any diffusion / SR model. It covers
the data path those papers need: filtered search, size-aware download, SICD →
geocoded COG conversion, and georeferenced ML chips.

## How umbra-py helps

- **Index search** with polarization and incidence filters
  (`CatalogIndex.from_release()` / `index.search(...)`) so multi-filter corpus
  assembly does not crawl S3.
- **Download** (`download_item`) after confirming asset size (e.g. HTTP `HEAD` /
  `Content-Length` on the asset `href`).
- **Convert** (`sicd_to_geocoded_cog` / `umbra convert`) when you need SLC
  amplitude on a map.
- **Chips** (`write_chips` / `umbra chips`) for georeferenced training tiles from
  open GEC (or a converted SICD COG).

Walkthrough:
[`examples/09_isr_training_set.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/09_isr_training_set.ipynb)
(search → size-check → chip). Chip mechanics alone:
[`examples/05_detection_chips.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/05_detection_chips.ipynb).

## License & affiliation

Umbra open imagery is **CC BY 4.0** — attribute *"Contains Umbra open data,
licensed under CC BY 4.0."* when you publish derived products. This is an
independent, unofficial toolkit and is **not affiliated with Umbra Lab, Inc.**
