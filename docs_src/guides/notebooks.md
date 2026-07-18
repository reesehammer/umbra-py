# Example notebooks

The [`examples/`](https://github.com/reesehammer/umbra-py/tree/main/examples)
directory holds runnable notebooks that walk through the library end to end,
from a first search to SICD amplitude extraction. Each one is offline-friendly
where it can be and points at Umbra's live catalog where it must be.

| # | Notebook | What it covers |
| - | -------- | -------------- |
| 01 | [`01_hello_umbra.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/01_hello_umbra.ipynb) | First search against the live catalog; inspecting items. |
| 02 | [`02_download_and_open_gec.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/02_download_and_open_gec.ipynb) | Download a GEC product and open it as an array. |
| 03 | [`03_change_detection.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/03_change_detection.ipynb) | Multi-temporal change composites over a site. |
| 04 | [`04_amplitude_time_series.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/04_amplitude_time_series.ipynb) | Build an amplitude time series across passes. |
| 05 | [`05_detection_chips.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/05_detection_chips.ipynb) | Cut a scene into georeferenced ML training chips. |
| 06 | [`06_site_monitoring.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/06_site_monitoring.ipynb) | A standing-analyst monitoring loop over a site. |
| 07 | [`07_sicd_amplitude.ipynb`](https://github.com/reesehammer/umbra-py/blob/main/examples/07_sicd_amplitude.ipynb) | Extract amplitude from a complex SICD product. |

Run them locally with the matching extras installed, e.g.:

```bash
pip install "umbra-py[all]" jupyter
jupyter lab examples/
```
