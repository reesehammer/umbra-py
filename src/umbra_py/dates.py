"""Deterministic natural-language date parsing for search bounds.

``umbra search --start "3 months ago"`` should just work, without a model in
the loop. This module resolves human date expressions to concrete
:class:`datetime.date` bounds with plain calendar arithmetic -- the
deterministic first step of the natural-language search direction in
``docs/AI_INTEGRATION_IDEAS.md`` (C1): natural language in, an exact date out,
no LLM at runtime, fully offline-testable. It sits inside the library's
determinism boundary (the core never calls a model), so every command that
takes ``--start`` / ``--end`` -- ``search``, ``index build``, ``change``,
``timescan``, ``swipe``, ``map``, ``gallery``, and the MCP ``search_catalog``
tool -- inherits it for free, because they all funnel through
:func:`umbra_py.catalog._coerce_date`.

The resolver is *bound-aware*. A partial or period expression names a *span*
of days, and ``is_end`` selects which edge of that span to return, so
``--start EXPR --end EXPR`` stays intuitive -- each side snaps to the natural
edge of whatever the user named:

- ``"2024"`` is ``2024-01-01`` as a start bound and ``2024-12-31`` as an end
  bound; ``"2024-03"`` is the first vs. last day of that month.
- ``"last month"`` is the first vs. last day of the previous month;
  ``"this week"`` is Monday vs. Sunday of the current ISO week.
- A full ISO date (``YYYY-MM-DD``) and a point offset (``"3 months ago"``,
  ``"yesterday"``) are unambiguous single days and ignore ``is_end``.

Everything anchors on a single "today" (defaulting to :meth:`date.today`,
injectable via ``today=`` for tests), so the module is deterministic given that
anchor. Unrecognized input raises :class:`ValueError` with a message that lists
the accepted forms -- a self-describing error an agent can recover from.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime

__all__ = ["parse_date_bound"]

DateInput = str | date | datetime | None

_ISO_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR = re.compile(r"^(\d{4})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
# "3 months ago", "1 day ago", "a week ago" -- a point offset behind today.
_AGO = re.compile(r"^(?:(\d+)|an?)\s+(day|week|month|year)s?\s+ago$")
# "this/last week|month|year" -- a span whose edge `is_end` selects.
_PERIOD = re.compile(r"^(this|last)\s+(week|month|year)$")


def _month_last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift ``(year, month)`` by ``delta`` months (delta may be negative)."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _clamp_day(year: int, month: int, day: int) -> date:
    """A valid date in ``(year, month)``, clamping the day to the month length
    so e.g. one month before March 31 lands on the last day of February."""
    return date(year, month, min(day, _month_last_day(year, month)))


def _shift(anchor: date, unit: str, count: int) -> date:
    """``anchor`` moved by ``count`` (signed) units -- a single point in time."""
    if unit == "day":
        return date.fromordinal(anchor.toordinal() + count)
    if unit == "week":
        return date.fromordinal(anchor.toordinal() + 7 * count)
    if unit == "year":
        return _clamp_day(anchor.year + count, anchor.month, anchor.day)
    year, month = _add_months(anchor.year, anchor.month, count)
    return _clamp_day(year, month, anchor.day)


def _period_edge(today: date, unit: str, delta: int, is_end: bool) -> date:
    """The edge (start or, when ``is_end``, end) of the ``this``/``last``
    (``delta`` 0 or -1) week / month / year containing ``today``."""
    if unit == "week":
        # ISO week: Monday is the first day (weekday() == 0).
        monday = date.fromordinal(today.toordinal() - today.weekday() + 7 * delta)
        return date.fromordinal(monday.toordinal() + 6) if is_end else monday
    if unit == "month":
        year, month = _add_months(today.year, today.month, delta)
        day = _month_last_day(year, month) if is_end else 1
        return date(year, month, day)
    year = today.year + delta
    return date(year, 12, 31) if is_end else date(year, 1, 1)


def parse_date_bound(
    value: DateInput,
    *,
    is_end: bool = False,
    today: date | None = None,
) -> date | None:
    """Resolve a date expression to a concrete :class:`date`.

    ``value`` may be ``None`` (returns ``None``), a :class:`date` /
    :class:`datetime` (returned as a date, unchanged), or a string in any of
    the accepted forms:

    - a full ISO date ``YYYY-MM-DD``;
    - a bare year ``2024`` or year-month ``2024-03`` (see ``is_end``);
    - ``today`` / ``now`` / ``yesterday`` / ``tomorrow``;
    - a point offset ``"<n> <unit> ago"`` (unit: day/week/month/year, also
      ``"a week ago"``); or
    - a period ``this``/``last`` ``week``/``month``/``year`` (see ``is_end``).

    ``is_end`` disambiguates spans: a bare year, year-month, or period keyword
    resolves to the *first* day of that span by default and its *last* day when
    ``is_end`` is true, so a start bound and an end bound each snap to the
    natural edge. Full ISO dates and point offsets are single days and ignore
    ``is_end``.

    ``today`` overrides the anchor for relative expressions (defaults to
    :meth:`date.today`), which keeps the resolver deterministic under test.

    Raises :class:`ValueError` for an unrecognized string, with a message that
    lists the accepted forms.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = value.strip().lower()
    if not text:
        raise ValueError("empty date expression")

    if _ISO_FULL.match(text):
        # Unambiguous single day; validate via the stdlib parser.
        return date.fromisoformat(text)

    m = _YEAR.match(text)
    if m:
        year = int(m.group(1))
        return date(year, 12, 31) if is_end else date(year, 1, 1)

    m = _YEAR_MONTH.match(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"invalid month in date {value!r}")
        day = _month_last_day(year, month) if is_end else 1
        return date(year, month, day)

    anchor = today or date.today()
    if text in ("today", "now"):
        return anchor
    if text == "yesterday":
        return _shift(anchor, "day", -1)
    if text == "tomorrow":
        return _shift(anchor, "day", 1)

    m = _AGO.match(text)
    if m:
        count = 1 if m.group(1) is None else int(m.group(1))
        return _shift(anchor, m.group(2), -count)

    m = _PERIOD.match(text)
    if m:
        which, unit = m.group(1), m.group(2)
        return _period_edge(anchor, unit, -1 if which == "last" else 0, is_end)

    raise ValueError(
        f"Unrecognized date {value!r}. Use an ISO date (YYYY-MM-DD), a year or "
        "year-month (2024, 2024-03), 'today'/'yesterday'/'tomorrow', a relative "
        "offset ('3 months ago', 'a week ago'), or a period ('this month', "
        "'last year')."
    )
