from __future__ import annotations

import pytest

import cfxmark
from cfxmark.presentation import parse_cell_background, parse_color


def _verified_highlight_options() -> cfxmark.ConversionOptions:
    capability = cfxmark.ColorCapability(
        capability_id="synthetic-readback-highlight-v1",
        source_encoding="data-highlight-colour",
        value_kind="hex",
        render_strategy="highlight-attribute",
    )
    return cfxmark.ConversionOptions(
        profile="editable",
        color_capabilities=cfxmark.ColorCapabilityMatrix((capability,)),
    )


@pytest.mark.parametrize(
    ("source", "kind", "equality_prefix"),
    [
        ("red", "named", "named:"),
        ("#f00", "hex", "rgba:"),
        ("rgb(255,0,0)", "rgb", "rgba:"),
        ("rgba(255,0,0,0.5)", "rgba", "rgba:"),
        ("var(--ds-background-accent-red-subtlest)", "design-token", "token:"),
    ],
)
def test_table_background_grammar_preserves_exact_meaning(source: str, kind: str, equality_prefix: str) -> None:
    exact, equality_key, value_kind, _semantic = parse_color(source)

    assert exact
    assert value_kind == kind
    assert equality_key.startswith(equality_prefix)


def test_conflicting_background_encodings_fail_closed() -> None:
    background, losses = parse_cell_background(
        {"data-highlight-colour": "#ff0000", "style": "background-color:#00ff00"}
    )

    assert background is None
    assert losses == ("table cell background sources conflict",)


def test_verified_background_survives_cell_text_edit_and_fixed_point() -> None:
    options = _verified_highlight_options()
    storage = (
        "<table><thead><tr><th>H</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#ff0000">X</td></tr></tbody></table>'
    )
    base = cfxmark.to_md_artifact(storage, options=options)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("X", "Y"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=options,
    )
    again = cfxmark.to_md_artifact(candidate.xhtml, options=options)

    assert base.status == "ready_to_edit"
    presentation_cell = base.presentation.cells[0]
    occurrence_id = cfxmark.presentation_cell_occurrence_id(
        presentation_cell.table_id,
        presentation_cell.topology_hash,
        presentation_cell.anchor_row_index,
        presentation_cell.anchor_column_index,
    )
    assert any(
        entry.source_kind == "presentation" and entry.node_or_occurrence_id == occurrence_id
        for entry in base.source_map_entries
    )
    assert any(entry.source_kind == "presentation" for entry in candidate.source_map_entries)
    assert any(
        entry.source_kind == "presentation" and entry.node_or_occurrence_id == occurrence_id
        for entry in candidate.source_map_entries
    )
    assert candidate.push_safe is True
    assert 'data-highlight-colour="#ff0000"' in candidate.xhtml
    assert ">Y</td>" in candidate.xhtml
    assert again.presentation.cells[0].background.equality_key == base.presentation.cells[0].background.equality_key
    cfxmark.validate_managed_cfx_artifact(
        candidate,
        source_storage=storage,
        options=options,
    )


def test_css_background_and_markdown_alignment_edit_is_blocked_in_0_3() -> None:
    capability = cfxmark.ColorCapability(
        capability_id="synthetic-readback-style-v1",
        source_encoding="css-background-color",
        value_kind="hex",
        render_strategy="style-background-color",
    )
    options = cfxmark.ConversionOptions(
        profile="editable",
        color_capabilities=cfxmark.ColorCapabilityMatrix((capability,)),
    )
    storage = (
        '<table><thead><tr><th style="text-align:left">H</th></tr></thead><tbody><tr>'
        '<td style="text-align:left;background-color:#ff0000">X</td></tr></tbody></table>'
    )
    base = cfxmark.to_md_artifact(storage, options=options)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("| :--- |", "| :---: |"),
        presentation=base.presentation,
        base_artifact=base,
        options=options,
    )

    assert base.status == "ready_to_edit"
    assert any(
        entry.source_kind == "ast-node" and any(leaf.attribute_name == "style:text-align" for leaf in entry.leaves)
        for entry in candidate.source_map_entries
    )
    assert any(
        entry.source_kind == "presentation"
        and any(leaf.attribute_name == "style:background-color" for leaf in entry.leaves)
        for entry in candidate.source_map_entries
    )
    assert candidate.push_safe is False
    assert any(item.code == "table-style-edit-not-supported" for item in candidate.diagnostics)
    assert "text-align:left" in candidate.xhtml
    assert "background-color:#ff0000" in candidate.xhtml


