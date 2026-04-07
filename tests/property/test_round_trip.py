"""Hypothesis property-based round-trip tests.

The strategy generates random Markdown documents drawn from the
supported subset and asserts the canonical-form invariant:

* For Markdown ``m``: ``to_md(to_cfx(m))`` is a fixed point under one
  more pass through the pipeline. In other words,
  ``to_md(to_cfx(to_md(to_cfx(m)))) == to_md(to_cfx(m))``.

This guards against any normalization step that converges in more
than one pass — a class of bug that's almost impossible to catch by
hand.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

import cfxmark

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


word = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        max_codepoint=0x07F,
    ),
    min_size=1,
    max_size=8,
)


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
