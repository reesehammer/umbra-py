"""Tests for deterministic natural-language date-bound parsing."""

from datetime import date, datetime

import pytest

from umbra_py import parse_date_bound
from umbra_py.catalog import _coerce_date

# A fixed anchor so every relative expression resolves deterministically.
# 2024-03-15 is a Friday, so ISO-week math is easy to reason about.
TODAY = date(2024, 3, 15)


def parse(value, *, is_end=False):
    return parse_date_bound(value, is_end=is_end, today=TODAY)


# -- passthrough and None -----------------------------------------------------


def test_none_returns_none():
    assert parse(None) is None


def test_date_and_datetime_pass_through():
    assert parse(date(2023, 5, 1)) == date(2023, 5, 1)
    assert parse(datetime(2023, 5, 1, 12, 30)) == date(2023, 5, 1)


# -- ISO dates ----------------------------------------------------------------


def test_full_iso_date_is_edge_independent():
    assert parse("2024-01-02") == date(2024, 1, 2)
    assert parse("2024-01-02", is_end=True) == date(2024, 1, 2)


def test_invalid_iso_date_raises():
    with pytest.raises(ValueError):
        parse("2024-13-40")


# -- bare year / year-month snap to span edges --------------------------------


def test_bare_year_snaps_to_span_edges():
    assert parse("2024") == date(2024, 1, 1)
    assert parse("2024", is_end=True) == date(2024, 12, 31)


def test_year_month_snaps_to_span_edges():
    assert parse("2024-02") == date(2024, 2, 1)
    # February 2024 is a leap year -> 29 days.
    assert parse("2024-02", is_end=True) == date(2024, 2, 29)
    assert parse("2023-04", is_end=True) == date(2023, 4, 30)


def test_year_month_rejects_bad_month():
    with pytest.raises(ValueError):
        parse("2024-13")


# -- named single days --------------------------------------------------------


def test_today_yesterday_tomorrow():
    assert parse("today") == TODAY
    assert parse("now") == TODAY
    assert parse("yesterday") == date(2024, 3, 14)
    assert parse("tomorrow") == date(2024, 3, 16)


def test_case_and_whitespace_insensitive():
    assert parse("  Yesterday  ") == date(2024, 3, 14)


# -- relative point offsets (is_end has no effect) ----------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("1 day ago", date(2024, 3, 14)),
        ("10 days ago", date(2024, 3, 5)),
        ("a week ago", date(2024, 3, 8)),
        ("2 weeks ago", date(2024, 3, 1)),
        ("3 months ago", date(2023, 12, 15)),
        ("a month ago", date(2024, 2, 15)),
        ("1 year ago", date(2023, 3, 15)),
        ("an hour ago", None),  # unit not supported -> handled below
    ],
)
def test_relative_offsets(expr, expected):
    if expected is None:
        with pytest.raises(ValueError):
            parse(expr)
    else:
        assert parse(expr) == expected
        # A point offset ignores is_end.
        assert parse(expr, is_end=True) == expected


def test_month_offset_clamps_to_shorter_month():
    # One month before March 31 has no March-31 counterpart in February;
    # it clamps to the last valid day.
    assert parse_date_bound("1 month ago", today=date(2024, 3, 31)) == date(2024, 2, 29)


# -- period keywords snap to span edges ---------------------------------------


def test_this_and_last_month():
    assert parse("this month") == date(2024, 3, 1)
    assert parse("this month", is_end=True) == date(2024, 3, 31)
    assert parse("last month") == date(2024, 2, 1)
    assert parse("last month", is_end=True) == date(2024, 2, 29)


def test_last_month_crosses_year_boundary():
    assert parse_date_bound("last month", today=date(2024, 1, 10)) == date(2023, 12, 1)


def test_this_and_last_year():
    assert parse("this year") == date(2024, 1, 1)
    assert parse("this year", is_end=True) == date(2024, 12, 31)
    assert parse("last year") == date(2023, 1, 1)
    assert parse("last year", is_end=True) == date(2023, 12, 31)


def test_this_and_last_week():
    # TODAY is Friday 2024-03-15; that ISO week is Mon 3/11 .. Sun 3/17.
    assert parse("this week") == date(2024, 3, 11)
    assert parse("this week", is_end=True) == date(2024, 3, 17)
    assert parse("last week") == date(2024, 3, 4)
    assert parse("last week", is_end=True) == date(2024, 3, 10)


# -- errors -------------------------------------------------------------------


def test_unrecognized_expression_raises_with_hint():
    with pytest.raises(ValueError) as exc:
        parse("sometime next fortnight")
    # The message lists accepted forms so an agent can recover.
    assert "YYYY-MM-DD" in str(exc.value)


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse("   ")


# -- integration with the search choke point ----------------------------------


def test_coerce_date_delegates_and_honours_is_end():
    # _coerce_date is the single point both live and index search resolve
    # bounds through, so this is what every --start/--end actually calls.
    assert _coerce_date("2024") == date(2024, 1, 1)
    assert _coerce_date("2024", is_end=True) == date(2024, 12, 31)
    assert _coerce_date(None) is None
    assert _coerce_date(date(2022, 6, 1)) == date(2022, 6, 1)