def test_explicit_left_alignment_survives_css_background_fixed_point() -> None:
    capability = cfxmark.ColorCapability(
        capability_id="synthetic-readback-style-v1",
        source_encoding="css-background-color",
        value_kind="hex",
        render_strategy="style-background-color",
    )
    options = cfxmark.ConversionOptions(
        profile="editable",
        color_capabilities=cfxmark.ColorCapabilityMatrix((capability,)),
    )
    storage = (
        '<table><thead><tr><th style="text-align:left">H</th></tr></thead><tbody><tr>'
        '<td style="text-align:left;background-color:#ff0000">X</td></tr></tbody></table>'
    )
    base = cfxmark.to_md_artifact(storage, options=options)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        options=options,
    )

    assert "| :--- |" in base.markdown
    assert "text-align:left" in candidate.xhtml
    assert "background-color:#ff0000" in candidate.xhtml
    assert candidate.push_safe is True


def test_mixed_per_cell_alignment_is_remote_preserved() -> None:
    capability = cfxmark.ColorCapability(
        capability_id="synthetic-readback-style-v1",
        source_encoding="css-background-color",
        value_kind="hex",
        render_strategy="style-background-color",
    )
    options = cfxmark.ConversionOptions(
        profile="editable",
        color_capabilities=cfxmark.ColorCapabilityMatrix((capability,)),
    )
    storage = (
        "<table><thead><tr><th>H</th></tr></thead><tbody><tr>"
        '<td style="text-align:left;background-color:#ff0000">X</td></tr></tbody></table>'
    )

    artifact = cfxmark.to_md_artifact(storage, options=options)

    assert artifact.status == "ready_to_edit"
    candidate = cfxmark.to_cfx_artifact(
        artifact.markdown,
        presentation=artifact.presentation,
        base_artifact=artifact,
        splice_source=storage,
        options=options,
    )
    assert candidate.push_safe is True
    assert 'style="text-align:left;background-color:#ff0000"' in candidate.xhtml


@pytest.mark.parametrize("source_align", ["LEFT", "Left", "CENTER", "Right"])
def test_table_alignment_leaf_fingerprint_uses_case_insensitive_typed_value(source_align: str) -> None:
    storage = (
        f'<table><thead><tr><th style="text-align:{source_align}">H</th></tr></thead><tbody><tr>'
        f'<td style="text-align:{source_align}">X</td></tr></tbody></table><p>Z</p>'
    )
    base = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable"))
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Z", "Q"),
        presentation=base.presentation,
        base_artifact=base,
        options=cfxmark.ConversionOptions(profile="editable"),
    )

    def alignment_fingerprints(artifact: cfxmark.MarkdownArtifact | cfxmark.CfxArtifact) -> set[str]:
        return {
            leaf.value_fingerprint
            for entry in artifact.source_map_entries
            if entry.source_kind == "ast-node"
            for leaf in entry.leaves
            if leaf.attribute_name == "style:text-align"
        }

    assert base.status == "ready_to_edit"
    assert alignment_fingerprints(base) == alignment_fingerprints(candidate)
    assert ">Q</p>" in candidate.xhtml


