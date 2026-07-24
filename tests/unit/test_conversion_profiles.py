from __future__ import annotations

import pytest

import cfxmark
from cfxmark.ast import CellType, TableCell, TableRow, Text
from cfxmark.table_grid import bounded_table_grid_width

AC_NS = "http://atlassian.com/content"
RI_NS = "http://atlassian.com/resource/identifier"


def options(profile: str) -> cfxmark.ConversionOptions:
    return cfxmark.ConversionOptions(profile=profile)  # type: ignore[arg-type]


def test_strong_moves_boundary_whitespace_outside_delimiters() -> None:
    storage = "<p><strong>Alpha\u00a0</strong><strong><strong>Beta</strong></strong></p>"

    markdown = cfxmark.to_md(storage).markdown

    assert markdown == "**Alpha\u00a0Beta**\n"
    assert "****" not in markdown
    assert "<strong>" not in markdown


def test_nested_strong_inside_strong_is_flattened() -> None:
    storage = "<p><strong>Alpha<strong>/</strong><strong>Beta</strong></strong></p>"

    markdown = cfxmark.to_md(storage).markdown

    assert markdown == "**Alpha/Beta**\n"
    assert "****" not in markdown


def test_strong_boundary_whitespace_controls_commonmark_flanking() -> None:
    storage = "<p><strong>\u00a0 ┗ </strong>Suffix</p>"

    markdown = cfxmark.to_md(storage).markdown

    assert markdown == "&nbsp; **┗** Suffix\n"
    assert "<strong>" not in markdown


def test_nbsp_entities_are_symmetric_across_round_trip() -> None:
    storage = "<p>literal\u00a0named&nbsp;numeric&#160;done</p>"

    markdown_1 = cfxmark.to_md(storage).markdown
    assert markdown_1 is not None
    assert markdown_1.count("\u00a0") == 3

    storage_1 = cfxmark.to_cfx(markdown_1).xhtml
    assert storage_1 is not None
    assert storage_1.count("\u00a0") == 3

    markdown_2 = cfxmark.to_md(storage_1).markdown
    assert markdown_2 == markdown_1


def test_boundary_nbsp_uses_stable_entity_encoding() -> None:
    storage = (
        "<p><strong>Label</strong>\u00a0</p>"
        "<table><tbody><tr><th>Header\u00a0</th></tr>"
        "<tr><td>Value\u00a0</td></tr></tbody></table>"
    )

    markdown_1 = cfxmark.to_md(storage).markdown
    assert markdown_1 is not None
    assert markdown_1.count("&nbsp;") == 3

    storage_1 = cfxmark.to_cfx(markdown_1).xhtml
    assert storage_1 is not None
    markdown_2 = cfxmark.to_md(storage_1).markdown

    assert markdown_2 == markdown_1


def test_editable_and_readable_profiles_have_distinct_contracts() -> None:
    storage = (
        f'<p xmlns:ac="{AC_NS}" xmlns:ri="{RI_NS}">'
        '<span style="color:var(--ds-text-accent-green, #216e4e)">Ready</span>'
        '<span style="background-color:var(--ds-background-accent-orange-subtlest, #fff3eb)">Watch</span>'
        "<ins>Underlined</ins>"
        '<ac:emoticon ac:name="information"/>'
        '<ac:image ac:width="320" ac:height="180" ac:thumbnail="true" ac:align="center">'
        '<ri:attachment ri:filename="diagram.png"/>'
        "</ac:image>"
        '<ac:unknown ac:value="private"/>'
        "</p>"
    )

    editable = cfxmark.to_md(storage, options=options("editable"))
    readable = cfxmark.to_md(storage, options=options("readable"))

    assert editable.markdown is not None
    assert editable.push_safe is False
    assert '<span data-cfx-color="green">Ready</span>' in editable.markdown
    assert '<mark data-cfx-color="amber">Watch</mark>' in editable.markdown
    assert "<ins>Underlined</ins>" in editable.markdown
    assert "ℹ️" in editable.markdown
    assert "<!-- cfxmark:img w=320 h=180 thumbnail=1 align=center -->" in editable.markdown
    assert "cfxmark:asset" in editable.markdown
    assert "cfxmark:unsupported" in editable.markdown
    assert "<ac:unknown" not in editable.markdown
    assert "cfxmark:payloads" not in editable.markdown

    assert readable.markdown is not None
    assert readable.push_safe is False
    assert "ReadyWatchUnderlined" in readable.markdown
    assert "data-cfx-color" not in readable.markdown
    assert "<ins>" not in readable.markdown
    assert "#cfxmark:" not in readable.markdown
    assert "cfxmark:asset" not in readable.markdown
    assert "<ac:unknown" not in readable.markdown


