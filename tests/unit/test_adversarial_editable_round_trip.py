from __future__ import annotations

import html

import lxml.etree as ET
import pytest

import cfxmark

EDITABLE = cfxmark.ConversionOptions(profile="editable")
_WRAPPER_NAMESPACES = 'xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource/identifier"'


def _parse_fragment(rendered: str) -> ET._Element:
    return ET.fromstring(f"<root {_WRAPPER_NAMESPACES}>{rendered}</root>")


def _round_trip(storage: str) -> tuple[str, str]:
    first = cfxmark.to_md(storage, options=EDITABLE)
    assert first.push_safe is True
    assert first.markdown is not None
    rendered = cfxmark.to_cfx(first.markdown, options=EDITABLE).xhtml
    assert rendered is not None
    assert cfxmark.to_md(rendered, options=EDITABLE).markdown == first.markdown
    return first.markdown, rendered


@pytest.mark.parametrize(
    "text",
    [
        "# heading-shaped text",
        "- list-shaped text",
        "+ list-shaped text",
        "> quote-shaped text",
        "1. ordered-list-shaped text",
        "~~strike-shaped text~~",
        "    indented text",
        "<Tag> <url> <b>",
        "&lt; remains an entity literal",
    ],
)
def test_paragraph_text_never_becomes_markdown_structure(text: str) -> None:
    storage = f"<p>{html.escape(text)}</p>"

    _markdown, rendered = _round_trip(storage)

    root = _parse_fragment(rendered)
    assert "".join(root.itertext()) == text


def test_image_alt_with_markdown_punctuation_round_trips_exactly() -> None:
    storage = (
        '<p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier" '
        'ac:alt="[left] *star* _under_ \\ slash">'
        '<ri:attachment ri:filename="image.png"/>'
        "</ac:image></p>"
    )

    _markdown, rendered = _round_trip(storage)

    root = _parse_fragment(rendered)
    image = root[0][0]
    assert image.get("{http://atlassian.com/content}alt") == "[left] *star* _under_ \\ slash"


@pytest.mark.parametrize("code", ["|", r"\|"])
def test_inline_code_with_pipe_stays_in_one_table_cell(code: str) -> None:
    storage = (
        "<table><thead><tr><th>Header</th></tr></thead>"
        f"<tbody><tr><td><code>{html.escape(code)}</code></td></tr></tbody></table>"
    )

    _markdown, rendered = _round_trip(storage)

    root = _parse_fragment(rendered)
    cells = root.xpath(".//tbody/tr/td")
    assert len(cells) == 1
    assert "".join(cells[0].itertext()) == code


@pytest.mark.parametrize(
    "url",
    [
        "relative path",
        "relative)",
        "https://example.test/a b)",
        "<literal>",
    ],
)
def test_link_destination_round_trips_exactly(url: str) -> None:
    storage = f'<p><a href="{html.escape(url, quote=True)}">label</a></p>'

    _markdown, rendered = _round_trip(storage)

    root = _parse_fragment(rendered)
    assert root[0][0].get("href") == url


def test_xml_comments_and_processing_instructions_are_ignored_without_losing_tail_text() -> None:
    storage = "<!--before--><p>A<!--middle-->B<?sample value?>C</p><?after value?>"

    result = cfxmark.to_md(storage, options=EDITABLE)

    assert result.markdown == "ABC\n"
    assert result.push_safe is False
    assert result.losses == ("XML comments and processing instructions are not represented in Markdown",) * 4
    dropped = [diagnostic for diagnostic in result.diagnostics if diagnostic.code == "xml-comment-dropped"]
    assert len(dropped) == 4
    assert len({diagnostic.occurrence_id for diagnostic in dropped}) == 4
    jira = cfxmark.to_jira_wiki(storage, input_format="xhtml")
    assert jira.jira_wiki == "ABC\n"
    assert jira.push_safe is False


def test_legacy_background_color_round_trips() -> None:
    storage = '<p><span style="background-color:#ffeceb">Alert</span></p>'

    first = cfxmark.to_md(storage)
    assert first.markdown is not None
    rendered = cfxmark.to_cfx(first.markdown).xhtml

    assert rendered is not None
    assert "background-color:" in rendered
    assert "Alert" in rendered


def test_markdown_table_alignment_survives_storage_round_trip() -> None:
    markdown = "| Center | Right |\n| :---: | ---: |\n| A | B |\n"

    storage = cfxmark.to_cfx(markdown, options=EDITABLE).xhtml

    assert storage is not None
    assert 'style="text-align: center;"' in storage
    assert 'style="text-align: right;"' in storage
    assert cfxmark.to_md(storage, options=EDITABLE).markdown == markdown


@pytest.mark.parametrize("code", [" x ", " leading", "trailing "])
def test_inline_code_boundary_spaces_round_trip(code: str) -> None:
    storage = f"<p><code>{html.escape(code)}</code></p>"

    _markdown, rendered = _round_trip(storage)

    root = _parse_fragment(rendered)
    assert "".join(root[0][0].itertext()) == code


def test_single_data_row_is_not_promoted_to_header() -> None:
    storage = "<table><tbody><tr><td>Only data</td></tr></tbody></table>"

    result = cfxmark.to_md(storage, options=EDITABLE)

    assert result.markdown is not None
    assert result.markdown.startswith("|  |\n| --- |\n| Only data |")
    assert result.push_safe is False
    assert result.losses == ("body-only tables require a synthetic Markdown header row",)


def test_readable_merged_table_hides_continuation_syntax() -> None:
    storage = (
        '<table><thead><tr><th colspan="2">Header</th></tr></thead>'
        '<tbody><tr><td rowspan="2">Tall</td><td>One</td></tr>'
        "<tr><td>Two</td></tr></tbody></table>"
    )

    result = cfxmark.to_md(storage, options=cfxmark.ConversionOptions(profile="readable"))

    assert result.markdown is not None
    assert "| < |" not in result.markdown
    assert "| ^ |" not in result.markdown
