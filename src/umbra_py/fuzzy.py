"""Fuzzy / alias task-name matching for ``area=`` search.

By default ``area=`` is a literal case-insensitive substring against each Umbra
task directory name (e.g. ``area="centerfield"`` matches ``"Centerfield,
Utah"``). That is exact and fast, but it misses the ways people -- and language
models paraphrasing a request -- actually type a site name:

* a different word order (``"utah centerfield"``),
* punctuation the label carries and the query drops (``"centerfield utah"``
  against ``"Centerfield, Utah"``), or
* a small typo (``"centrfield"``).

``fuzzy`` mode is the *deterministic first step* of the C1 natural-language
search plan (see ``docs/AI_INTEGRATION_IDEAS.md``): natural language in, an
ordinary filter out, **no model call at runtime**. It stays inside the
library's determinism boundary -- plain token comparison and :mod:`difflib`,
nothing learned, nothing networked -- so it is fully offline-testable.

Two properties are load-bearing:

1. **It is a strict superset of the substring match.** Anything the plain
   substring path returns, fuzzy also returns, so turning it on never *drops*
   a result -- it only widens. Both the live (:class:`~umbra_py.catalog.UmbraCatalog`)
   and the indexed (:class:`~umbra_py.index.CatalogIndex`) search paths share
   this module, so they agree.
2. **Semantic aliasing is deliberately out of scope.** ``"grain storage north
   dakota"`` reaching ``"Beet Piler - ND"`` needs an embedding index (tracked
   for the future ``[ai]`` extra); it is *not* something plain string
   similarity can or should fake.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

__all__ = ["task_matches", "matching_tasks"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A query token matches a task token when it is a substring of it (so prefixes
# and the plain substring case keep working) or is close enough under difflib's
# ratio to absorb a small typo. 0.8 accepts roughly one edit in a ~5-character
# token while still rejecting unrelated words ("port" vs "point" is ~0.67).
_TOKEN_RATIO = 0.8


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, punctuation and spacing discarded."""
    return _TOKEN_RE.findall(text.lower())


def _token_matches(query_token: str, task_token: str) -> bool:
    if query_token in task_token:
        return True
    return SequenceMatcher(None, query_token, task_token).ratio() >= _TOKEN_RATIO


def task_matches(query: str, task_name: str, *, fuzzy: bool = True) -> bool:
    """Return ``True`` if ``query`` should match ``task_name``.

    With ``fuzzy=False`` this is exactly the legacy case-insensitive substring
    test. With ``fuzzy=True`` it is that OR a token-wise fuzzy match: **every**
    token in ``query`` must match **some** token in ``task_name`` (order- and
    punctuation-independent), where a per-token match is a substring or a close
    :func:`difflib.SequenceMatcher` ratio. Requiring every query token keeps
    precision -- a two-word query does not match a task that only shares one
    word.
    """
    if query.lower() in task_name.lower():
        return True
    if not fuzzy:
        return False
    query_tokens = _tokens(query)
    task_tokens = _tokens(task_name)
    if not query_tokens or not task_tokens:
        return False
    return all(any(_token_matches(q, t) for t in task_tokens) for q in query_tokens)


def matching_tasks(query: str, task_names: Iterable[str], *, fuzzy: bool = True) -> list[str]:
    """Filter ``task_names`` to those matching ``query`` (see :func:`task_matches`),
    preserving input order."""
    return [name for name in task_names if task_matches(query, name, fuzzy=fuzzy)]
