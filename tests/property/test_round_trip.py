"""Hypothesis property-based round-trip tests.

The strategy generates random Markdown documents drawn from the
supported subset and asserts the canonical-form invariant:

* For Markdown ``m``: ``to_md(to_cfx(m))`` is a fixed point under one
  more pass through the pipeline. In other words,
  ``to_md(to_cfx(to_md(to_cfx(m)))) == to_md(to_cfx(m))``.

This guards against any normalization step that converges in more
than one pass — a class of bug that's almost impossible to catch by
hand.

The module runs the property twice: once over an ASCII-only corpus
(the original v0.1.0 coverage) and once over a CJK-inclusive corpus
with inline emphasis. The second run is the regression safety net
added in v0.1.4: the original strategy set ``max_codepoint=0x07F`` so
the Korean / CJK boundary fallback (``<strong>`` raw HTML) was never
exercised by property tests — the CJK-inclusive strategy closes that
gap.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

import cfxmark

# ---------------------------------------------------------------------------
# Building blocks — ASCII word
# ---------------------------------------------------------------------------


word = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        max_codepoint=0x07F,
    ),
    min_size=1,
    max_size=8,
)


# ---------------------------------------------------------------------------
# Building blocks — CJK-inclusive word
#
# The alphabet spans:
#   * ASCII letters + digits (U+0030..U+007A)
#   * Hangul Syllables       (U+AC00..U+D7A3)
#   * CJK Unified Ideographs (U+4E00..U+9FFF)
#
# Every glyph above is ``str.isalnum() == True``, so every boundary is a
# word-char boundary — the exact condition that makes ``to_md`` emit
# ``<strong>`` raw HTML instead of ``**``. The property holds: after a
# single round trip, the document converges to a fixed point regardless
# of which delimiter form the renderer chose.
# ---------------------------------------------------------------------------


_cjk_alphabet = st.one_of(
    # ASCII letters + digits.
    st.characters(whitelist_categories=("Lu", "Ll", "Nd"), max_codepoint=0x7F),
    # Hangul Syllables (U+AC00..U+D7A3) — category "Lo".
    st.characters(
        whitelist_categories=("Lo",),
        min_codepoint=0xAC00,
        max_codepoint=0xD7A3,
    ),
    # CJK Unified Ideographs (U+4E00..U+9FFF) — category "Lo".
    st.characters(
        whitelist_categories=("Lo",),
        min_codepoint=0x4E00,
        max_codepoint=0x9FFF,
    ),
)


word_cjk = st.text(alphabet=_cjk_alphabet, min_size=1, max_size=6)


@st.composite
def words(draw: st.DrawFn) -> str:
    return " ".join(draw(st.lists(word, min_size=1, max_size=6)))


@st.composite
def heading(draw: st.DrawFn) -> str:
    level = draw(st.integers(min_value=1, max_value=6))
    text = draw(words())
    return "#" * level + " " + text


@st.composite
def paragraph(draw: st.DrawFn) -> str:
    return draw(words())


@st.composite
def bullet_list(draw: st.DrawFn) -> str:
    items = draw(st.lists(words(), min_size=1, max_size=4))
    return "\n".join(f"- {it}" for it in items)


@st.composite
def code_block(draw: st.DrawFn) -> str:
    lang = draw(st.sampled_from(["", "python", "bash", "json"]))
    body = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
                max_codepoint=0x07F,
            ),
            min_size=1,
            max_size=40,
        )
    )
    body = body.replace("`", "")
    return f"```{lang}\n{body}\n```"


@st.composite
def block(draw: st.DrawFn) -> str:
    return draw(
        st.one_of(
            heading(),
            paragraph(),
            bullet_list(),
            code_block(),
        )
    )


@st.composite
def document(draw: st.DrawFn) -> str:
    blocks = draw(st.lists(block(), min_size=1, max_size=8))
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(document())
def test_round_trip_converges_after_one_pass(md_input: str) -> None:
    once = cfxmark.to_md(cfxmark.to_cfx(md_input).xhtml).markdown
    twice = cfxmark.to_md(cfxmark.to_cfx(once).xhtml).markdown
    assert once == twice


# ---------------------------------------------------------------------------
# CJK-inclusive strategy with inline emphasis (v0.1.4)
#
# The existing ``document`` strategy only emits plain text and does not
# exercise the Strong / Emphasis / Strikethrough AST path at all. The CJK
# variant below injects ``**bold**``, ``*italic*``, and ``~~strike~~`` runs
# inside paragraphs, headings, and list items — usually adjacent to
# non-ASCII word characters so the renderer's HTML fallback branch fires.
# ---------------------------------------------------------------------------


@st.composite
def words_cjk(draw: st.DrawFn) -> str:
    return " ".join(draw(st.lists(word_cjk, min_size=1, max_size=4)))


@st.composite
def emphasized_cjk(draw: st.DrawFn) -> str:
    """Return a short inline emphasis run drawn from CJK/ASCII words.

    The emphasis is rendered with either Markdown delimiters (``**X**``,
    ``*X*``, ``~~X~~``) — after the first pipeline pass the renderer may
    convert these to the raw HTML form, and the property still has to
    converge on the second pass.
    """

    inner = draw(word_cjk)
    marker = draw(st.sampled_from(["**", "*", "~~"]))
    return f"{marker}{inner}{marker}"


@st.composite
def paragraph_cjk(draw: st.DrawFn) -> str:
    """A paragraph that may contain inline CJK emphasis adjacent to
    CJK/ASCII word characters (the boundary case)."""

    parts: list[str] = []
    # Lead-in word so the opening delimiter is adjacent to a word char.
    if draw(st.booleans()):
        parts.append(draw(word_cjk))
    # The emphasis run, or just plain words.
    if draw(st.booleans()):
        parts.append(draw(emphasized_cjk()))
    else:
        parts.append(draw(word_cjk))
    # Trailing word so the closing delimiter is adjacent to a word char.
    if draw(st.booleans()):
        parts.append(draw(word_cjk))
    body = "".join(parts)
    if not body:
        body = draw(word_cjk)
    return body


@st.composite
def heading_cjk(draw: st.DrawFn) -> str:
    level = draw(st.integers(min_value=1, max_value=6))
    text = draw(paragraph_cjk())
    return "#" * level + " " + text


@st.composite
def bullet_list_cjk(draw: st.DrawFn) -> str:
    items = draw(st.lists(paragraph_cjk(), min_size=1, max_size=4))
    return "\n".join(f"- {it}" for it in items)


@st.composite
def block_cjk(draw: st.DrawFn) -> str:
    return draw(
        st.one_of(
            heading_cjk(),
            paragraph_cjk(),
            bullet_list_cjk(),
        )
    )


@st.composite
def document_cjk(draw: st.DrawFn) -> str:
    blocks = draw(st.lists(block_cjk(), min_size=1, max_size=6))
    return "\n\n".join(blocks) + "\n"


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(document_cjk())
def test_round_trip_converges_after_one_pass_cjk(md_input: str) -> None:
    """CJK-inclusive one-pass convergence property.

    The property is identical to :func:`test_round_trip_converges_after_one_pass`
    but draws from a CJK-inclusive alphabet with inline emphasis. On the
    first pass, any ``**X**`` whose outer chars are word characters is
    rewritten to ``<strong>X</strong>`` (the HTML fallback); on the
    second pass the result must equal the first.
    """

    once = cfxmark.to_md(cfxmark.to_cfx(md_input).xhtml).markdown
    twice = cfxmark.to_md(cfxmark.to_cfx(once).xhtml).markdown
    assert once == twice


# ---------------------------------------------------------------------------
# Jira wiki property tests (v0.2.0)
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402 — local import to avoid polluting module-level namespace


@st.composite
def document_simple_text(draw: st.DrawFn) -> str:
    """Documents with only plain ASCII words — no special characters.

    Used for text-preservation checks: every word that goes in must come
    out (modulo wiki escaping, but plain alnum words need no escaping).
    """
    blocks = draw(st.lists(paragraph(), min_size=1, max_size=4))
    return "\n\n".join(blocks) + "\n"


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(document_simple_text())
def test_jira_wiki_text_preservation(md_input: str) -> None:
    """Every plain-text word from the input appears in the Jira wiki output.

    We generate documents containing only alphanumeric words (no special
    characters) so that wiki escaping does not transform any token, and
    verify that all words are present in the rendered output.
    """
    result = cfxmark.to_jira_wiki(md_input)
    assert result.jira_wiki is not None
    wiki = result.jira_wiki
    for token in md_input.split():
        # Strip markdown heading markers that are not content.
        clean = token.lstrip("#").strip()
        if clean:
            assert clean in wiki, f"token {clean!r} missing from wiki output"


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(document_cjk())
def test_jira_wiki_no_crash_cjk(md_input: str) -> None:
    """to_jira_wiki must never raise on CJK-inclusive documents."""
    result = cfxmark.to_jira_wiki(md_input)
    # We only assert it didn't crash and produced something.
    assert result.jira_wiki is not None


@st.composite
def document_headings_only(draw: st.DrawFn) -> str:
    """A document consisting entirely of headings at varied levels."""
    headings = draw(st.lists(heading(), min_size=1, max_size=8))
    return "\n\n".join(headings) + "\n"


_HEADING_LINE_RE = _re.compile(r"^h\d\. (.+)$", _re.MULTILINE)
_MD_HEADING_RE = _re.compile(r"^(#{1,6}) (.+)$", _re.MULTILINE)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(document_headings_only())
def test_jira_wiki_heading_monotonicity(md_input: str) -> None:
    """The order of heading texts is preserved after promotion.

    The Jira wiki renderer may *promote* heading levels (H3→H2, H4→H3,
    …) but must never reorder or drop headings.
    """
    result = cfxmark.to_jira_wiki(md_input)
    assert result.jira_wiki is not None

    # Extract heading text labels from the Markdown input (in order).
    input_texts = [m.group(2).strip() for m in _MD_HEADING_RE.finditer(md_input)]
    # Extract heading text labels from the Jira wiki output (in order).
    output_texts = [m.group(1).strip() for m in _HEADING_LINE_RE.finditer(result.jira_wiki)]

    assert input_texts == output_texts, (
        f"Heading order changed:\n  input:  {input_texts}\n  output: {output_texts}"
    )