def test_editable_profile_round_trips_coarse_color_image_and_alignment() -> None:
    markdown = (
        '<span data-cfx-color="red">Blocked</span> '
        '<mark data-cfx-color="blue">Info</mark>\n\n'
        "![diagram](diagram.png)<!-- cfxmark:img w=640 h=360 thumbnail=1 align=right -->"
        '<!-- cfxmark:asset src="diagram.png" -->\n\n'
        "Centered paragraph<!-- cfxmark:align=center -->\n"
    )

    storage = cfxmark.to_cfx(markdown, options=options("editable")).xhtml

    assert storage is not None
    assert "--ds-text-accent-red" in storage
    assert "--ds-background-accent-blue-subtlest" in storage
    assert 'ac:width="640"' in storage
    assert 'ac:height="360"' in storage
    assert 'ac:thumbnail="true"' in storage
    assert 'ac:align="right"' in storage
    assert 'style="text-align: center;"' in storage

    markdown_2 = cfxmark.to_md(storage, options=options("editable")).markdown
    assert markdown_2 == markdown


def test_colspan_and_rowspan_have_independent_round_trip_coverage() -> None:
    storage = (
        "<table><tbody>"
        '<tr><th colspan="2">Header</th></tr>'
        '<tr><td rowspan="2">Tall</td><td>One</td></tr>'
        "<tr><td>Two</td></tr>"
        "</tbody></table>"
    )

    markdown_1 = cfxmark.to_md(storage, options=options("editable")).markdown
    assert markdown_1 is not None
    assert "| Header | < |" in markdown_1
    assert "| ^ | Two |" in markdown_1

    storage_1 = cfxmark.to_cfx(markdown_1, options=options("editable")).xhtml
    assert storage_1 is not None
    assert 'colspan="2"' in storage_1
    assert 'rowspan="2"' in storage_1

    markdown_2 = cfxmark.to_md(storage_1, options=options("editable")).markdown
    assert markdown_2 == markdown_1


def test_rowspan_in_middle_column_does_not_shift_on_reparse() -> None:
    storage = (
        "<table><tbody>"
        "<tr><th>Left</th><th>Middle</th><th>Right</th></tr>"
        '<tr><td>One</td><td rowspan="2">Tall</td><td>Three</td></tr>'
        "<tr><td>Four</td><td>Six</td></tr>"
        "</tbody></table>"
    )

    markdown_1 = cfxmark.to_md(storage, options=options("editable")).markdown
    assert markdown_1 is not None
    assert "| Four | ^ | Six |" in markdown_1

    storage_1 = cfxmark.to_cfx(markdown_1, options=options("editable")).xhtml
    assert storage_1 is not None
    markdown_2 = cfxmark.to_md(storage_1, options=options("editable")).markdown

    assert markdown_2 == markdown_1


def test_table_cell_alignment_marker_stays_with_its_segment() -> None:
    storage = (
        "<table><tbody><tr><th>Header</th></tr><tr><td>"
        '<p style="text-align: center;">Centered</p><p>Plain</p>'
        "</td></tr></tbody></table>"
    )

    markdown = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown is not None
    assert "Centered<!-- cfxmark:align=center --><br>Plain" in markdown
    storage_2 = cfxmark.to_cfx(markdown, options=options("editable")).xhtml
    assert storage_2 is not None
    assert '<p style="text-align: center;">Centered</p><p>Plain</p>' in storage_2


