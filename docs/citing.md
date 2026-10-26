# Citing umbra-py

Cite the software when a paper, report, or dataset depends on it. Attribute
the imagery separately: the code is Apache-2.0, and the scenes are Umbra
Space open data under CC BY 4.0.

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.TBD.svg)](https://doi.org/10.5281/zenodo.TBD)

!!! note "DOI placeholder"
    `10.5281/zenodo.TBD` is not a minted DOI. After the first GitHub release
    that Zenodo archives, replace it — here, in the README, and in
    `CITATION.cff` — with the concept DOI. On the Zenodo record that is the
    value under **Cite all versions?**. It resolves to the latest archived
    version. The DOI in the record's badge is the version DOI for that
    release only.

```bibtex
@software{hammerstrom2026umbrapy,
  author  = {Hammerstrom, Reese},
  title   = {umbra-py},
  year    = {2026},
  version = {0.1.4},
  doi     = {10.5281/zenodo.TBD},
  url     = {https://github.com/reesehammer/umbra-py},
  license = {Apache-2.0}
}
```

Machine-readable metadata:

- [CITATION.cff](https://github.com/reesehammer/umbra-py/blob/main/CITATION.cff)
  — Citation File Format 1.2.0. GitHub renders it as **Cite this repository**.
- [.zenodo.json](https://github.com/reesehammer/umbra-py/blob/main/.zenodo.json)
  — the metadata Zenodo copies onto the archive. When both files are present,
  Zenodo uses `.zenodo.json` and ignores `CITATION.cff` for the deposit.

## Data attribution

Imagery is © Umbra Space, licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Umbra's
[Satellite Tasking Services Terms and Conditions](https://umbra.space/umbra-satellite-tasking-services-terms-and-conditions/)
§3(a) license archival content published on umbra.space, including the
[Open Data program](https://umbra.space/open-data/), under CC BY 4.0.
§3(b) requires this copyright notice on or adjacent to adapted materials,
with `[YEAR]` the year the image was first created:

> SAR-derived data © [YEAR] Umbra Lab, Inc.

Also credit Umbra, link the license, and say if you changed the data.
umbra-py stamps a shorter line on its own outputs: *"Contains Umbra open
data, licensed under CC BY 4.0."* That line does not replace the notice
above. This project is not affiliated with or endorsed by Umbra Lab, Inc.
