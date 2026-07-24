from __future__ import annotations

import html

import pytest

import cfxmark

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def test_adjacent_mixed_jira_lists_preserve_every_item() -> None:
    result = cfxmark.from_jira_wiki("* A\n\n# B\n* C")

    assert result.markdown == "- A\n\n1. B\n\n- C\n"


def test_orphan_mixed_jira_marker_does_not_crash_or_drop_text() -> None:
    result = cfxmark.from_jira_wiki("* A\n#* mixed")

    assert "A" in (result.markdown or "")
    assert "mixed" in (result.markdown or "")


@pytest.mark.parametrize("tail", ["# y", "- y", "1. y", "===", "---"])
def test_hard_break_following_line_cannot_become_a_block(tail: str) -> None:
    storage = f"<p>x<br/>{html.escape(tail)}</p>"

    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown
    rendered = cfxmark.to_cfx(markdown or "", options=EDITABLE).xhtml

    assert rendered == storage


def test_list_item_preserves_space_between_adjacent_inline_code() -> None:
    # A single space between two inline code spans inside a list item is
    # meaningful content, not XML formatting. The list-item parser used to drop
    # whitespace-only tails (unlike the paragraph parser), so the two spans lost
    # their separator and coalesced into one -- a silent content loss.
    storage = "<ul><li><code>a.py</code> <code>b</code> tail</li></ul>"
    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown
    assert "`a.py` `b`" in markdown
    rendered = cfxmark.to_cfx(markdown or "", options=EDITABLE).xhtml
    assert rendered == storage


def test_list_item_truly_adjacent_inline_code_coalesces() -> None:
    # With no separator markdown cannot represent two spans, so they coalesce
    # into one visible run -- a serialization equivalence, not a loss.
    storage = "<ul><li><code>a</code><code>b</code></li></ul>"
    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown
    assert "`ab`" in markdown
    rendered = cfxmark.to_cfx(markdown or "", options=EDITABLE).xhtml
    assert cfxmark.storage_semantically_equal(rendered, storage)


def test_jira_external_image_target_and_parameters_round_trip() -> None:
    markdown = "![A](https://example.test/path/image.png?size=1#frag)\n"

    wiki = cfxmark.to_jira_wiki(markdown, input_format="markdown").jira_wiki
    back = cfxmark.from_jira_wiki(wiki or "")

    assert wiki == "!https://example.test/path/image.png?size=1#frag|alt=A!\n"
    assert "https://example.test/path/image.png?size=1#frag" in (back.markdown or "")
    assert back.attachments == ()


def test_jira_image_alt_and_size_reach_a_fixed_point() -> None:
    wiki_1 = r"!image.png|alt=a \[x\] \*,width=200,height=100!"

    markdown_1 = cfxmark.from_jira_wiki(wiki_1).markdown or ""
    wiki_2 = cfxmark.to_jira_wiki(markdown_1, input_format="markdown").jira_wiki or ""
    markdown_2 = cfxmark.from_jira_wiki(wiki_2).markdown or ""

    assert markdown_2 == markdown_1
    assert wiki_2 == wiki_1 + "\n"


@pytest.mark.parametrize(
    ("markdown", "expected_wiki"),
    [
        ("![a,b](f.png)\n", r"!f.png|alt=a\,b!" + "\n"),
        (
            "![A](https://example.test/x.png?q=a|b)\n",
            r"!https://example.test/x.png?q=a\|b|alt=A!" + "\n",
        ),
    ],
)
def test_jira_image_delimiters_are_escaped_and_reach_a_fixed_point(markdown: str, expected_wiki: str) -> None:
    wiki_1 = cfxmark.to_jira_wiki(markdown, input_format="markdown").jira_wiki or ""
    markdown_1 = cfxmark.from_jira_wiki(wiki_1).markdown or ""
    wiki_2 = cfxmark.to_jira_wiki(markdown_1, input_format="markdown").jira_wiki or ""

    assert wiki_1 == expected_wiki
    assert wiki_2 == wiki_1


def test_jira_table_monospace_pipe_stays_in_one_cell() -> None:
    markdown = cfxmark.from_jira_wiki("||Header||\n|a {{p|q}} b|").markdown or ""

    assert markdown == "| Header |\n| --- |\n| a `p\\|q` b |\n"


def test_ambiguous_gfm_table_code_pipe_is_rejected() -> None:
    with pytest.raises(cfxmark.ParseError, match="unescaped pipe"):
        cfxmark.to_cfx("| Header |\n| --- |\n| `a|b` |\n")


def test_escaped_backticks_with_pipe_in_table_are_not_a_code_span() -> None:
    # A backslash-escaped backtick is literal text to the CommonMark/mistletoe
    # inline parser, not a code-span delimiter, so a `|` between two of them is a
    # real cell delimiter -- not an unescaped pipe inside a code span. This is how
    # a Confluence table whose cells hold literal-backtick text renders, and it
    # must NOT be rejected (a genuine `<code>a|b</code>` span still is, above).
    result = cfxmark.to_cfx("| H1 | H2 | H3 |\n| --- | --- | --- |\n| \\`x | y\\` | z |\n", options=EDITABLE)
    assert "<td>`x</td>" in result.xhtml
    assert "<td>y`</td>" in result.xhtml