def test_header_rowspan_keeps_a_valid_gfm_header_and_round_trips() -> None:
    storage = (
        "<table><thead>"
        '<tr><th rowspan="2">Category</th><th>Primary</th></tr>'
        "<tr><th>Secondary</th></tr>"
        "</thead><tbody><tr><td>A</td><td>B</td></tr></tbody></table>"
    )

    markdown_1 = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown_1 == ("| Category | Primary |\n| --- | --- |\n| ^ | Secondary |\n| A | B |\n")
    storage_1 = cfxmark.to_cfx(markdown_1, options=options("editable")).xhtml
    assert storage_1 is not None
    assert '<th rowspan="2">Category</th>' in storage_1
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == markdown_1


@pytest.mark.parametrize("attribute", ["colspan", "rowspan"])
def test_oversized_table_span_fails_closed_before_logical_grid_expansion(attribute: str) -> None:
    storage = f'<table><tbody><tr><td {attribute}="1001">X</td></tr></tbody></table>'

    artifact = cfxmark.to_md_artifact(storage, options=options("editable"))

    assert artifact.status == "needs_migration"
    assert artifact.push_safe is False
    assert any(diagnostic.code == "table-cell-span-limit-exceeded" for diagnostic in artifact.diagnostics)
    assert artifact.presentation.addresses == ()


def test_wide_table_grid_amplification_fails_closed_before_rectangular_allocation() -> None:
    rows = "".join("<tr><td>X</td></tr>" for _ in range(10))
    storage = f'<table><thead><tr><th colspan="1000">H</th></tr></thead><tbody>{rows}</tbody></table>'

    artifact = cfxmark.to_md_artifact(storage, options=options("editable"))

    assert artifact.status == "needs_migration"
    assert any(diagnostic.code == "table-cell-span-limit-exceeded" for diagnostic in artifact.diagnostics)
    assert artifact.presentation.addresses == ()
    assert len(artifact.markdown) < len(storage) * 4


def test_bounded_grid_width_counts_columns_shifted_by_inherited_rowspan() -> None:
    rows = (
        TableRow(cells=(TableCell(kind=CellType.DATA, children=(Text(content="A"),), rowspan=2),)),
        TableRow(cells=(TableCell(kind=CellType.DATA, children=(Text(content="B"),)),)),
    )

    assert bounded_table_grid_width(rows) == 2


def test_overlapping_colspan_and_inherited_rowspan_fails_closed() -> None:
    storage = (
        "<table><thead><tr><th>H1</th><th>H2</th></tr></thead><tbody>"
        '<tr><td>A</td><td rowspan="2">B</td></tr>'
        '<tr><td colspan="2">C</td></tr>'
        "</tbody></table>"
    )

    artifact = cfxmark.to_md_artifact(storage, options=options("editable"))

    assert artifact.status == "needs_migration"
    assert artifact.presentation.addresses == ()
    assert any(diagnostic.code == "table-cell-span-overlap" for diagnostic in artifact.diagnostics)


def test_alignment_columns_follow_shared_inherited_rowspan_placement() -> None:
    storage = (
        "<table><thead><tr>"
        '<th style="text-align:left">H1</th><th style="text-align:right">H2</th>'
        "</tr></thead><tbody>"
        '<tr><td rowspan="2" style="text-align:left">A</td><td style="text-align:right">B</td></tr>'
        '<tr><td style="text-align:right">C</td></tr>'
        "</tbody></table>"
    )

    artifact = cfxmark.to_md_artifact(storage, options=options("editable"))

    assert artifact.status == "ready_to_edit"
    assert "| :--- | ---: |" in artifact.markdown
    assert not any(diagnostic.code == "table-column-alignment-mixed" for diagnostic in artifact.diagnostics)


def test_aligned_paragraph_with_hard_break_round_trips_as_one_paragraph() -> None:
    storage = '<p style="text-align: center;">A<br/>B</p>'

    markdown_1 = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown_1 == "A<br>B<!-- cfxmark:align=center -->\n"
    storage_1 = cfxmark.to_cfx(markdown_1, options=options("editable")).xhtml
    assert storage_1 == '<p style="text-align: center;">A<br/>B</p>'
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == markdown_1


def test_markdown_two_space_hard_break_canonicalizes_once_to_br() -> None:
    markdown = "A  \nB\n"

    canonical = cfxmark.normalize_md(markdown)

    assert canonical == "A<br>B\n"
    storage = cfxmark.to_cfx(canonical, options=options("editable")).xhtml
    assert storage == "<p>A<br/>B</p>"
    assert cfxmark.to_md(storage, options=options("editable")).markdown == canonical


