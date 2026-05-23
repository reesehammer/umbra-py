"""How the five baseline scripts collapse when umbra-py is installed.

Each function below replaces one (or two) of ``01_search_catalog_pystac.py``
.. ``05_open_sicd.py`` from this folder. ``main`` runs the search + classify
flow end-to-end against Umbra's live catalog; the download and SICD
conversion entry points are left for you to wire up to a real asset.

Install::

    pip install umbra-py            # for 01 - 04
    pip install "umbra-py[convert]" # for 05 (sarpy, rasterio, numpy)

Run::

    python 06_with_umbra_py.py
"""

from __future__ import annotations

from pathlib import Path

from umbra_py import UmbraCatalog, UmbraItem, download_item


# Replaces 01_search_catalog_pystac.py and 02_search_catalog_handrolled.py.
# ``search`` prunes child catalogs by date token *before* fetching them, and
# intersects each item's bbox in-traversal -- the heuristics live in the
# library instead of in your script.
def search() -> list[UmbraItem]:
    catalog = UmbraCatalog()
    return list(
        catalog.search(
            bbox=(-68.1, 10.4, -67.9, 10.6),
            start="2024-02-08",
            end="2024-02-08",
            product_types=["GEC"],
            limit=5,
        )
    )


# Replaces 03_find_the_geotiff.py. ``asset_href`` resolves the canonical
# product type ("GEC", "SICD", ...) against both the old explicit-key
# layout and the new filename-key layout (``..._GEC_MM.tif``).
def find_geotiff(item: UmbraItem) -> str:
    return item.asset_href("GEC")


# Replaces 04_download_assets.py. Anonymous HTTPS streaming with a ``.part``
# sidecar and HTTP ``Range`` resume happen inside ``download_item``.
def download_gec(item: UmbraItem, dest_dir: Path) -> list[Path]:
    return download_item(item, dest_dir=dest_dir, assets=["GEC"])


# Replaces 05_open_sicd.py. Requires the ``[convert]`` extra.
def sicd_amplitude(src: Path, dst: Path) -> Path:
    from umbra_py.convert import sicd_to_amplitude_geotiff

    return sicd_to_amplitude_geotiff(src, dst)


def main() -> None:
    items = search()
    if not items:
        print("no items matched the example query")
        return

    item = items[0]
    print(item.summary())
    print()
    print("available assets:", item.available_assets)
    print("GEC href        :", find_geotiff(item))

    # Uncomment to actually pull data (a GEC GeoTIFF can be hundreds of MB).
    # paths = download_gec(item, Path("downloads"))
    # print("downloaded      :", paths)

    # To convert a SICD to an inspection-quality amplitude GeoTIFF:
    # sicd_amplitude(Path("input_SICD.nitf"), Path("amplitude.tif"))


if __name__ == "__main__":
    main()
