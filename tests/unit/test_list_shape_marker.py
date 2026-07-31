"""What the list-shape marker may and may not do to a document.

The marker exists for shapes storage holds and CommonMark cannot spell: a list
whose items carry block content, and a one-item loose list. A pull writes it; an
author never does.

Its first implementation was held as converter state rather than bound to a list,
and a reviewer measured two consequences. A marker followed immediately by prose
deleted the prose -- silently, with `push_safe` still true and no diagnostic --
because the marker matched the paragraph's first line and the whole paragraph was
dropped. And a marker with no list after it stayed armed, applying itself to
whatever list came next, past paragraphs and headings.

So the tests below are mostly negative. The positive case is one line; the rest
say what the marker must not reach.
"""

from __future__ import annotations

import cfxmark

# `editable` is the profile the managed workflow uses. This said "markdown-first",
# which is not a `ConversionOptions` profile at all -- `Literal["legacy", "editable",
# "readable"]` -- and is not validated at runtime, so every assertion in this file was
# silently exercising `legacy` while the file name claimed the managed path.
STORAGE = cfxmark.ConversionOptions(profile="editable")
LOOSE = "<!-- cfxmark:list loose -->"
TIGHT = "<!-- cfxmark:list tight -->"


def _storage(markdown: str) -> str:
    return cfxmark.to_cfx_artifact(markdown, options=STORAGE).xhtml


def _artifact(markdown: str):
    return cfxmark.to_cfx_artifact(markdown, options=STORAGE)


# --------------------------------------------------------------------------
# It reaches the next list, and nothing else
# --------------------------------------------------------------------------


def test_a_marker_applies_to_the_list_immediately_after_it() -> None:
    assert _storage(f"{LOOSE}\n- a\n") == "<ul><li><p>a</p></li></ul>"


def test_a_marker_does_not_reach_past_a_paragraph() -> None:
    """It used to. The marker was converter state with no scope, so it applied
    itself to a list several blocks later that no pull had said anything about."""

    assert _storage(f"{LOOSE}\n\nIntervening paragraph\n\n- a\n") == ("<p>Intervening paragraph</p><ul><li>a</li></ul>")


def test_a_marker_does_not_reach_past_a_heading() -> None:
    assert _storage(f"{LOOSE}\n\n# Heading\n\n- a\n") == "<h1>Heading</h1><ul><li>a</li></ul>"


def test_a_marker_with_no_list_after_it_loses_nothing() -> None:
    assert _storage(f"{LOOSE}\n\nJust a paragraph\n") == "<p>Just a paragraph</p>"


def test_two_markers_before_one_list_apply_neither() -> None:
    """A contradiction is not resolved by picking one of them."""

    assert _storage(f"{LOOSE}\n\n{TIGHT}\n\n- a\n") == "<ul><li>a</li></ul>"


# --------------------------------------------------------------------------
# It never costs content
# --------------------------------------------------------------------------


def test_a_marker_followed_immediately_by_prose_keeps_the_prose() -> None:
    """The defect that mattered most, and the reason the whole paragraph has to
    match rather than its first line.

    Without a blank line the two are a single paragraph to CommonMark. Treating
    that paragraph as a marker and dropping it deleted the author's sentence, and
    reported `push_safe=True` with no diagnostic while doing it."""

    artifact = _artifact(f"{LOOSE}\nIntervening paragraph\n\n- a\n")
    assert "Intervening paragraph" in artifact.xhtml
    # And it does not pass silently: the stray comment is a reported loss.
    assert artifact.push_safe is False


def test_the_mixed_marker_followed_immediately_by_prose_keeps_the_prose() -> None:
    """The same defect as the test above, on the third marker, which did not inherit
    the guard.

    `_list_shape_marker` grew two guards for exactly this -- the token must be a
    paragraph and it must have one child -- and its docstring records why: matching
    only the first source line deleted an author's sentence silently, with
    `push_safe` still true. `_mixed_list_items` was added later on a guard-free path,
    reading the first source line and discarding the whole token, so a marker written
    at the top of a pulled file plus one typed sentence lost the sentence.

    Not a hypothetical edit: the pull writes the marker as the file's first line, so
    typing a sentence above the list is enough.
    """

    artifact = _artifact("<!-- cfxmark:list loose=0 -->\nIntervening paragraph\n\n- a\n- b\n")

    assert "Intervening paragraph" in artifact.xhtml
    assert artifact.push_safe is False