def test_table_cell_hard_break_uses_the_same_canonical_br() -> None:
    storage = "<table><thead><tr><th>Header</th></tr></thead><tbody><tr><td>A<br/>B</td></tr></tbody></table>"

    markdown = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown == "| Header |\n| --- |\n| A<br>B |\n"
    assert cfxmark.to_cfx(markdown, options=options("editable")).xhtml == storage


def test_code_and_pre_content_is_not_rewritten_as_hard_break_markup() -> None:
    storage = "<pre>A  \nB&lt;br&gt;</pre>"

    markdown = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown == "```\nA  \nB<br>\n```\n"
    rendered = cfxmark.to_cfx(markdown, options=options("editable")).xhtml
    assert rendered is not None
    assert "<![CDATA[A  \nB<br>]]>" in rendered


def test_nbsp_adjacent_to_hard_break_is_preserved_as_content() -> None:
    storage = "<p>A\u00a0<br/>B</p>"

    markdown = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown == "A\u00a0<br>B\n"
    rendered = cfxmark.to_cfx(markdown, options=options("editable")).xhtml
    assert rendered == storage


def test_orphan_alignment_marker_is_pruned() -> None:
    markdown = "<!-- cfxmark:align=center -->\n"

    assert cfxmark.normalize_md(markdown) == "\n"
    assert cfxmark.to_cfx(markdown, options=options("editable")).xhtml == ""


def test_orphan_alignment_marker_does_not_attach_to_the_next_table_cell() -> None:
    markdown = "| Empty | Text |\n| --- | --- |\n| <!-- cfxmark:align=center --> | Next |\n"

    storage = cfxmark.to_cfx(markdown, options=options("editable")).xhtml

    assert storage is not None
    assert "text-align" not in storage
    assert "<td/><td>Next</td>" in storage
    assert cfxmark.normalize_md(markdown) == ("| Empty | Text |\n| --- | --- |\n|  | Next |\n")


def test_unknown_foreground_color_is_diagnosed_and_preserved_exactly() -> None:
    storage = '<p><span style="color:#123456">State</span></p>'

    first = cfxmark.to_md(storage, options=options("editable"))

    assert first.markdown == '<span data-cfx-color="#123456">State</span>\n'
    assert first.losses == ()
    assert any("unknown foreground color" in warning for warning in first.warnings)
    storage_1 = cfxmark.to_cfx(first.markdown, options=options("editable")).xhtml
    assert storage_1 == '<p><span style="color:#123456">State</span></p>'
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == first.markdown


def test_unknown_background_color_is_diagnosed_and_preserved_exactly() -> None:
    storage = '<p><span style="background-color:#123456">State</span></p>'

    first = cfxmark.to_md(storage, options=options("editable"))

    assert first.markdown == '<mark data-cfx-color="#123456">State</mark>\n'
    assert first.losses == ()
    assert any("unknown background color" in warning for warning in first.warnings)
    storage_1 = cfxmark.to_cfx(first.markdown, options=options("editable")).xhtml
    assert storage_1 == '<p><span style="background-color:#123456">State</span></p>'
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == first.markdown


def test_color_names_are_classified_by_token_not_substring() -> None:
    storage = '<p><span style="color:orangered">State</span></p>'

    result = cfxmark.to_md(storage, options=options("editable"))

    assert result.markdown == '<span data-cfx-color="orangered">State</span>\n'
    assert any("unknown foreground color" in warning for warning in result.warnings)


def test_inverse_foreground_color_has_a_stable_semantic_label() -> None:
    storage = '<p><span style="color:var(--ds-text-inverse, #ffffff)">Inverse</span></p>'

    first = cfxmark.to_md(storage, options=options("editable"))

    assert first.markdown == '<span data-cfx-color="inverse">Inverse</span>\n'
    assert first.warnings == ()
    storage_1 = cfxmark.to_cfx(first.markdown, options=options("editable")).xhtml
    assert storage_1 is not None
    assert "--ds-text-inverse" in storage_1
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == first.markdown


