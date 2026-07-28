"""Semantic oracle for the asterisk fallback decision.

The renderer has two ways to spell a Strong / Emphasis node: delimiters
(``**x**`` / ``*x*``) or raw HTML (``<strong>x</strong>``). Picking wrongly is
a silent data bug in either direction:

* delimiters where CommonMark will not re-parse them  -> the style is lost;
* HTML where delimiters would have worked             -> Confluence editor
  artifacts leak into ordinary Markdown.

This module generates boundary characters around a styled run and asserts both
directions at once:

1. whatever spelling the renderer chose, re-parsing it reproduces the source
   storage (no style is ever lost);
2. when the renderer chose HTML, the delimiter spelling really would have
   failed (no unnecessary HTML).

The generated alphabet deliberately includes Unicode Symbol characters
(``★``, ``°``, ``€``, emoji). They are neither whitespace nor punctuation, so
they block flanking exactly like letters do -- and a ``str.isalnum()``-based
predicate misses them. Property 2 fails for that predicate.

Compatibility target: the locked mistletoe 1.5.1 parser, whose punctuation
table follows CommonMark 0.30 (ASCII punctuation plus Unicode category
``P*``). Unicode ``S*`` membership alone does not make a character
punctuation here; upgrading mistletoe requires re-running this oracle.
"""

from __future__ import annotations

import html
import re

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

import cfxmark

# ---------------------------------------------------------------------------
# Boundary alphabet
# ---------------------------------------------------------------------------

WORD = ["a", "Z", "7", "가", "한", "漢", "あ"]
SPACE = [" ", "　"]  # ASCII space and ideographic space (Zs)
ASCII_PUNCT = [".", ",", "!", "(", ")", ":", ";", "?", "'", '"', "-", "/", "*", "_", "~"]
UNICODE_PUNCT = ["」", "「", "、", "…", "—", "·"]
SYMBOL = ["★", "°", "€", "🙂", "→", "±"]  # So / Sc / Sm — never punctuation here

# Inner boundary characters. Whitespace is excluded: leading / trailing space
# inside a style node is hoisted out of the delimiters by
# ``_split_boundary_whitespace``, which is a separate canonical transform and
# would make property 1 measure that instead of the flanking decision.
INNER_EDGE = WORD + ASCII_PUNCT + UNICODE_PUNCT + SYMBOL

# Outer boundary characters, including "nothing" (start / end of paragraph).
OUTER = [""] + WORD + SPACE + ASCII_PUNCT + UNICODE_PUNCT + SYMBOL

_RAW_STRONG = re.compile(r"<strong>(.*?)</strong>", re.DOTALL)
_RAW_EM = re.compile(r"<em>(.*?)</em>", re.DOTALL)


def _storage(tag: str, prev: str, inner: str, nxt: str) -> str:
    return f"<p>{html.escape(prev)}<{tag}>{html.escape(inner)}</{tag}>{html.escape(nxt)}</p>"


def _to_delimiters(markdown: str, tag: str) -> str:
    """Rewrite the renderer's raw-HTML choice into the delimiter spelling.

    Rewriting the rendered output (instead of re-assembling the Markdown from
    the raw characters) keeps every escape the renderer applied, so the two
    spellings differ only in the delimiter itself.
    """

    if tag == "strong":
        return _RAW_STRONG.sub(r"**\1**", markdown)
    return _RAW_EM.sub(r"*\1*", markdown)


def _styled_texts(storage: str, tag: str) -> list[str]:
    """Visible text of every ``tag`` run in a storage fragment.

    The oracle is the *style node*, not the whole document: a boundary space
    that the paragraph trims is a separate canonical transform and would
    otherwise mask what this property is measuring.
    """

    pattern = _RAW_STRONG if tag == "strong" else _RAW_EM
    return [html.unescape(match.group(1)) for match in pattern.finditer(storage)]


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    tag=st.sampled_from(["strong", "em"]),
    prev=st.sampled_from(OUTER),
    inner_first=st.sampled_from(INNER_EDGE),
    inner_last=st.sampled_from(INNER_EDGE),
    nxt=st.sampled_from(OUTER),
)
def test_asterisk_fallback_is_exactly_as_needed(
    tag: str,
    prev: str,
    inner_first: str,
    inner_last: str,
    nxt: str,
) -> None:
    inner = f"{inner_first}x{inner_last}"
    storage = _storage(tag, prev, inner, nxt)
    markdown = cfxmark.to_md(storage).markdown.rstrip("\n")

    # Property 1 — the chosen spelling never loses the style.
    assert _styled_texts(cfxmark.to_cfx(markdown).xhtml, tag) == [inner], (
        f"round trip lost the style: storage={storage!r} markdown={markdown!r}"
    )

    # Property 2 — HTML is used only where delimiters would actually fail.
    if f"<{tag}>" not in markdown:
        return
    delimited = _to_delimiters(markdown, tag)
    assert _styled_texts(cfxmark.to_cfx(delimited).xhtml, tag) != [inner], (
        f"unnecessary HTML fallback: storage={storage!r} markdown={markdown!r} "
        f"delimiters would have worked: {delimited!r}"
    )


def test_word_class_predicate_is_killed_by_this_oracle(monkeypatch: object) -> None:
    """Guard the strength of the alphabet above, not the renderer.

    The predicate replaced by this work classified boundaries with
    ``str.isalnum()``. It was wrong in both directions, and an oracle that
    cannot see both is not evidence of anything. Pin that here: if a future
    edit weakens the generated alphabet, this test still fails.
    """

    from cfxmark.renderers import md as renderer

    def word_class_predicate(inner: str, prev_char: str, next_char: str) -> bool:
        if not inner:
            return False
        if not inner[0].strip() or not inner[-1].strip():
            return False
        return bool(prev_char and prev_char.isalnum()) or bool(next_char and next_char.isalnum())

    monkeypatch.setattr(renderer, "_needs_asterisk_fallback", word_class_predicate)  # type: ignore[attr-defined]

    # Direction 1 — unnecessary HTML: word inside, word outside.
    storage = _storage("strong", "가", "볼드", "이다")
    markdown = cfxmark.to_md(storage).markdown.rstrip("\n")
    assert "<strong>" in markdown
    delimited = _to_delimiters(markdown, "strong")
    assert _styled_texts(cfxmark.to_cfx(delimited).xhtml, "strong") == ["볼드"], (
        "delimiters would have worked, so the HTML was unnecessary"
    )

    # Direction 2 — missed fallback: punctuation inside, Unicode symbol outside.
    symbol_storage = _storage("strong", "★", "(주의)", "★")
    symbol_markdown = cfxmark.to_md(symbol_storage).markdown.rstrip("\n")
    assert "<strong>" not in symbol_markdown
    assert _styled_texts(cfxmark.to_cfx(symbol_markdown).xhtml, "strong") != ["(주의)"], (
        "the word-class predicate emitted delimiters that silently drop the style"
    )
