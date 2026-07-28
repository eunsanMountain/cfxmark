"""Boundary classification behind the asterisk fallback decision.

These tests pin the classification helpers directly. The observable behavior
is covered by ``tests/unit/test_cfx_to_md.py`` and the semantic property in
``tests/property/test_emphasis_flanking.py``; what is pinned here is *why*
those choices come out the way they do, because getting the character classes
wrong is the failure mode that produced the previous over-broad policy.

Compatibility target: mistletoe 1.5.1 / CommonMark 0.30.
"""

from __future__ import annotations

import pytest

from cfxmark.renderers.md import (
    _is_md_punct,
    _is_md_space,
    _needs_asterisk_fallback,
    _needs_strikethrough_fallback,
)

# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch", list("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"))
def test_ascii_punctuation_is_punctuation(ch: str) -> None:
    # CommonMark's ASCII punctuation set is explicit. Several members are
    # Unicode category S*, so a category-only test would miss them.
    assert _is_md_punct(ch)


@pytest.mark.parametrize("ch", ["」", "「", "、", "。", "…", "—", "·", "¿"])
def test_unicode_p_category_is_punctuation(ch: str) -> None:
    assert _is_md_punct(ch)


@pytest.mark.parametrize("ch", ["★", "°", "€", "🙂", "→", "±", "©"])
def test_non_ascii_symbols_are_not_punctuation(ch: str) -> None:
    # CommonMark 0.31.2 would count these as punctuation; the pinned parser
    # (0.30) does not. Treating them as punctuation would suppress a fallback
    # that is actually required.
    assert not _is_md_punct(ch)
    assert not _is_md_space(ch)


@pytest.mark.parametrize("ch", ["가", "a", "Z", "7", "漢", "あ"])
def test_word_characters_are_neither_punctuation_nor_space(ch: str) -> None:
    assert not _is_md_punct(ch)
    assert not _is_md_space(ch)


@pytest.mark.parametrize("ch", [" ", "\t", "\n", " ", " ", "　"])
def test_unicode_whitespace_including_zs(ch: str) -> None:
    assert _is_md_space(ch)


def test_empty_character_is_neither() -> None:
    assert not _is_md_punct("")
    assert not _is_md_space("")


# ---------------------------------------------------------------------------
# Asterisk predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inner", "prev_char", "next_char"),
    [
        ("볼드", "가", "이"),  # word inside, word outside
        ("bold", "x", "y"),
        ("볼드", "", ""),  # no outer characters at all
        ("(주의)", " ", " "),  # punctuation inside, whitespace outside
        ("(주의)", "", ""),  # punctuation inside, nothing outside
        ("(주의)", ".", ","),  # punctuation inside, punctuation outside
        ("★볼드★", "가", "이"),  # symbol inside is not punctuation
    ],
)
def test_asterisk_fallback_not_needed(inner: str, prev_char: str, next_char: str) -> None:
    assert not _needs_asterisk_fallback(inner, prev_char, next_char)


@pytest.mark.parametrize(
    ("inner", "prev_char", "next_char"),
    [
        ("볼드.", "", "이"),  # closer blocked by a word character
        (".볼드", "가", ""),  # opener blocked by a word character
        ("(주의)", "★", ""),  # opener blocked by a Unicode symbol
        ("(주의)", "", "🙂"),  # closer blocked by an emoji
        ("(값)", "€", ""),  # opener blocked by a currency symbol
        ("3.5.", "°", "°"),  # both sides blocked
    ],
)
def test_asterisk_fallback_needed(inner: str, prev_char: str, next_char: str) -> None:
    assert _needs_asterisk_fallback(inner, prev_char, next_char)


def test_asterisk_boundaries_are_evaluated_independently() -> None:
    # ``prev_char`` pairs with the first inside character and ``next_char``
    # with the last one. Crossing the pairs would make these two agree.
    assert _needs_asterisk_fallback(".볼드", "가", "")
    assert not _needs_asterisk_fallback("볼드.", "가", "")
    assert _needs_asterisk_fallback("볼드.", "", "가")
    assert not _needs_asterisk_fallback(".볼드", "", "가")


# ---------------------------------------------------------------------------
# Strikethrough keeps its own contract
# ---------------------------------------------------------------------------


def test_strikethrough_predicate_is_not_the_asterisk_predicate() -> None:
    # mistletoe matches ``~~`` with a regex rather than the flanking rules, so
    # the two predicates must stay separate. A word boundary is a fallback for
    # ``~~`` and not for ``**``.
    assert _needs_strikethrough_fallback("취소", "가", "이")
    assert not _needs_asterisk_fallback("취소", "가", "이")


@pytest.mark.parametrize(
    ("inner", "prev_char", "next_char", "expected"),
    [
        ("취소", "가", "이", True),
        ("취소", " ", " ", False),
        ("취소", "", "", False),
        ("취소", "★", "★", False),
    ],
)
def test_strikethrough_predicate_behavior_is_unchanged(
    inner: str, prev_char: str, next_char: str, expected: bool
) -> None:
    assert _needs_strikethrough_fallback(inner, prev_char, next_char) is expected