def test_the_mixed_marker_does_not_reach_past_a_heading() -> None:
    """A setext heading under the marker is not a paragraph at all, which is what the
    missing type guard let through -- the heading was swallowed with the token."""

    artifact = _artifact("<!-- cfxmark:list loose=0 -->\nHeading\n=======\n\n- a\n- b\n")

    assert "Heading" in artifact.xhtml


def test_a_marker_inside_a_blockquote_is_read_back() -> None:
    """A marker resolved by indexing the outer source line cannot work in a container:
    the raw line carries the `> ` prefix, so it never matched. The marker was then
    dropped as an ordinary comment, the item's `<p>` wrapper was lost, and a stray
    `<p/>` appeared where the marker had been -- new content in a page nobody edited.

    The child token's own text is authoritative at any depth, which is what
    `_is_blank_paragraph_marker` already relied on.
    """

    storage = "<blockquote><ul><li><p>Keep</p></li></ul></blockquote>"
    markdown = cfxmark.to_md_artifact(storage, options=STORAGE).markdown
    back = cfxmark.to_cfx_artifact(markdown, options=STORAGE)

    assert LOOSE in markdown
    assert back.xhtml == storage
    assert [d.code for d in back.diagnostics] == []


def test_a_marker_inside_a_code_block_is_text() -> None:
    """Not an instruction. A document *about* this format has to be publishable."""

    artifact = _artifact(f"```\n{LOOSE}\n```\n\n- a\n")
    assert LOOSE in artifact.xhtml
    assert "<ul><li>a</li></ul>" in artifact.xhtml


def test_a_marker_inside_a_sentence_is_text() -> None:
    """One child among several, so it is content by the same rule that keeps the
    prose above."""

    assert "<ul><li>a</li></ul>" in _storage(f"Text with {LOOSE} inside it.\n\n- a\n")


# --------------------------------------------------------------------------
# CommonMark decides when nothing says otherwise
# --------------------------------------------------------------------------


def test_an_unmarked_loose_list_is_loose() -> None:
    """CommonMark 0.30: blank lines between items make a list loose, and a loose
    list wraps each item's content in a paragraph. Hand-written Markdown gets
    this answer with no marker involved."""

    assert _storage("- a\n\n- b\n") == "<ul><li><p>a</p></li><li><p>b</p></li></ul>"


def test_an_unmarked_tight_list_is_tight() -> None:
    assert _storage("- a\n- b\n") == "<ul><li>a</li><li>b</li></ul>"


def test_a_tight_marker_overrides_the_list_it_names() -> None:
    """Only the list it names. This is how a pull records a shape CommonMark
    would otherwise read back differently."""

    assert _storage(f"{TIGHT}\n- a\n\n- b\n") == "<ul><li>a</li><li>b</li></ul>"


def test_a_marker_applies_at_the_depth_it_was_written() -> None:
    """The nested list keeps CommonMark's own answer; only the outer list is
    named. Written as a round trip because that is the shape a pull produces."""

    storage = "<ul><li><p>outer</p><ul><li>inner</li></ul></li></ul>"
    markdown = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable")).markdown
    assert "<li>inner</li>" in _storage(markdown)