def test_neutral_background_normalizes_to_gray_once() -> None:
    markdown = '<mark data-cfx-color="neutral">Background</mark>\n'

    storage = cfxmark.to_cfx(markdown, options=options("editable")).xhtml

    assert storage is not None
    assert cfxmark.to_md(storage, options=options("editable")).markdown == (
        '<mark data-cfx-color="gray">Background</mark>\n'
    )


def test_background_hex_uses_known_palette_and_is_fixed_point() -> None:
    storage = '<p><span style="background-color:#ffeceb">Alert</span></p>'

    first = cfxmark.to_md(storage, options=options("editable"))

    assert first.markdown == '<mark data-cfx-color="red">Alert</mark>\n'
    assert first.warnings == ()
    storage_1 = cfxmark.to_cfx(first.markdown, options=options("editable")).xhtml
    assert storage_1 is not None
    assert "--ds-background-accent-red-subtlest" in storage_1
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == first.markdown


def test_foreground_and_background_colors_both_survive() -> None:
    storage = '<p><span style="color:#123456;background-color:#ffeceb">Both</span></p>'

    first = cfxmark.to_md(storage, options=options("editable"))

    assert first.markdown == ('<mark data-cfx-color="red"><span data-cfx-color="#123456">Both</span></mark>\n')
    assert any("unknown foreground color" in warning for warning in first.warnings)
    storage_1 = cfxmark.to_cfx(first.markdown, options=options("editable")).xhtml
    assert storage_1 is not None
    assert "background-color:" in storage_1
    assert "color:#123456" in storage_1
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == first.markdown


def test_adjacent_identical_color_spans_are_coalesced() -> None:
    storage = '<p><span style="color:#123456">A</span><span style="color:#123456">B</span></p>'

    result = cfxmark.to_md(storage, options=options("editable"))

    assert result.markdown == '<span data-cfx-color="#123456">AB</span>\n'


def test_unsupported_marker_is_fixed_point_and_cannot_render_to_storage() -> None:
    # A body-carrying macro: parameter-only macros now round-trip through the
    # managed opaque payload transport and no longer produce this marker.
    storage = (
        '<p>Before <ac:structured-macro ac:name="widget">'
        '<ac:parameter ac:name="mode">full</ac:parameter>'
        "<ac:rich-text-body><p>inner</p></ac:rich-text-body>"
        "</ac:structured-macro> after</p>"
    )

    result = cfxmark.to_md(storage, options=options("editable"))

    assert result.push_safe is False
    assert 'macro="widget"' in (result.markdown or "")
    assert cfxmark.normalize_md(result.markdown or "") == result.markdown
    with pytest.raises(cfxmark.ConversionError, match="unsupported Confluence content"):
        cfxmark.to_cfx(result.markdown or "", options=options("editable"))


def test_block_unsupported_marker_is_fixed_point_and_names_the_macro() -> None:
    storage = (
        '<ac:structured-macro ac:name="widget"><ac:parameter ac:name="mode">full</ac:parameter></ac:structured-macro>'
    )

    result = cfxmark.to_md(storage, options=options("editable"))

    assert result.push_safe is False
    assert result.markdown is not None
    assert "> \\[Unsupported Confluence content\\]" in result.markdown
    assert 'kind=block macro="widget" push-safe=false' in result.markdown
    assert cfxmark.normalize_md(result.markdown) == result.markdown
    with pytest.raises(cfxmark.ConversionError, match="unsupported Confluence content"):
        cfxmark.to_cfx(result.markdown, options=options("editable"))


def test_boundary_nbsp_survives_heading_list_and_blockquote() -> None:
    storage = (
        "<h1>&#160;Title&#160;</h1>"
        "<ul><li><p>&#160;Item&#160;</p></li></ul>"
        "<blockquote><p>&#160;Quote&#160;</p></blockquote>"
    )

    markdown_1 = cfxmark.to_md(storage, options=options("editable")).markdown

    assert markdown_1 == ("# &nbsp;Title&nbsp;\n\n- &nbsp;Item&nbsp;\n\n> &nbsp;Quote&nbsp;\n")
    storage_1 = cfxmark.to_cfx(markdown_1, options=options("editable")).xhtml
    assert storage_1 is not None
    assert storage_1.count("\u00a0") == 6
    assert cfxmark.to_md(storage_1, options=options("editable")).markdown == markdown_1
