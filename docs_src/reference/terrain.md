# Terrain: DEM & geoid

Terrain support for [conversion](convert.md): auto-fetch the covering
Copernicus GLO-30 DEM tiles and the EGM geoid grid, so `umbra convert --dem
auto --geoid auto` needs no hand-found elevation data.

## DEM

::: umbra_py.fetch_dem_for_bbox

::: umbra_py.copernicus_tile_id

::: umbra_py.tile_ids_for_bbox

::: umbra_py.default_dem_cache_dir

::: umbra_py.DemUnavailableError

## Geoid

::: umbra_py.fetch_geoid_grid

::: umbra_py.geoid_grid_url

::: umbra_py.default_geoid_cache_dir