def test_a_one_item_loose_list_nested_in_a_tight_one_is_a_known_gap() -> None:
    """Stated as a test so the limit is a fact rather than a claim.

    The renderer writes a marker for the inner list and the parser does not pick
    it up at that depth, so the shape does not come back. It is safe -- the
    comparator reports `unknown_blocked` and nothing publishes -- but "the marker
    applies at every depth" is not true, and a suite that only tested the depths
    that work would let that sentence stand.

    Both halves are asserted: the marker really is written, and the round trip
    really does not restore it. If either changes, this test is the place to
    notice.
    """

    from cfxmark.compatibility import assess_markdown_compatibility

    storage = "<ul><li>outer<ul><li><p>inner</p></li></ul></li></ul>"
    markdown = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable")).markdown

    assert LOOSE in markdown
    assert _storage(markdown) != storage
    assert assess_markdown_compatibility(storage, options=STORAGE).classification == "unknown_blocked"


def test_a_list_with_mixed_item_shapes_round_trips():
    """C3's converter gap, localised to one shape.

    `List.loose` is a per-*list* flag -- right for CommonMark, where tight/loose is a
    list-level rendering property -- while storage carries the shape per *item*. So a list
    mixing `<li>alpha</li>` with `<li><p>beta</p></li>` flattened to tight and the loose
    item lost its wrapper. Uniform lists in either form already survived, which is why this
    went unnoticed until a live corpus document graded `converter_fix_required` on it.

    Not a named loss: Markdown *can* hold a paragraph in a list item -- that is what a loose
    list is -- so calling it a loss would ask someone to consent to a defect. Live
    2026-07-29: the server stores `<li><p>alpha</p></li>` exactly as sent, so the unwrap is
    ours alone.
    """

    import cfxmark

    options = cfxmark.ConversionOptions(profile="editable")
    for storage in (
        "<ul><li>alpha</li><li><p>beta</p></li></ul>",
        "<ul><li><p>alpha</p></li><li>beta</li></ul>",
        "<ul><li>a</li><li><p>b</p></li><li>c</li></ul>",
        "<ol><li>a</li><li><p>b</p></li></ol>",
        (
            "<ol><li>a</li><li><p>b</p>"
            '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[x]]></ac:plain-text-body>'
            "</ac:structured-macro></li><li>c</li></ol>"
        ),
    ):
        markdown = cfxmark.to_md_artifact(storage, options=options).markdown
        back = cfxmark.to_cfx_artifact(markdown, options=options).xhtml
        assert back == storage, f"{storage}\n  md:   {markdown!r}\n  back: {back!r}"


def test_uniform_lists_are_unaffected():
    """The discriminating half: whatever fixes the mixed case must not disturb these."""

    import cfxmark

    options = cfxmark.ConversionOptions(profile="editable")
    for storage in (
        "<ul><li>alpha</li></ul>",
        "<ul><li><p>alpha</p></li></ul>",
        "<ul><li><p>a</p></li><li><p>b</p></li></ul>",
        "<ul><li><p>a</p><p>b</p></li></ul>",
        "<ul><li>a</li><li>b</li></ul>",
    ):
        markdown = cfxmark.to_md_artifact(storage, options=options).markdown
        back = cfxmark.to_cfx_artifact(markdown, options=options).xhtml
        assert back == storage, f"{storage}\n  md:   {markdown!r}\n  back: {back!r}"


def test_a_malformed_mixed_marker_is_not_a_marker():
    """An empty or unparseable index list names no item, so it is prose, not an instruction.

    Returning `()` for `loose=` with nothing after it would consume the line as a marker and
    make the list tight — quietly, and on a document whose author wrote that text themselves.
    `None` lets it stay ordinary content, which is what an unrecognised comment is.
    """

    from cfxmark.ast import mixed_list_marker, parse_mixed_list_marker

    assert parse_mixed_list_marker("<!-- cfxmark:list loose= -->") is None
    assert parse_mixed_list_marker("<!-- cfxmark:list loose=x -->") is None
    assert parse_mixed_list_marker("<!-- cfxmark:list loose=1,two -->") is None
    assert parse_mixed_list_marker("<!-- cfxmark:list loose -->") is None
    assert parse_mixed_list_marker("just prose") is None
    # And the round trip of the spelling it does accept.
    assert parse_mixed_list_marker(mixed_list_marker((0, 2))) == (0, 2)