def test_non_table_leading_pipe_with_code_pipe_is_not_rejected() -> None:
    result = cfxmark.to_cfx("| prose `a|b` only\n", options=EDITABLE)

    assert result.xhtml == "<p>| prose <code>a|b</code> only</p>"


def test_jira_list_code_block_normalizes_once_with_warning() -> None:
    markdown = "- item\n\n  ```\n  code\n  ```\n"

    first = cfxmark.to_jira_wiki(markdown, input_format="markdown")
    markdown_2 = cfxmark.from_jira_wiki(first.jira_wiki or "").markdown or ""
    second = cfxmark.to_jira_wiki(markdown_2, input_format="markdown")

    assert second.jira_wiki == first.jira_wiki
    assert any("cannot nest" in warning for warning in first.warnings)


def test_boundary_nbsp_survives_confluence_and_jira_writes() -> None:
    markdown = "\u00a0A\u00a0"

    assert cfxmark.to_cfx(markdown, options=EDITABLE).xhtml == "<p>\u00a0A\u00a0</p>"
    assert cfxmark.to_jira_wiki(markdown, input_format="markdown").jira_wiki == "\u00a0A\u00a0\n"


def test_nbsp_does_not_invalidate_authenticated_opaque_payload() -> None:
    storage = "<custom-block><p>\u00a0value\u00a0</p></custom-block>"

    markdown = cfxmark.to_md(storage).markdown or ""
    restored = cfxmark.to_cfx(markdown).xhtml or ""

    assert cfxmark.canonicalize_cfx(restored) == cfxmark.canonicalize_cfx(storage)


@pytest.mark.parametrize("content", ["x", "x\n", "x\n\n", ""])
def test_code_block_trailing_newlines_are_byte_preserved(content: str) -> None:
    storage = (
        '<ac:structured-macro xmlns:ac="http://atlassian.com/content" ac:name="code">'
        f"<ac:plain-text-body><![CDATA[{content}]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )

    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown or ""
    rendered = cfxmark.to_cfx(markdown, options=EDITABLE).xhtml or ""

    assert f"<![CDATA[{content}]]>" in rendered


def test_link_title_loss_is_reported_for_jira() -> None:
    result = cfxmark.to_jira_wiki('[A](https://example.test "Title")', input_format="markdown")

    assert result.jira_wiki == "[A|https://example.test]\n"
    assert any("title" in warning for warning in result.warnings)


def test_text_code_language_is_preserved() -> None:
    storage = (
        '<ac:structured-macro xmlns:ac="http://atlassian.com/content" ac:name="code">'
        '<ac:parameter ac:name="language">text</ac:parameter>'
        "<ac:plain-text-body><![CDATA[x]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )

    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown or ""
    rendered = cfxmark.to_cfx(markdown, options=EDITABLE).xhtml or ""

    assert markdown.startswith("```text\n")
    assert '<ac:parameter ac:name="language">text</ac:parameter>' in rendered


def test_whitespace_only_inline_code_reaches_a_fixed_point() -> None:
    first = cfxmark.to_md("<p>a <code>  </code> b</p>", options=EDITABLE).markdown or ""
    second = cfxmark.to_md(cfxmark.to_cfx(first, options=EDITABLE).xhtml or "", options=EDITABLE).markdown

    assert first == "a `  ` b\n"
    assert second == first


def test_real_external_cfxmark_fragment_is_preserved_and_diagnosed() -> None:
    markdown = "![](https://example.test/image.png#cfxmark:w=100)\n"

    result = cfxmark.to_cfx(markdown, options=EDITABLE)

    assert 'ri:value="https://example.test/image.png#cfxmark:w=100"' in (result.xhtml or "")
    assert any("reserved cfxmark fragment" in warning for warning in result.warnings)


def test_external_image_dimensions_use_unambiguous_metadata() -> None:
    storage = (
        '<p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier" ac:width="100">'
        '<ri:url ri:value="https://example.test/image.png"/></ac:image></p>'
    )

    markdown = cfxmark.to_md(storage, options=EDITABLE).markdown or ""
    rendered = cfxmark.to_cfx(markdown, options=EDITABLE).xhtml or ""

    assert "<!-- cfxmark:img w=100 -->" in markdown
    assert "#cfxmark" not in markdown
    assert 'ac:width="100"' in rendered
    assert 'ri:value="https://example.test/image.png"' in rendered


def test_layout_comment_body_table_and_emoticon_are_not_silently_push_safe() -> None:
    cases = [
        (
            '<ac:layout xmlns:ac="http://atlassian.com/content"><ac:layout-section>'
            "<ac:layout-cell><p>left</p></ac:layout-cell><ac:layout-cell><p>right</p></ac:layout-cell>"
            "</ac:layout-section></ac:layout>",
            "layout",
        ),
        ("<p>a</p><!-- hidden --><p>b</p>", "comments"),
        ("<table><tbody><tr><td>A</td></tr></tbody></table>", "body-only tables"),
        ('<p><ac:emoticon xmlns:ac="http://atlassian.com/content" ac:name="smile"/></p>', "emoticon"),
    ]

    for storage, loss_fragment in cases:
        result = cfxmark.to_md(storage, options=EDITABLE)
        assert result.push_safe is False
        assert any(loss_fragment in loss for loss in result.losses)