def test_markdown_authoritative_tables_allow_column_and_alignment_rewrites_without_losing_macro_identity() -> None:
    storage = (
        "<p>Before</p>"
        '<ac:structured-macro ac:name="jira" ac:schema-version="1" ac:macro-id="macro-1">'
        '<ac:parameter ac:name="jqlQuery">key = DEMO-1</ac:parameter>'
        "</ac:structured-macro>"
        '<table class="confluenceTable"><thead><tr>'
        '<th class="confluenceTh" style="text-align:right">First</th>'
        '<th class="confluenceTh" style="text-align:right">Second</th>'
        "</tr></thead><tbody><tr>"
        '<td class="confluenceTd" style="text-align:right">A</td>'
        '<td class="confluenceTd" style="text-align:right">B</td>'
        "</tr></tbody></table>"
        '<table class="confluenceTable"><thead><tr>'
        '<th class="confluenceTh" style="text-align:right">Third</th>'
        '<th class="confluenceTh" style="text-align:right">Fourth</th>'
        "</tr></thead><tbody><tr>"
        '<td class="confluenceTd" style="text-align:right">C</td>'
        '<td class="confluenceTd" style="text-align:right">D</td>'
        "</tr></tbody></table>"
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(storage, options=options)
    edited = (
        base.markdown.replace("| First | Second |", "| First | Second | Speed |")
        .replace("| ---: | ---: |", "| :--- | :---: | ---: |")
        .replace("| A | B |", "| A | B | 1 |")
        .replace("| Third | Fourth |", "| Third | Fourth | Speed |")
        .replace("| C | D |", "| C | D | 2 |")
    )

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=options,
    )

    assert candidate.ownership_markdown_authoritative_tables is True
    assert candidate.push_safe is True
    assert 'ac:macro-id="macro-1"' in candidate.xhtml
    assert candidate.xhtml.count("<table") == 2
    assert 'style="text-align: left;"' in candidate.xhtml
    assert 'style="text-align: center;"' in candidate.xhtml
    assert 'style="text-align: right;"' in candidate.xhtml
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=options)


@pytest.mark.parametrize(
    "storage",
    [
        (
            '<table><colgroup><col style="width:50%"/></colgroup><thead><tr>'
            '<th style="text-align:right">H</th></tr></thead><tbody><tr>'
            '<td style="text-align:right">X</td></tr></tbody></table>'
        ),
        (
            '<table><thead><tr><th style="text-align:right">H</th></tr></thead><tbody><tr>'
            '<td style="text-align:right;background-color:#ff0000">X</td>'
            "</tr></tbody></table>"
        ),
    ],
    ids=["column-width", "cell-background"],
)
def test_non_markdown_table_presentation_keeps_the_existing_fail_closed_proof(storage: str) -> None:
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(storage, options=options)
    edited = (
        base.markdown.replace("| H |", "| H | New |")
        .replace("| ---: |", "| ---: | ---: |")
        .replace("| X |", "| X | 1 |")
    )

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=options,
    )

    assert candidate.ownership_markdown_authoritative_tables is False
    assert candidate.push_safe is False


@pytest.mark.parametrize("source_align", ["LEFT !important", "left!important", "left /*x*/"])
def test_table_alignment_with_unsupported_css_suffix_is_remote_preserved(source_align: str) -> None:
    storage = (
        f'<table><thead><tr><th style="text-align:{source_align}">H</th></tr></thead><tbody><tr>'
        f'<td style="text-align:{source_align}">X</td></tr></tbody></table>'
    )

    artifact = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable"))

    assert artifact.status == "ready_to_edit"
    candidate = cfxmark.to_cfx_artifact(
        artifact.markdown,
        presentation=artifact.presentation,
        base_artifact=artifact,
        splice_source=storage,
        options=cfxmark.ConversionOptions(profile="editable"),
    )
    assert candidate.push_safe is True
    assert f"text-align:{source_align}" in candidate.xhtml


@pytest.mark.parametrize(
    ("attribute", "source_encoding", "value_kind", "render_strategy"),
    [
        ('style="background-color:#FF0000"', "css-background-color", "hex", "style-background-color"),
        ('data-highlight-colour="#FF0000"', "data-highlight-colour", "hex", "highlight-attribute"),
        ('data-highlight-colour="Green"', "data-highlight-colour", "named", "highlight-attribute"),
    ],
)
def test_presentation_leaf_fingerprint_uses_typed_color_meaning(
    attribute: str,
    source_encoding: str,
    value_kind: str,
    render_strategy: str,
) -> None:
    capability = cfxmark.ColorCapability(
        capability_id="synthetic-color-spelling-v1",
        source_encoding=source_encoding,  # type: ignore[arg-type]
        value_kind=value_kind,  # type: ignore[arg-type]
        render_strategy=render_strategy,  # type: ignore[arg-type]
    )
    options = cfxmark.ConversionOptions(
        profile="editable",
        color_capabilities=cfxmark.ColorCapabilityMatrix((capability,)),
    )
    storage = f"<table><thead><tr><th>H</th></tr></thead><tbody><tr><td {attribute}>X</td></tr></tbody></table>"
    base = cfxmark.to_md_artifact(storage, options=options)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("X", "Y"),
        presentation=base.presentation,
        base_artifact=base,
        options=options,
    )

    def presentation_fingerprints(artifact: cfxmark.MarkdownArtifact | cfxmark.CfxArtifact) -> set[tuple[str, str]]:
        return {
            (leaf.attribute_name or "", leaf.value_fingerprint)
            for entry in artifact.source_map_entries
            if entry.source_kind == "presentation"
            for leaf in entry.leaves
        }

    assert base.status == "ready_to_edit"
    assert presentation_fingerprints(base) == presentation_fingerprints(candidate)
    assert ">Y</td>" in candidate.xhtml


