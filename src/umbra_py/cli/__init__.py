"""Command-line interface: ``umbra search | info | download | map``.

This package is the ``cli.py`` module that grew past five thousand lines, split
along the seams the commands already had. Nothing moved out of the CLI and no
name changed: :data:`cli` is the same Click group, every command is registered
on it by importing its module below, and this file re-exports every name the
module defined so ``from umbra_py.cli import …`` keeps working.

The layout, in the order a reader would walk it:

* :mod:`._root` -- the ``umbra`` group itself, the ``UMBRA_JSON_ERRORS``
  envelope, and the ``main()`` entry point. It imports no command module, so
  the commands can import it.
* :mod:`._shared` -- the option groups (geography, task name, acquisition
  properties, token, manifest) and the search-vs-explicit-URLs gathering that
  more than one command needs.
* :mod:`.discover` -- ``search``, ``watch``, ``info``, ``context``,
  ``llms-txt``, ``ask``: which acquisitions exist.
* :mod:`.scenes` -- ``describe``, ``download``, ``quicklook``, ``view``,
  ``load``: one acquisition at a time.
* :mod:`.process` -- ``stack``, ``convert``, ``chips``: data products.
* :mod:`.composites` -- ``change``, ``timescan``, ``swipe``: multi-pass
  pictures of one site.
* :mod:`.atlas` -- ``map``, ``gallery``: where the archive has imagery.
* :mod:`.explore` -- ``mcp``, ``serve``, ``demo``, ``tiles``, ``showcase``:
  the commands that stand something up.
* :mod:`.indexes` -- ``index``, ``semantic``, ``embed``: the local sidecars.

Import order matters exactly once: ``_root`` defines the group the command
modules decorate, so it comes first. Everything after it is registration.
"""

from __future__ import annotations

from ._root import _emit_umbra_error, _json_errors_requested, cli, main
from ._shared import (
    _acquisition_filter_kwargs,
    _acquisition_filter_manifest,
    _acquisition_filter_options,
    _area_option,
    _baked_previews,
    _baked_thumbnails,
    _built_note,
    _check_token_not_local,
    _emit_render_manifest,
    _fuzzy_option,
    _gather_featured_sites,
    _gather_items,
    _geometry_option,
    _index_path,
    _item_from_url,
    _local_index_options,
    _manifest_option,
    _parse_bbox,
    _parse_percentile,
    _place_option,
    _progress_printer,
    _resolve_aois,
    _resolve_geography,
    _resolve_intersects,
    _resolve_search_bbox,
    _search_source,
    _search_subtitle,
    _sha256_file,
    _token_option,
)

# Importing a command module is what registers its commands on ``cli``; the
# names re-exported beside it are the ones tests and docs address by name.
from .atlas import gallery, map_cmd  # noqa: E402
from .composites import change, swipe, timescan  # noqa: E402
from .discover import (  # noqa: E402
    _print_watch_result,
    ask,
    context,
    info,
    llms_txt_cmd,
    search,
    watch_cmd,
)
from .explore import _tiles_fetch, demo, mcp, serve, showcase, tiles  # noqa: E402
from .indexes import (  # noqa: E402
    _embed_path,
    _print_scene_matches,
    _semantic_path,
    embed,
    embed_build,
    embed_fetch,
    embed_info,
    embed_search,
    embed_similar,
    index,
    index_bake,
    index_bake_thumbnails,
    index_build,
    index_export,
    index_export_thumbnails,
    index_fetch,
    index_fetch_thumbnails,
    index_info,
    index_update,
    semantic,
    semantic_build,
    semantic_info,
    semantic_search,
)
from .process import chips, convert, stack  # noqa: E402
from .scenes import describe, download, load_cmd, quicklook, view  # noqa: E402

__all__ = ["cli", "main"]
