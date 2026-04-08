"""Unit tests for the Jira wiki markup renderer.

One test per mapping rule as specified in §3.2.3 (inline) and §3.2.4
(block) of the v0.2.0 plan.
"""

from __future__ import annotations

import re

import pytest

from cfxmark.api import _resolve_input_format, to_jira_wiki
from cfxmark.ast import (
    BlockQuote,
    CellType,
    CodeBlock,
    DirectiveMacro,
    Document,
    Emphasis,
    HardBreak,
    Heading,
    HorizontalRule,
    Image,
    InlineCode,
    InlineOpaque,
    Link,
    List,
    ListItem,
    ListType,
    Paragraph,
    SoftBreak,
    Strikethrough,
    Strong,
    Table,
    TableCell,
    TableRow,
    Text,
)
from cfxmark.renderers.jira_wiki import render_jira_wiki

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def doc(*blocks):
    """Convenience: wrap blocks in a Document."""
    return Document(children=tuple(blocks))


def para(*inlines):
    """Convenience: wrap inlines in a Paragraph."""
    return Paragraph(children=tuple(inlines))


def render(document, **kwargs):
    """Call render_jira_wiki and return (body, warnings)."""
    return render_jira_wiki(document, **kwargs)


# ---------------------------------------------------------------------------
# Heading promotion rules (§3.2.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,expected_prefix",
    [
        (1, "h1."),
        (2, "h2."),
        (3, "h2."),  # PROMOTED
        (4, "h3."),  # PROMOTED
        (5, "h4."),
        (6, "h5."),
    ],
)
def test_heading_levels(level, expected_prefix):
    d = doc(Heading(level=level, children=(Text(content="X"),)))
    body, warnings = render(d)
    assert body.startswith(f"{expected_prefix} X"), repr(body)
    assert not warnings


# ---------------------------------------------------------------------------
# Inline mapping rules (§3.2.3)
# ---------------------------------------------------------------------------


def test_text_plain():
    d = doc(para(Text(content="hello world")))
    body, _ = render(d)
    assert "hello world" in body


def test_text_escape_asterisk():
    d = doc(para(Text(content="a*b")))
    body, _ = render(d)
    assert r"a\*b" in body


def test_text_escape_underscore():
    d = doc(para(Text(content="a_b")))
    body, _ = render(d)
    assert r"a\_b" in body


def test_text_escape_open_brace():
    d = doc(para(Text(content="a{b")))
    body, _ = render(d)
    assert r"a\{b" in body


def test_text_escape_open_bracket():
    d = doc(para(Text(content="a[b")))
    body, _ = render(d)
    assert r"a\[b" in body


def test_text_escape_pipe():
    d = doc(para(Text(content="a|b")))
    body, _ = render(d)
    assert r"a\|b" in body


def test_text_escape_backslash():
    d = doc(para(Text(content="a\\b")))
    body, _ = render(d)
    assert r"a\\b" in body


def test_soft_break():
    d = doc(para(Text(content="a"), SoftBreak(), Text(content="b")))
    body, _ = render(d)
    assert "a b" in body


def test_hard_break_dropped_with_warning():
    d = doc(para(Text(content="a"), HardBreak(), Text(content="b")))
    body, warnings = render(d)
    assert "ab" in body or "a" in body
    assert any("br" in w or "hard_break" in w or "dropped" in w.lower() for w in warnings)


def test_strong():
    d = doc(para(Strong(children=(Text(content="bold"),))))
    body, _ = render(d)
    assert "*bold*" in body


def test_emphasis():
    d = doc(para(Emphasis(children=(Text(content="italic"),))))
    body, _ = render(d)
    assert "_italic_" in body


def test_strikethrough():
    d = doc(para(Strikethrough(children=(Text(content="struck"),))))
    body, _ = render(d)
    assert "-struck-" in body


def test_inline_code():
    d = doc(para(InlineCode(content="x = 1")))
    body, _ = render(d)
    assert "{{x = 1}}" in body


def test_link_with_text():
    d = doc(para(Link(url="https://example.com", children=(Text(content="click"),))))
    body, _ = render(d)
    assert "[click|https://example.com]" in body


def test_link_empty_children():
    d = doc(para(Link(url="https://example.com", children=())))
    body, _ = render(d)
    assert "[https://example.com]" in body


def test_image_with_alt():
    d = doc(para(Image(src="path/to/diagram.png", alt="diagram")))
    body, _ = render(d)
    assert "!diagram.png|alt=diagram!" in body


