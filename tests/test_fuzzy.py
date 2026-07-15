"""Offline tests for the deterministic fuzzy task-name matcher."""

from __future__ import annotations

from umbra_py.fuzzy import matching_tasks, task_matches


def test_substring_still_matches_in_both_modes():
    # The legacy substring behaviour is preserved with or without fuzzy.
    assert task_matches("centerfield", "Centerfield, Utah")
    assert task_matches("centerfield", "Centerfield, Utah", fuzzy=False)


def test_fuzzy_is_word_order_independent():
    assert task_matches("utah centerfield", "Centerfield, Utah")
    # Order-independence is a fuzzy-only widening; substring can't do it.
    assert not task_matches("utah centerfield", "Centerfield, Utah", fuzzy=False)


def test_fuzzy_ignores_punctuation_between_words():
    assert task_matches("centerfield utah", "Centerfield, Utah")
    assert not task_matches("centerfield utah", "Centerfield, Utah", fuzzy=False)


def test_fuzzy_tolerates_a_small_typo():
    assert task_matches("centrfield", "Centerfield, Utah")
    assert task_matches("centerfeld utah", "Centerfield, Utah")


def test_fuzzy_requires_every_query_token_to_match():
    # "utah" matches but "seattle" does not -> the whole query fails.
    assert not task_matches("seattle utah", "Centerfield, Utah")


def test_fuzzy_rejects_unrelated_words():
    # difflib ratio must not treat merely similar-looking words as matches.
    assert not task_matches("port", "Point Reyes")
    assert not task_matches("nowhere", "Centerfield, Utah")


def test_semantic_aliasing_is_out_of_scope():
    # Plain string similarity cannot (and should not pretend to) bridge this;
    # it belongs to the future embedding index, not the deterministic matcher.
    assert not task_matches("grain storage north dakota", "Beet Piler - ND")


def test_matching_tasks_filters_and_preserves_order():
    names = ["Centerfield, Utah", "Beet Piler - ND", "Provo, Utah"]
    assert matching_tasks("utah", names) == ["Centerfield, Utah", "Provo, Utah"]
    assert matching_tasks("utah provo", names) == ["Provo, Utah"]


def test_fuzzy_never_drops_a_substring_match():
    # Superset property: anything substring returns, fuzzy also returns.
    names = ["Centerfield, Utah", "Port of Long Beach", "Suez Canal"]
    for q in ("center", "long beach", "suez", "canal"):
        subset = matching_tasks(q, names, fuzzy=False)
        superset = matching_tasks(q, names, fuzzy=True)
        assert set(subset) <= set(superset)
