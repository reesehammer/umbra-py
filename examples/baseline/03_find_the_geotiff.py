"""Figure out which STAC asset is the GEC GeoTIFF (it depends on vintage).

Why this is here
----------------
Umbra has shipped at least two generations of STAC item layouts:

Old (early 2023-era) — assets are keyed by *product type*::

    {
      "assets": {
        "GEC":   {"href": ".../foo.tif",  "type": "image/tiff; ..."},
        "SICD":  {"href": ".../foo.nitf", "type": "application/octet-stream"},
        "SIDD":  {"href": ".../foo.nitf", "type": "application/octet-stream"},
        "CPHD":  {"href": ".../foo.cphd"},
        "metadata": {"href": ".../foo.json"}
      }
    }

New (recent acquisitions) — assets are keyed by *filename*::

    {
      "assets": {
        "2024-12-01-13-22-37_UMBRA-04_GEC_MM.tif":  {...},
        "2024-12-01-13-22-37_UMBRA-04_SICD_MM.nitf": {...},
        "2024-12-01-13-22-37_UMBRA-04_CSI_MM.tif":  {...},
        ...
      }
    }

A naive ``item.assets["GEC"]`` lookup works on the first but raises
``KeyError`` on the second, and every consumer ends up reinventing the same
heuristic to map an arbitrary key to ``GEC | CSI | SIDD | SICD | CPHD``.

The classifier below is exactly the heuristic umbra-py uses
(``src/umbra_py/models.py:_classify_asset``); reproducing it here is the cost
of doing this without the library.

Run::

    python 03_find_the_geotiff.py
"""

from __future__ import annotations

# Canonical product types, ordered from most processed (start here) to most raw.
PRODUCT_ASSETS = ("GEC", "CSI", "SIDD", "SICD", "CPHD")


def classify_asset(key: str, asset: dict) -> str | None:
    """Map a STAC asset to one of GEC | CSI | SIDD | SICD | CPHD | metadata.

    Looks at both the asset key and its href, because the filename-style keys
    embed the product type as a substring.
    """
    haystack = f"{key} {asset.get('href', '')}".upper()
    media = (asset.get("type") or "").lower()
    is_geotiff = ".tif" in haystack.lower() or "geotiff" in media

    if "CPHD" in haystack:
        return "CPHD"
    if "SICD" in haystack:
        return "SICD"
    if "SIDD" in haystack:
        return "SIDD"
    if "CSI" in haystack and is_geotiff:
        return "CSI"
    if "METADATA" in haystack:
        return "metadata"
    if is_geotiff:
        return "GEC"
    return None


def asset_map(item: dict) -> dict[str, str]:
    """Return {canonical_product_type: actual_asset_key} for one item.

    When several assets resolve to the same product type (e.g. a SIDD and a
    CSI both classify as imagery), prefer the non-CSI "primary" one.
    """
    result: dict[str, str] = {}
    for key, asset in item.get("assets", {}).items():
        canon = classify_asset(key, asset)
        if canon is None:
            continue
        existing = result.get(canon)
        if existing is not None:
            if "CSI" in key.upper() and "CSI" not in existing.upper():
                continue
        result[canon] = key
    return result


def asset_href(item: dict, product_type: str) -> str:
    """Return the download URL for ``GEC`` (or any other product type)."""
    mapping = asset_map(item)
    key = mapping.get(product_type)
    if key is None:
        available = ", ".join(p for p in PRODUCT_ASSETS if p in mapping) or "none"
        raise KeyError(f"Item has no {product_type!r} asset. Available: {available}.")
    return item["assets"][key]["href"]


# Two minimal fixtures showing both layouts. In practice you pull these from
# the catalog (see 02_search_catalog_handrolled.py).
OLD_STYLE_ITEM = {
    "id": "2d1827a2-d04c-4e15-9319-3810d011540b",
    "assets": {
        "metadata": {"href": "s3://.../foo.json", "type": "application/json"},
        "GEC": {
            "href": "s3://.../foo.tif",
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        },
        "SICD": {"href": "s3://.../foo.nitf", "type": "application/vnd.nitf"},
        "SIDD": {"href": "s3://.../foo.nitf", "type": "application/vnd.nitf"},
        "CPHD": {"href": "s3://.../foo.cphd", "type": "application/octet-stream"},
    },
}

NEW_STYLE_ITEM = {
    "id": "2024-12-01-13-22-37_UMBRA-04",
    "assets": {
        "2024-12-01-13-22-37_UMBRA-04_GEC_MM.tif": {
            "href": "s3://.../2024-12-01-13-22-37_UMBRA-04_GEC_MM.tif",
            "type": "image/tiff; application=geotiff",
        },
        "2024-12-01-13-22-37_UMBRA-04_CSI_MM.tif": {
            "href": "s3://.../2024-12-01-13-22-37_UMBRA-04_CSI_MM.tif",
            "type": "image/tiff; application=geotiff",
        },
        "2024-12-01-13-22-37_UMBRA-04_SICD_MM.nitf": {
            "href": "s3://.../2024-12-01-13-22-37_UMBRA-04_SICD_MM.nitf",
            "type": "application/octet-stream",
        },
        "2024-12-01-13-22-37_UMBRA-04_METADATA.json": {
            "href": "s3://.../2024-12-01-13-22-37_UMBRA-04_METADATA.json",
            "type": "application/json",
        },
    },
}


def demo(label: str, item: dict) -> None:
    print(f"--- {label} ---")
    print("raw asset keys :", list(item["assets"]))
    print("classified     :", asset_map(item))
    print("GEC href       :", asset_href(item, "GEC"))
    # The naive way is fine for the old style and explodes for the new style:
    try:
        print('naive  ["GEC"] :', item["assets"]["GEC"]["href"])
    except KeyError as exc:
        print(f'naive  ["GEC"] : KeyError({exc!r})')
    print()


def main() -> None:
    demo("old-style item (explicit keys)", OLD_STYLE_ITEM)
    demo("new-style item (filename keys)", NEW_STYLE_ITEM)


if __name__ == "__main__":
    main()