def test_image_without_alt():
    d = doc(para(Image(src="path/to/ego.png", alt="")))
    body, _ = render(d)
    assert "!ego.png!" in body


def test_image_basename_only():
    """Only the basename of the src path should appear in output."""
    d = doc(para(Image(src="assets/sub/photo.jpg", alt="")))
    body, _ = render(d)
    assert "!photo.jpg!" in body
    assert "assets" not in body


def test_inline_opaque_emits_label_and_warning():
    d = doc(para(InlineOpaque(raw_xml="<x/>", opaque_id="abc123", label="some-label")))
    body, warnings = render(d)
    assert "(cfx:some-label)" in body
    assert any("inline opaque" in w.lower() or "cfx" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Block mapping rules (§3.2.4)
# ---------------------------------------------------------------------------


def test_paragraph_renders_inline():
    d = doc(para(Text(content="hello")))
    body, _ = render(d)
    assert "hello" in body


def test_code_block_no_lang():
    d = doc(CodeBlock(content="x = 1", language=None))
    body, _ = render(d)
    assert "{code}\nx = 1\n{code}" in body


def test_code_block_with_lang():
    d = doc(CodeBlock(content="x = 1", language="python"))
    body, _ = render(d)
    assert "{code:python}\nx = 1\n{code}" in body


def test_code_block_content_truncation_warning():
    """Content containing {code} literal should produce a warning."""
    d = doc(CodeBlock(content="before {code} after", language=None))
    body, warnings = render(d)
    assert any("truncat" in w.lower() or "{code}" in w for w in warnings)


def test_blockquote_dropped_with_warning():
    d = doc(BlockQuote(children=(para(Text(content="quoted")),)))
    body, warnings = render(d)
    assert "quoted" not in body
    assert any("blockquote" in w.lower() or "dropped" in w.lower() for w in warnings)


def test_horizontal_rule():
    d = doc(HorizontalRule())
    body, _ = render(d)
    assert "----" in body


def test_table_dropped_with_warning():
    cell = TableCell(kind=CellType.DATA, children=(Text(content="a"),))
    row = TableRow(cells=(cell,))
    d = doc(Table(header=None, body=(row,)))
    body, warnings = render(d)
    assert "a" not in body
    assert any("table" in w.lower() or "dropped" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Directive macros (§3.2.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["info", "note", "warning", "tip"])
def test_native_admonition(name):
    """info/note/warning/tip → {name}\n...\n{name} (NOT panel!)."""
    body_para = para(Text(content="body text"))
    d = doc(DirectiveMacro(name=name, body=(body_para,)))
    body, warnings = render(d)
    assert f"{{{name}}}" in body
    assert "body text" in body
    assert "panel" not in body
    # No warning for known admonitions
    assert not any(name in w for w in warnings)


def test_unknown_directive_inlines_body_with_warning():
    body_para = para(Text(content="some content"))
    d = doc(DirectiveMacro(name="expand", body=(body_para,)))
    body, warnings = render(d)
    assert "some content" in body
    assert any("expand" in w or "unknown" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# List mapping rules — nested marker chains (§3.2.4)
# ---------------------------------------------------------------------------


def _bullet_item(text: str) -> ListItem:
    return ListItem(children=(para(Text(content=text)),))


def _ordered_item(text: str) -> ListItem:
    return ListItem(children=(para(Text(content=text)),))


def test_bullet_list_single_level():
    """- foo → * foo"""
    d = doc(List(list_type=ListType.BULLET, items=(_bullet_item("foo"),)))
    body, _ = render(d)
    assert "* foo" in body


def test_ordered_list_single_level():
    """1. foo → # foo"""
    d = doc(List(list_type=ListType.ORDERED, items=(_ordered_item("foo"),)))
    body, _ = render(d)
    assert "# foo" in body


def test_bullet_bullet_nested():
    """- - bar → ** bar"""
    inner = List(list_type=ListType.BULLET, items=(_bullet_item("bar"),))
    outer_item = ListItem(children=(inner,))
    d = doc(List(list_type=ListType.BULLET, items=(outer_item,)))
    body, _ = render(d)
    assert "** bar" in body


def test_ordered_ordered_nested():
    """1. 1. bar → ## bar"""
    inner = List(list_type=ListType.ORDERED, items=(_ordered_item("bar"),))
    outer_item = ListItem(children=(inner,))
    d = doc(List(list_type=ListType.ORDERED, items=(outer_item,)))
    body, _ = render(d)
    assert "## bar" in body


def test_bullet_ordered_nested():
    """- 1. bar → *# bar"""
    inner = List(list_type=ListType.ORDERED, items=(_ordered_item("bar"),))
    outer_item = ListItem(children=(inner,))
    d = doc(List(list_type=ListType.BULLET, items=(outer_item,)))
    body, _ = render(d)
    assert "*# bar" in body


def test_ordered_bullet_nested():
    """1. - bar → #* bar"""
    inner = List(list_type=ListType.BULLET, items=(_bullet_item("bar"),))
    outer_item = ListItem(children=(inner,))
    d = doc(List(list_type=ListType.ORDERED, items=(outer_item,)))
    body, _ = render(d)
    assert "#* bar" in body


def test_bullet_ordered_bullet_nested():
    """- 1. - baz → *#* baz"""
    innermost = List(list_type=ListType.BULLET, items=(_bullet_item("baz"),))
    middle_item = ListItem(children=(innermost,))
    middle = List(list_type=ListType.ORDERED, items=(middle_item,))
    outer_item = ListItem(children=(middle,))
    d = doc(List(list_type=ListType.BULLET, items=(outer_item,)))
    body, _ = render(d)
    assert "*#* baz" in body


def test_empty_list_item():
    item = ListItem(children=())
    d = doc(List(list_type=ListType.BULLET, items=(item,)))
    body, _ = render(d)
    # Should not crash; output contains the bullet marker (trailing space may be stripped)
    assert body is not None
    assert "*" in body


# ---------------------------------------------------------------------------
# Section slicing
# ---------------------------------------------------------------------------


def test_section_present():
    h = Heading(level=2, children=(Text(content="Intro"),))
    p = para(Text(content="content"))
    h2 = Heading(level=2, children=(Text(content="Other"),))
    d = doc(h, p, h2)
    body, _ = render(d, section="Intro")
    assert "content" in body
    assert "Other" not in body


def test_section_missing_returns_none():
    d = doc(Heading(level=2, children=(Text(content="Intro"),)))
    body, warnings = render(d, section="Missing")
    assert body is None
    assert warnings == []


def test_section_empty_returns_empty_string():
    h = Heading(level=2, children=(Text(content="Empty"),))
    h2 = Heading(level=2, children=(Text(content="Next"),))
    d = doc(h, h2)
    body, _ = render(d, section="Empty")
    assert body == ""


def test_section_single_h2_returns_full_content():
    """When the requested H2 is the only H2 in the document, the slice
    runs to EOF (no terminator H2 to stop on)."""
    h = Heading(level=2, children=(Text(content="Story Summary"),))
    p1 = para(Text(content="first"))
    p2 = para(Text(content="second"))
    d = doc(h, p1, p2)
    body, _ = render(d, section="Story Summary")
    assert body is not None
    assert "first" in body
    assert "second" in body


def test_section_terminated_by_horizontal_rule():
    """``<hr/>`` ends the section even if no terminator H2 follows."""
    h = Heading(level=2, children=(Text(content="Story Summary"),))
    p = para(Text(content="kept"))
    hr = HorizontalRule()
    p_after = para(Text(content="dropped"))
    d = doc(h, p, hr, p_after)
    body, _ = render(d, section="Story Summary")
    assert body is not None
    assert "kept" in body
    assert "dropped" not in body


def test_section_first_match_wins_when_h2_repeats():
    """If the same H2 title appears twice, the slice ends at the
    second occurrence — the first wins, the duplicate is content of
    nothing (it terminates the slice)."""
    h_first = Heading(level=2, children=(Text(content="Story Summary"),))
    p_first = para(Text(content="first content"))
    h_dup = Heading(level=2, children=(Text(content="Story Summary"),))
    p_dup = para(Text(content="dup content"))
    d = doc(h_first, p_first, h_dup, p_dup)
    body, _ = render(d, section="Story Summary")
    assert body is not None
    assert "first content" in body
    assert "dup content" not in body


def test_section_cdata_inside_macro_does_not_truncate():
    """A ``<h2>`` literal embedded inside a Confluence code macro's
    ``<ac:plain-text-body><![CDATA[...]]>`` body must NOT be parsed as
    a section terminator. The AST renderer is naturally immune to this
    because CDATA contents flow through the parser as a CodeBlock /
    OpaqueBlock node, not as a Heading. This test pins the behavior so
    a future regex-based shortcut cannot reintroduce the bug.

    The XHTML below is a code-fenced block whose body literally
    contains ``## Story Summary`` text — it must be preserved verbatim
    inside the requested section.
    """
    xhtml = (
        "<h2>Story Summary</h2>"
        "<p>real summary</p>"
        '<ac:structured-macro ac:name="code">'
        '<ac:plain-text-body><![CDATA[## Story Summary]]></ac:plain-text-body>'
        "</ac:structured-macro>"
        "<h2>Next Section</h2>"
        "<p>excluded</p>"
    )
    result = to_jira_wiki(
        xhtml, input_format="xhtml", section="Story Summary"
    )
    assert result.jira_wiki is not None
    assert "real summary" in result.jira_wiki
    assert "excluded" not in result.jira_wiki
    # The CDATA-fenced "## Story Summary" must survive as code-block
    # content, not be parsed as a section terminator that truncates
    # the slice prematurely.
    assert "Story Summary" in result.jira_wiki  # appears inside the {code} body


# ---------------------------------------------------------------------------
# drop_leading_notice
# ---------------------------------------------------------------------------


def test_drop_leading_notice_matching():
    p_notice = para(Text(content="NOTICE: do not edit"))
    p_body = para(Text(content="real content"))
    d = doc(p_notice, p_body)
    body, _ = render(d, drop_leading_notice=(re.compile(r"NOTICE"),))
    assert "NOTICE" not in body
    assert "real content" in body


def test_drop_leading_notice_non_matching():
    p_first = para(Text(content="normal intro"))
    p_body = para(Text(content="body"))
    d = doc(p_first, p_body)
    body, _ = render(d, drop_leading_notice=(re.compile(r"NOTICE"),))
    assert "normal intro" in body
    assert "body" in body


def test_drop_leading_notice_italic_wrapped():
    """Leading notice wrapped in Emphasis still gets dropped."""
    p_notice = para(Emphasis(children=(Text(content="NOTICE: skip this"),)))
    p_body = para(Text(content="actual content"))
    d = doc(p_notice, p_body)
    body, _ = render(d, drop_leading_notice=(re.compile(r"NOTICE"),))
    assert "NOTICE" not in body
    assert "actual content" in body


def test_drop_leading_notice_strong_wrapped():
    """Leading notice wrapped in Strong still gets dropped (R4-10)."""
    p_notice = para(Strong(children=(Text(content="NOTICE: skip this"),)))
    p_body = para(Text(content="actual content"))
    d = doc(p_notice, p_body)
    body, _ = render(d, drop_leading_notice=(re.compile(r"NOTICE"),))
    assert "NOTICE" not in body
    assert "actual content" in body


def test_drop_leading_notice_mixed_inline():
    """Mixed inline (Strong + Text) flattens for the drop check (R4-10)."""
    p_notice = para(
        Strong(children=(Text(content="NOTICE:"),)),
        Text(content=" do not edit this"),
    )
    p_body = para(Text(content="actual content"))
    d = doc(p_notice, p_body)
    body, _ = render(d, drop_leading_notice=(re.compile(r"NOTICE.*do not edit"),))
    assert "NOTICE" not in body
    assert "do not edit" not in body
    assert "actual content" in body


def test_drop_leading_notice_korean_italic_preserved():
    """A legitimate Korean one-liner italic summary survives when no
    pattern matches. The drop is policy-neutral — only paragraphs
    matching a caller-supplied regex are removed."""
    p_summary = para(
        Emphasis(children=(Text(content="요약: 신규 기능 추가"),))
    )
    p_body = para(Text(content="본문 내용"))
    d = doc(p_summary, p_body)
    # Pattern matches English NOTICE template — Korean italic must survive
    body, _ = render(d, drop_leading_notice=(re.compile(r"NOTICE"),))
    assert "요약" in body
    assert "본문 내용" in body


# ---------------------------------------------------------------------------
# Dropped-construct warning format (RLM B-4)
# ---------------------------------------------------------------------------


def test_no_dropped_warning_on_clean_input():
    """A document with no dropped constructs emits zero warnings."""
    d = doc(
        Heading(level=2, children=(Text(content="Title"),)),
        para(Text(content="just text")),
    )
    _, warnings = render(d)
    assert warnings == []


def test_dropped_table_warning_count_format():
    """Three tables in one document → '<table>x3 will be dropped'."""
    cell = TableCell(kind=CellType.DATA, children=(Text(content="x"),))
    row = TableRow(cells=(cell,))
    table = Table(header=None, body=(row,))
    d = doc(table, table, table)
    _, warnings = render(d)
    assert any("<table>x3 will be dropped" in w for w in warnings)


def test_dropped_blockquote_warning_count_format():
    bq = BlockQuote(children=(para(Text(content="quoted")),))
    d = doc(bq, bq)
    _, warnings = render(d)
    assert any("<blockquote>x2 will be dropped" in w for w in warnings)


def test_dropped_hard_break_warning_count_format():
    """HardBreak emits '<br>xN will be dropped' (one count per occurrence)."""
    p = para(
        Text(content="line1"),
        HardBreak(),
        Text(content="line2"),
        HardBreak(),
        Text(content="line3"),
    )
    d = doc(p)
    _, warnings = render(d)
    assert any("<br>x2 will be dropped" in w for w in warnings)


# ---------------------------------------------------------------------------
# input_format detection (via to_jira_wiki)
# ---------------------------------------------------------------------------


def test_input_format_auto_markdown():
    result = to_jira_wiki("Hello world\n", input_format="auto")
    assert result.jira_wiki is not None
    assert "Hello world" in result.jira_wiki


def test_input_format_auto_xhtml():
    xhtml = "<ac:structured-macro ac:name='toc'/>"
    result = to_jira_wiki(xhtml, input_format="auto")
    # Should parse as xhtml (starts with <ac:)
    assert result.document is not None


def test_input_format_auto_ambiguous_tag_warns():
    """<p>...</p> starts with < but is not Confluence-specific → markdown default + warning."""
    result = to_jira_wiki("<p>hello</p>", input_format="auto")
    assert any("auto" in w.lower() or "markdown" in w.lower() for w in result.warnings)


def test_input_format_forced_markdown():
    result = to_jira_wiki("# Hello\n", input_format="markdown")
    assert result.jira_wiki is not None
    assert "h1." in result.jira_wiki


def test_input_format_forced_xhtml():
    xhtml = "<p>hello</p>"
    result = to_jira_wiki(xhtml, input_format="xhtml")
    assert result.jira_wiki is not None


def test_input_format_auto_xml_declaration():
    """<?xml prefix is detected as xhtml without an ambiguity warning."""
    warnings: list[str] = []
    fmt = _resolve_input_format("<?xml version='1.0'?><root/>", "auto", warnings)
    assert fmt == "xhtml"
    assert not any("defaulting to markdown" in w for w in warnings)


def test_input_format_auto_doctype():
    """<!DOCTYPE prefix is detected as xhtml without an ambiguity warning."""
    warnings: list[str] = []
    fmt = _resolve_input_format("<!DOCTYPE html><html/>", "auto", warnings)
    assert fmt == "xhtml"
    assert not any("defaulting to markdown" in w for w in warnings)


# ---------------------------------------------------------------------------
# HTML entity unescape via XHTML input (RLM B-3)
# ---------------------------------------------------------------------------
#
# cfxmark's AST-based renderer relies on lxml's parser to unescape XML
# entities once, on the way into the AST. The Jira wiki renderer then
# escapes only the Jira-significant characters (* _ { [ | \). These
# tests pin the contract so a future regex-based shortcut cannot
# silently double-escape or skip entity decoding.


def test_entity_basic_lt_gt_amp_quot():
    """``&lt;`` ``&gt;`` ``&amp;`` ``&quot;`` decode to literal characters."""
    xhtml = "<p>a &lt; b &amp; c &gt; d &quot;e&quot;</p>"
    result = to_jira_wiki(xhtml, input_format="xhtml")
    assert result.jira_wiki is not None
    assert "a < b & c > d \"e\"" in result.jira_wiki


def test_entity_numeric_decimal():
    """``&#39;`` (numeric apostrophe) decodes to a literal ``'``."""
    xhtml = "<p>it&#39;s here</p>"
    result = to_jira_wiki(xhtml, input_format="xhtml")
    assert result.jira_wiki is not None
    assert "it's here" in result.jira_wiki


def test_entity_double_escape_single_pass():
    """``&amp;quot;`` is unescaped exactly once → literal ``&quot;``.

    The lxml parser performs a single decoding pass: the outer
    ``&amp;`` becomes a literal ``&``, and the inner ``quot;`` is
    surfaced as plain text. This pins single-pass semantics so a
    future regex hack cannot accidentally re-decode and turn the
    payload into a literal ``"``.
    """
    xhtml = "<p>&amp;quot;double&amp;quot;</p>"
    result = to_jira_wiki(xhtml, input_format="xhtml")
    assert result.jira_wiki is not None
    assert "&quot;double&quot;" in result.jira_wiki