def test_table_reorder_keeps_background_by_unique_identity() -> None:
    options = _verified_highlight_options()
    storage = (
        "<table><thead><tr><th>H1</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#ff0000">A</td></tr></tbody></table>'
        "<table><thead><tr><th>H2</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#00ff00">B</td></tr></tbody></table>'
    )
    green = cfxmark.ColorCapability(
        capability_id="synthetic-readback-highlight-v1",
        source_encoding="data-highlight-colour",
        value_kind="hex",
        render_strategy="highlight-attribute",
    )
    options = cfxmark.ConversionOptions(profile="editable", color_capabilities=cfxmark.ColorCapabilityMatrix((green,)))
    base = cfxmark.to_md_artifact(storage, options=options)
    first, second = base.markdown.strip().split("\n\n")

    candidate = cfxmark.to_cfx_artifact(
        f"{second}\n\n{first}\n",
        presentation=base.presentation,
        base_artifact=base,
        options=options,
    )

    assert candidate.push_safe is True
    assert candidate.xhtml.index("#00ff00") < candidate.xhtml.index("#ff0000")


def test_duplicate_table_identity_is_blocking_not_first_match() -> None:
    options = _verified_highlight_options()
    table = (
        "<table><thead><tr><th>H</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#ff0000">X</td></tr></tbody></table>'
    )

    artifact = cfxmark.to_md_artifact(table + table, options=options)

    assert artifact.status == "needs_migration"
    assert any(diagnostic.code == "table-presentation-ambiguous" for diagnostic in artifact.diagnostics)
    assert len({address.table_id for address in artifact.presentation.addresses}) == 2
    assert len(artifact.presentation.ambiguous_table_ids) == 2


def test_unstyled_duplicate_tables_have_distinct_state_keys_and_still_fail_closed() -> None:
    table = "<table><tbody><tr><td>X</td></tr></tbody></table>"

    artifact = cfxmark.to_md_artifact(table + table, options=cfxmark.ConversionOptions(profile="editable"))

    assert artifact.status == "needs_migration"
    assert len({address.table_id for address in artifact.presentation.addresses}) == 2
    assert len(artifact.presentation.ambiguous_table_ids) == 2
    assert any(diagnostic.code == "table-presentation-ambiguous" for diagnostic in artifact.diagnostics)


def test_merged_cell_presentation_is_recorded_only_on_anchor() -> None:
    options = _verified_highlight_options()
    storage = (
        "<table><thead><tr><th>H1</th><th>H2</th></tr></thead><tbody><tr>"
        '<td colspan="2" data-highlight-colour="#ff0000">X</td></tr></tbody></table>'
    )

    artifact = cfxmark.to_md_artifact(storage, options=options)

    assert [(cell.anchor_row_index, cell.anchor_column_index) for cell in artifact.presentation.cells] == [(1, 0)]
    assert [
        (
            address.logical_row_index,
            address.logical_column_index,
            address.anchor_row_index,
            address.anchor_column_index,
            address.is_anchor,
        )
        for address in artifact.presentation.addresses
    ] == [
        (0, 0, 0, 0, True),
        (0, 1, 0, 1, True),
        (1, 0, 1, 0, True),
        (1, 1, 1, 0, False),
    ]


def test_unstyled_table_cells_still_have_logical_anchor_addresses() -> None:
    artifact = cfxmark.to_md_artifact(
        "<table><tbody><tr><td>A</td><td>B</td></tr></tbody></table>",
        options=cfxmark.ConversionOptions(profile="editable"),
    )

    assert artifact.presentation.cells == ()
    assert [
        (address.logical_row_index, address.logical_column_index, address.is_anchor)
        for address in artifact.presentation.addresses
    ] == [(0, 0, True), (0, 1, True)]
