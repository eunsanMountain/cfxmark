"""Unit tests for the Jira wiki markup parser (``from_jira_wiki``).

The parser is explicitly **lossy** — see
:mod:`cfxmark.parsers.jira_wiki` for the contract. These tests pin
the behaviour of each supported construct (block + inline) plus a
suite of adversarial fixtures that caught bugs during development,
and finally run every real-world fixture in
``tests/fixtures/jira_wiki`` through the parser to make sure no
regression can sneak in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cfxmark.ast import (
    BlockQuote,
    CodeBlock,
    DirectiveMacro,
    Document,
    Emphasis,
    Heading,
    HorizontalRule,
    Image,
    InlineCode,
    Link,
    List,
    ListItem,
    ListType,
    Paragraph,
    Strikethrough,
    Strong,
    Table,
    Text,
)
from cfxmark.jira import from_jira_wiki
from cfxmark.parsers.jira_wiki import parse_jira_wiki


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse(source: str) -> Document:
    document, _warnings, _attachments = parse_jira_wiki(source)
    return document


def parse_full(source: str):
    return parse_jira_wiki(source)


def first_block(source: str):
    doc = parse(source)
    assert doc.children, "expected at least one block"
    return doc.children[0]


def only_block(source: str):
    doc = parse(source)
    assert len(doc.children) == 1, f"expected exactly one block, got {len(doc.children)}"
    return doc.children[0]


# ---------------------------------------------------------------------------
# Headings (D1c — 1:1 mapping)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_heading_levels_are_identity(level: int) -> None:
    block = only_block(f"h{level}. Title")
    assert isinstance(block, Heading)
    assert block.level == level
    assert block.children == (Text(content="Title"),)


def test_heading_directly_after_paragraph_no_blank_line() -> None:
    """Jira authors frequently stick a heading on the line right after
    the previous paragraph (no separating blank line). The parser must
    recognise this — real Jira content has this pattern frequently
    whenever an author omits the trailing blank line between a
    paragraph and the next section."""
    doc = parse("paragraph line\nh3. Section")
    assert len(doc.children) == 2
    assert isinstance(doc.children[0], Paragraph)
    assert isinstance(doc.children[1], Heading)
    assert doc.children[1].level == 3


def test_heading_empty_content() -> None:
    """``h2.`` with nothing after it (an empty-heading separator used
    as a visual divider in real issue bodies) must not crash."""
    doc = parse("h2. \nbody")
    # Empty heading has level=2, children=empty. Followed by paragraph.
    assert any(
        isinstance(b, Heading) and b.level == 2 for b in doc.children
    )


# ---------------------------------------------------------------------------
# Inline — emphasis
# ---------------------------------------------------------------------------


def test_bold_single_asterisks() -> None:
    block = only_block("*bold text*")
    assert isinstance(block, Paragraph)
    assert block.children == (Strong(children=(Text(content="bold text"),)),)


def test_italic_single_underscores() -> None:
    block = only_block("_italic text_")
    assert isinstance(block, Paragraph)
    assert block.children == (Emphasis(children=(Text(content="italic text"),)),)


def test_strikethrough_single_dashes() -> None:
    block = only_block("word -struck out- word")
    assert isinstance(block, Paragraph)
    assert Strikethrough(children=(Text(content="struck out"),)) in block.children


def test_inline_code_double_braces() -> None:
    block = only_block("run {{x = 1}} here")
    assert isinstance(block, Paragraph)
    assert InlineCode(content="x = 1") in block.children


# ---------------------------------------------------------------------------
# Inline — boundary-aware parsing (D3a)
# ---------------------------------------------------------------------------


def test_tilde_inside_word_is_plain_text() -> None:
    """``v1.0~v2.0`` must NOT be parsed as a subscript. Both sides
    of the ``~`` are word characters, so the opener boundary check
    fails. This pins the boundary-aware parsing decision (D3a) that
    keeps real-world version-range text intact."""
    block = only_block("v1.0~v2.0")
    assert isinstance(block, Paragraph)
    # The run should collapse to a single Text node with the literal
    # characters preserved.
    text = "".join(
        n.content for n in block.children if isinstance(n, Text)
    )
    assert "v1.0~v2.0" in text


def test_unclosed_tilde_is_plain_text() -> None:
    """``~next week`` at the start of a paragraph with no closing
    ``~`` on the same line must stay as plain text."""
    block = only_block("~next week: first milestone")
    assert isinstance(block, Paragraph)
    text = "".join(
        n.content for n in block.children if isinstance(n, Text)
    )
    assert text.startswith("~next week")


def test_closed_tilde_drops_markers_with_warning() -> None:
    """A properly boundary-matched ``~sub~`` has no AST equivalent
    (cfxmark has no subscript node), so the markers are dropped and a
    lossy warning is recorded."""
    document, warnings, _ = parse_full("word ~sub~ here")
    para = document.children[0]
    assert isinstance(para, Paragraph)
    text = "".join(
        n.content for n in para.children if isinstance(n, Text)
    )
    assert "sub" in text
    assert "~sub~" not in text
    assert any("dropped" in w for w in warnings)


def test_bold_inside_word_is_plain_text() -> None:
    """``2*3*4`` is literal — the leading ``*`` fails the left-
    boundary check (previous char is alphanumeric)."""
    block = only_block("2*3*4")
    assert isinstance(block, Paragraph)
    text = "".join(
        n.content for n in block.children if isinstance(n, Text)
    )
    assert text == "2*3*4"
    assert not any(isinstance(n, Strong) for n in block.children)


def test_asterisk_at_word_boundary_is_bold() -> None:
    block = only_block("word *bold* word")
    assert isinstance(block, Paragraph)
    assert any(
        isinstance(n, Strong)
        and n.children == (Text(content="bold"),)
        for n in block.children
    )


def test_emphasis_content_must_not_start_with_whitespace() -> None:
    """``* text *`` is literal — the content ``text`` has a leading
    space, so the opener is rejected."""
    block = only_block("see * text * here")
    assert isinstance(block, Paragraph)
    assert not any(isinstance(n, Strong) for n in block.children)


# ---------------------------------------------------------------------------
# Inline — escape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escaped,expected",
    [
        (r"\*", "*"),
        (r"\_", "_"),
        (r"\{", "{"),
        (r"\[", "["),
        (r"\|", "|"),
        (r"\\", "\\"),
        (r"\~", "~"),
        (r"\+", "+"),
        (r"\^", "^"),
        (r"\-", "-"),
    ],
)
def test_escape_produces_literal(escaped: str, expected: str) -> None:
    block = only_block(f"before {escaped} after")
    assert isinstance(block, Paragraph)
    text = "".join(
        n.content for n in block.children if isinstance(n, Text)
    )
    assert expected in text


# ---------------------------------------------------------------------------
# Inline — links
# ---------------------------------------------------------------------------


def test_labelled_link() -> None:
    block = only_block("[label|https://example.com]")
    assert isinstance(block, Paragraph)
    (link,) = block.children
    assert isinstance(link, Link)
    assert link.url == "https://example.com"
    assert link.children == (Text(content="label"),)


def test_bare_url_link() -> None:
    block = only_block("[https://example.com]")
    assert isinstance(block, Paragraph)
    (link,) = block.children
    assert isinstance(link, Link)
    assert link.url == "https://example.com"
    assert link.children == ()


def test_link_label_with_nested_brackets() -> None:
    """Real Jira content frequently has labels like
    ``[EXAMPLE-100 [draft] extended mode spec|url]`` — the label
    contains a literal ``[...]`` that MUST NOT be re-interpreted as
    another link. Parsing labels with ``allow_link=False`` guarantees
    this."""
    block = only_block(
        "[EXAMPLE-100 [draft] extended mode spec|https://example.com/browse/EXAMPLE-100]"
    )
    (link,) = block.children
    assert isinstance(link, Link)
    assert link.url == "https://example.com/browse/EXAMPLE-100"
    # The nested brackets must appear as literal text nodes inside the
    # label — not as a Link child.
    label_text = "".join(
        n.content for n in link.children if isinstance(n, Text)
    )
    assert "[draft]" in label_text
    assert not any(isinstance(n, Link) for n in link.children)


def test_attachment_link_image_extension() -> None:
    """``[^diagram.png]`` → :class:`Image` because ``.png`` is in
    ``_IMAGE_EXTENSIONS``."""
    document, _, attachments = parse_full("See [^diagram.png] for details")
    para = document.children[0]
    assert isinstance(para, Paragraph)
    assert any(isinstance(n, Image) and n.src == "diagram.png" for n in para.children)
    assert "diagram.png" in attachments


def test_attachment_link_document_extension() -> None:
    """``[^notes.msg]`` → :class:`Link` with ``attachment:`` URL
    scheme for non-image files like ``.msg`` / ``.docx`` / ``.txt``."""
    document, _, attachments = parse_full("See [^notes.msg] for details")
    para = document.children[0]
    assert isinstance(para, Paragraph)
    link = next(n for n in para.children if isinstance(n, Link))
    assert link.url == "attachment:notes.msg"
    assert "notes.msg" in attachments


def test_user_mention_dropped_with_warning() -> None:
    document, warnings, _ = parse_full("cc [~johndoe] please review")
    para = document.children[0]
    assert isinstance(para, Paragraph)
    assert not any(isinstance(n, Link) for n in para.children)
    assert any("mention" in w for w in warnings)


# ---------------------------------------------------------------------------
# Inline — images
# ---------------------------------------------------------------------------


def test_image_bare() -> None:
    document, _, attachments = parse_full("!image-1.png!")
    para = document.children[0]
    (img,) = para.children
    assert isinstance(img, Image)
    assert img.src == "image-1.png"
    assert img.alt == ""
    assert "image-1.png" in attachments


def test_image_with_alt_and_dimensions() -> None:
    document, _, _ = parse_full("!chart.jpg|alt=Chart,width=640,height=480!")
    para = document.children[0]
    (img,) = para.children
    assert isinstance(img, Image)
    assert img.src == "chart.jpg"
    assert img.alt == "Chart"
    assert img.width == 640
    assert img.height == 480


def test_image_must_have_extension() -> None:
    """Plain text ``hello!world!`` must not be parsed as an image —
    the "filename" ``hello`` has no dot."""
    block = only_block("hello !world! there")
    assert isinstance(block, Paragraph)
    assert not any(isinstance(n, Image) for n in block.children)


# ---------------------------------------------------------------------------
# Inline — color macro
# ---------------------------------------------------------------------------


def test_color_macro_keeps_content_drops_style() -> None:
    document, warnings, _ = parse_full(
        "Some {color:#de350b}red text{color} here"
    )
    para = document.children[0]
    text = "".join(
        n.content for n in para.children if isinstance(n, Text)
    )
    assert "red text" in text
    assert "de350b" not in text
    assert any("color" in w for w in warnings)


# ---------------------------------------------------------------------------
# Block — lists
# ---------------------------------------------------------------------------


def test_bullet_list_basic() -> None:
    block = only_block("* first\n* second\n* third")
    assert isinstance(block, List)
    assert block.list_type == ListType.BULLET
    assert len(block.items) == 3


def test_ordered_list_basic() -> None:
    block = only_block("# first\n# second")
    assert isinstance(block, List)
    assert block.list_type == ListType.ORDERED
    assert len(block.items) == 2


def test_dash_list_treated_as_bullet() -> None:
    block = only_block("- first\n- second")
    assert isinstance(block, List)
    assert block.list_type == ListType.BULLET
    assert len(block.items) == 2


def test_list_with_leading_whitespace() -> None:
    """Real Jira content commonly uses ``\\s*\\*`` for every bullet
    (one-space indent before the marker) — leading whitespace on the
    marker line must not break detection."""
    block = only_block(" * first\n ** nested\n * second")
    assert isinstance(block, List)
    assert len(block.items) == 2  # two top-level items
    # First item has a nested list child
    first = block.items[0]
    assert any(isinstance(c, List) for c in first.children)


def test_nested_list_bullet_under_bullet() -> None:
    doc = parse("* outer\n** inner")
    outer_list = doc.children[0]
    assert isinstance(outer_list, List)
    assert outer_list.list_type == ListType.BULLET
    outer_item = outer_list.items[0]
    nested = next(c for c in outer_item.children if isinstance(c, List))
    assert nested.list_type == ListType.BULLET
    assert len(nested.items) == 1


def test_nested_list_three_levels() -> None:
    source = "* a\n** b\n*** c"
    doc = parse(source)
    outer = doc.children[0]
    assert isinstance(outer, List)
    level1_item = outer.items[0]
    level2_list = next(c for c in level1_item.children if isinstance(c, List))
    level2_item = level2_list.items[0]
    level3_list = next(c for c in level2_item.children if isinstance(c, List))
    level3_para = level3_list.items[0].children[0]
    assert isinstance(level3_para, Paragraph)
    assert level3_para.children == (Text(content="c"),)


# ---------------------------------------------------------------------------
# Block — tables
# ---------------------------------------------------------------------------


def test_simple_table() -> None:
    source = "|a|b|c|\n|d|e|f|"
    block = only_block(source)
    assert isinstance(block, Table)
    assert block.header is None
    assert len(block.body) == 2
    assert len(block.body[0].cells) == 3


def test_table_with_header_row() -> None:
    source = "||h1||h2||\n|a|b|"
    block = only_block(source)
    assert isinstance(block, Table)
    assert block.header is not None
    assert len(block.header.cells) == 2
    assert len(block.body) == 1


def test_table_multi_line_cell() -> None:
    """Real Jira content has cells that span multiple source lines
    (a header line followed by a clarifier line that still belongs to
    the same cell). The parser must join the lines AND re-split by
    ``|`` so that the pipe character on the continuation line is
    recognised as a cell separator."""
    source = "|Mode Alpha\n(OR)| body text |"
    block = only_block(source)
    assert isinstance(block, Table)
    assert len(block.body) == 1
    row = block.body[0]
    assert len(row.cells) == 2
    # First cell contains the newline + ``(OR)``.
    cell_text = "".join(
        n.content for n in row.cells[0].children if isinstance(n, Text)
    )
    assert "Mode Alpha" in cell_text
    assert "(OR)" in cell_text


def test_table_stops_at_heading() -> None:
    """Real Jira authors often put a heading immediately after the
    last table row with no blank separator. The parser must NOT
    absorb the heading into the last cell."""
    source = "|a|b|\n|c|d|\nh3. Section"
    doc = parse(source)
    assert len(doc.children) == 2
    assert isinstance(doc.children[0], Table)
    assert isinstance(doc.children[1], Heading)
    assert doc.children[1].level == 3


def test_table_escaped_pipe_in_cell() -> None:
    """``\\|\\|`` inside a cell must stay as literal text instead of
    being read as an additional (empty) cell separator."""
    source = "|a \\|\\| b|c|"
    block = only_block(source)
    assert isinstance(block, Table)
    row = block.body[0]
    assert len(row.cells) == 2
    cell_text = "".join(
        n.content for n in row.cells[0].children if isinstance(n, Text)
    )
    assert "||" in cell_text


# ---------------------------------------------------------------------------
# Block — paired macros
# ---------------------------------------------------------------------------


def test_code_macro_no_language() -> None:
    source = "{code}\nx = 1\n{code}"
    block = only_block(source)
    assert isinstance(block, CodeBlock)
    assert block.content == "x = 1"
    assert block.language is None


def test_code_macro_with_language() -> None:
    source = "{code:python}\nprint(1)\n{code}"
    block = only_block(source)
    assert isinstance(block, CodeBlock)
    assert block.content == "print(1)"
    assert block.language == "python"


def test_code_macro_with_parametrised_language() -> None:
    source = "{code:language=java|title=Foo}\nSystem.out.println();\n{code}"
    block = only_block(source)
    assert isinstance(block, CodeBlock)
    assert block.language == "java"


def test_noformat_macro() -> None:
    source = "{noformat}\nliteral *text*\n{noformat}"
    block = only_block(source)
    assert isinstance(block, CodeBlock)
    assert block.content == "literal *text*"
    assert block.language is None


def test_quote_macro() -> None:
    source = "{quote}\nquoted paragraph\n{quote}"
    block = only_block(source)
    assert isinstance(block, BlockQuote)
    inner = block.children[0]
    assert isinstance(inner, Paragraph)


def test_bq_line_blockquote() -> None:
    block = only_block("bq. single line quote")
    assert isinstance(block, BlockQuote)


def test_info_admonition() -> None:
    source = "{info:title=Heads up}\nbody text\n{info}"
    block = only_block(source)
    assert isinstance(block, DirectiveMacro)
    assert block.name == "info"
    assert ("title", "Heads up") in block.parameters


def test_panel_mapped_to_note_with_warning() -> None:
    """D4: ``{panel}`` has no native cfxmark admonition, so it is
    mapped to ``{note}`` with a lossy warning."""
    document, warnings, _ = parse_full(
        "{panel:title=Summary}\nbody\n{panel}"
    )
    block = document.children[0]
    assert isinstance(block, DirectiveMacro)
    assert block.name == "note"
    assert any("panel" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Horizontal rule
# ---------------------------------------------------------------------------


def test_horizontal_rule() -> None:
    block = only_block("----")
    assert isinstance(block, HorizontalRule)


def test_horizontal_rule_with_trailing_whitespace() -> None:
    block = only_block("----   ")
    assert isinstance(block, HorizontalRule)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_document() -> None:
    doc, warnings, attachments = parse_full("")
    assert doc == Document(children=())
    assert warnings == []
    assert attachments == ()


def test_none_input_via_empty_string() -> None:
    # The API accepts empty string; None is not valid per type hints.
    doc, _, _ = parse_full("")
    assert isinstance(doc, Document)


def test_unclosed_code_macro_falls_back_to_paragraph() -> None:
    """An unterminated ``{code}`` block must not swallow the rest of
    the document — the opener falls through to paragraph handling."""
    source = "{code}\nbody that never closes"
    doc = parse(source)
    assert len(doc.children) >= 1
    # The first block is a Paragraph (the unclosed macro), not a
    # CodeBlock.
    assert isinstance(doc.children[0], Paragraph)


def test_attachments_deduplicated() -> None:
    source = "!img.png!\n\n!img.png!"
    _, _, attachments = parse_full(source)
    assert attachments == ("img.png",)


# ---------------------------------------------------------------------------
# Fixture corpus — real issue descriptions
# ---------------------------------------------------------------------------


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "jira_wiki"
FIXTURES = sorted(FIXTURE_DIR.glob("*.wiki"))


@pytest.mark.parametrize("fixture", FIXTURES, ids=[p.name for p in FIXTURES])
def test_fixture_parses_without_crashing(fixture: Path) -> None:
    """Every real-world wiki fixture must parse without raising and
    must produce at least one block."""
    source = fixture.read_text()
    document, warnings, attachments = parse_full(source)
    assert isinstance(document, Document)
    assert document.children, f"empty parse result for {fixture.name}"
    # Warnings and attachments are allowed to be empty.
    assert isinstance(warnings, list)
    assert isinstance(attachments, tuple)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[p.name for p in FIXTURES])
def test_fixture_round_trip_reaches_fixed_point(fixture: Path) -> None:
    """After at most two passes, the ``wiki → md → wiki`` cycle must
    reach a fixed point. The contract is NOT byte-identical with the
    original source — it is ``≈ canonicalize(source)`` — but the
    canonical form itself must be stable: pass 1 (``wiki → md``)
    canonicalizes and pass 2 (``md → wiki → md``) must be idempotent.

    This pins v0.3's "at most two passes" guarantee. If a future
    change regresses it back to three-pass convergence (as the
    pre-fix closing-bracket-escape issue did), this test flips red
    and the regression must be diagnosed before release.
    """
    from cfxmark import to_jira_wiki

    source = fixture.read_text()
    md1 = from_jira_wiki(source).markdown
    wiki2 = to_jira_wiki(
        md1, input_format="markdown", heading_promotion="jira"
    ).jira_wiki
    md3 = from_jira_wiki(wiki2).markdown
    wiki4 = to_jira_wiki(
        md3, input_format="markdown", heading_promotion="jira"
    ).jira_wiki
    md5 = from_jira_wiki(wiki4).markdown
    assert md3 == md5, (
        f"{fixture.name}: Markdown canonical form is not stable at the "
        f"second pass (md3 != md5). The parser / renderer pair is not "
        f"idempotent for this input."
    )
    assert wiki2 == wiki4, (
        f"{fixture.name}: Jira wiki canonical form is not stable at the "
        f"second pass (wiki2 != wiki4)."
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=[p.name for p in FIXTURES])
def test_fixture_warnings_are_not_tracebacks(fixture: Path) -> None:
    """Warnings are human-readable strings, not exception tracebacks."""
    source = fixture.read_text()
    _, warnings, _ = parse_full(source)
    for w in warnings:
        assert isinstance(w, str)
        assert "\n" not in w, f"multi-line warning: {w!r}"
        assert "Traceback" not in w
