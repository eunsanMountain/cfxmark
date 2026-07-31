from __future__ import annotations

from dataclasses import replace

import pytest

import cfxmark
import cfxmark.protected as protected
from cfxmark.opaque import opaque_id_for
from cfxmark.storage_map import extract_storage_source, storage_leaf_identity

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def test_opaque_macro_is_marker_free_and_preserved_during_unrelated_edit() -> None:
    storage = (
        "<p>Before</p>"
        '<ac:structured-macro ac:name="widget" ac:macro-id="macro-1">'
        '<ac:parameter ac:name="mode">exact</ac:parameter>'
        "</ac:structured-macro>"
        "<p>After</p>"
    )

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert base.push_safe is True
    assert len(base.protected_regions) == 1
    assert "cfxmark:unsupported" not in base.markdown
    assert "cfxmark:payloads" not in base.markdown
    assert "cfx-storage" not in base.markdown
    edited = base.markdown.replace("Before", "Before edited")
    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.preserved_regions == (base.protected_regions[0].occurrence_id,)
    assert 'ac:macro-id="macro-1"' in candidate.xhtml
    assert '<ac:parameter ac:name="mode">exact</ac:parameter>' in candidate.xhtml
    assert "Before edited" in candidate.xhtml


def test_editing_protected_projection_blocks_before_write() -> None:
    storage = '<p>Before</p><ac:structured-macro ac:name="widget" ac:macro-id="macro-1"/><p>After</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    edited = base.markdown.replace("Unsupported Confluence content", "Changed protected content")

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "protected-region-edited" for item in candidate.diagnostics)
    assert candidate.protected_region_changes[0].kind in {"edit", "ambiguous"}


@pytest.mark.parametrize("destination", ["start", "end"])
def test_moving_protected_projection_is_blocked_by_semantic_matcher(destination: str) -> None:
    storage = '<p>Before</p><ac:structured-macro ac:name="widget"/><p>After</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    projection = base.protected_regions[0].display_label
    edited = f"{projection}\n\nBefore\n\nAfter\n" if destination == "start" else f"Before\n\nAfter\n\n{projection}\n"

    candidate = cfxmark.to_cfx_artifact(
        edited,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.protected_region_changes[0].kind == "move"
    assert any(item.code == "protected-region-edited" for item in candidate.diagnostics)


def test_inline_page_link_projection_is_marker_free_and_remote_backed() -> None:
    storage = (
        "<p>See "
        '<ac:link><ri:page ri:content-title="Roadmap"/>'
        "<ac:plain-text-link-body><![CDATA[Roadmap]]></ac:plain-text-link-body></ac:link>"
        " now.</p>"
    )

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert "Roadmap" in base.markdown
    assert "cfx:" not in base.markdown
    assert "cfxmark:unsupported" not in base.markdown
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("now", "today"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert '<ri:page ri:content-title="Roadmap"/>' in candidate.xhtml
    assert "today" in candidate.xhtml


def test_standalone_date_projection_is_readable_and_remote_backed() -> None:
    storage = '<p>Planned <time datetime="2025-03-20" /> date</p><p>After</p>'

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert "Planned [2025-03-20]{cfx:op-" in base.markdown
    assert "} date" in base.markdown
    assert "<!-- cfxmark:payloads -->" in base.markdown
    assert base.protected_regions == ()
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "After edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.splice_proof_complete is True
    assert '<time datetime="2025-03-20"/>' in candidate.xhtml
    assert "After edited" in candidate.xhtml


def test_authenticated_opaque_atoms_are_publishable_in_current_page_preflight() -> None:
    storages = (
        '<p>Planned <time datetime="2025-03-20"/> date</p>',
        '<p><ac:link><ri:user ri:userkey="user-1"/></ac:link></p>',
        '<p><ac:link><ri:attachment ri:filename="asset-01.png"/></ac:link></p>',
        (
            "<table><tbody><tr><td><p><ac:link><ri:attachment "
            'ri:filename="asset-01.png"/></ac:link></p></td></tr></tbody></table>'
        ),
    )

    for storage in storages:
        result = cfxmark.to_md(storage, options=EDITABLE)
        assert result.push_safe
        assert result.losses == ()


def test_table_div_without_an_authenticated_link_remains_loss_bearing() -> None:
    storage = '<table><tbody><tr><td><div class="custom">unmapped</div></td></tr></tbody></table>'

    result = cfxmark.to_md(storage, options=EDITABLE)

    assert not result.push_safe
    assert "unsupported Confluence content omitted from editable Markdown" in result.losses


def test_inline_projection_repeated_inside_table_island_does_not_create_false_ambiguity() -> None:
    storage = (
        "<p>See "
        '<ac:link><ri:page ri:content-title="Roadmap"/>'
        "<ac:plain-text-link-body><![CDATA[Roadmap]]></ac:plain-text-link-body></ac:link>"
        " now.</p>"
        '<table class="confluenceTable" style="width:100%">'
        '<colgroup><col style="width:100%"/></colgroup><tbody>'
        '<tr><th class="confluenceTh"><p><ac:link><ri:page ri:content-title="Roadmap"/>'
        "<ac:plain-text-link-body><![CDATA[Roadmap]]></ac:plain-text-link-body></ac:link></p></th></tr>"
        '<tr><td class="confluenceTd"><p>Value</p></td></tr>'
        "</tbody></table><p>After</p>"
    )

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert tuple(region.kind for region in base.protected_regions) == ("inline", "table-island")
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "After edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert '<ri:page ri:content-title="Roadmap"/>' in candidate.xhtml
    assert '<colgroup><col style="width:100%"/></colgroup>' in candidate.xhtml
    assert "After edited" in candidate.xhtml

    protected_edit = cfxmark.to_cfx_artifact(
        base.markdown.replace("See Roadmap", "See Changed", 1),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert protected_edit.push_safe is False
    assert any(item.code == "protected-region-edited" for item in protected_edit.diagnostics)


def test_protected_region_uses_fresh_remote_source_instead_of_tampered_base_document() -> None:
    storage = (
        "<p>See "
        '<ac:link><ri:page ri:content-title="Roadmap"/>'
        "<ac:plain-text-link-body><![CDATA[Roadmap]]></ac:plain-text-link-body></ac:link>"
        " now.</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    paragraph = base.document.children[0]
    inline = paragraph.children[1]
    injected_raw = inline.raw_xml.replace("Roadmap", "Injected")
    injected_inline = replace(inline, raw_xml=injected_raw, opaque_id=opaque_id_for(injected_raw))
    tampered_document = replace(
        base.document,
        children=(replace(paragraph, children=(paragraph.children[0], injected_inline, paragraph.children[2])),),
    )
    tampered_base = replace(base, document=tampered_document)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("now", "today"),
        base_artifact=tampered_base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert "Injected" not in candidate.xhtml
    assert '<ri:page ri:content-title="Roadmap"/>' in candidate.xhtml
    assert tuple(proof.occurrence_id for proof in candidate.protected_region_proofs) == candidate.preserved_regions
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)
    proof = candidate.protected_region_proofs[0]
    tampered_proof = replace(
        candidate,
        protected_region_proofs=(replace(proof, remote_subtree_fingerprint="sha256:" + "0" * 64),),
    )
    with pytest.raises(cfxmark.ConversionError, match="protected-region proof fingerprint"):
        cfxmark.validate_cfx_artifact(tampered_proof, source_storage=storage)


def test_splice_source_must_match_the_base_storage_digest() -> None:
    storage = '<p>A</p><ac:structured-macro ac:name="widget"/><p>B</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage + " ",
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "splice-source-mismatch" for item in candidate.diagnostics)


def test_headerless_colored_table_text_edit_preserves_remote_topology_and_style() -> None:
    storage = (
        "<table><tbody><tr>"
        '<td data-highlight-colour="#ffeeaa" style="text-align:center">A</td>'
        "<td>B</td>"
        "</tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    # Backgrounds are reported one atom per styled cell rather than as a count,
    # so this asserts the single styled cell and where it is.
    omissions = [item for item in base.diagnostics if item.code == "table-cell-background-omitted"]
    assert len(omissions) == 1
    assert omissions[0].location.table_row == 0
    assert omissions[0].location.table_column == 0
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("| A |", "| Edited |"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert "<thead>" not in candidate.xhtml
    assert "<th" not in candidate.xhtml
    assert 'data-highlight-colour="#ffeeaa"' in candidate.xhtml
    assert 'style="text-align:center"' in candidate.xhtml
    assert ">Edited</td>" in candidate.xhtml


def test_noncanonical_table_is_a_readable_protected_island() -> None:
    storage = (
        '<table class="confluenceTable" style="width:100%">'
        '<colgroup><col style="width:100%"/></colgroup><tbody>'
        '<tr><th class="confluenceTh" data-highlight-colour="#ffeeaa">'
        '<p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        '<tr><td class="confluenceTd"><p>Value</p></td></tr>'
        "</tbody></table><p>After</p><p><br/></p><p><br /></p>"
    )

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert base.push_safe is True
    assert base.unmapped_storage_leaves == ()
    assert len(base.protected_regions) == 1
    region = base.protected_regions[0]
    assert region.kind == "table-island"
    assert region.display_label == "Protected table at table[0]"
    assert "Header ✅" in base.markdown
    assert "Value" in base.markdown
    assert "cfxmark:" not in base.markdown
    assert not any(
        item.code in {"emoticon-to-unicode", "semantic-source-map-incomplete", "trailing-space-trimmed"}
        for item in base.diagnostics
    )

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "After edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.preserved_regions == (region.occurrence_id,)
    assert tuple(proof.occurrence_id for proof in candidate.protected_region_proofs) == candidate.preserved_regions
    assert candidate.splice_proof_complete is True
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)
    assert '<colgroup><col style="width:100%"/></colgroup>' in candidate.xhtml
    assert '<ac:emoticon ac:name="tick"/>' in candidate.xhtml
    assert 'data-highlight-colour="#ffeeaa"' in candidate.xhtml
    assert "After edited" in candidate.xhtml


def test_ragged_table_is_readable_but_remote_backed_during_outside_edit() -> None:
    """A row wider than the header must not pad every other row on publish.

    A live corpus page contains a mostly two-column table with one intentional four-cell
    row. GFM reparses it as a rectangular four-column table, so the readable
    projection is useful but cannot be the authority for that table's storage.
    """

    table = (
        "<table><thead><tr><th>key</th><th>value</th></tr></thead><tbody>"
        "<tr><td>a</td><td>b</td></tr>"
        "<tr><td>prefix</td><td>left</td><td>delta</td><td>right</td></tr>"
        "</tbody></table>"
    )
    storage = table + "<p>After</p>"

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert tuple(region.kind for region in base.protected_regions) == ("table-island",)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "After edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.push_safe is True
    assert table in candidate.xhtml
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)

    edited_table = cfxmark.to_cfx_artifact(
        base.markdown.replace("delta", "changed"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert edited_table.push_safe is False
    assert any(item.code == "protected-region-edited" for item in edited_table.diagnostics)


@pytest.mark.parametrize("change", ["edit", "move", "delete", "duplicate"])
def test_noncanonical_table_island_change_is_blocked(change: str) -> None:
    storage = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Value</p></td></tr></tbody></table><p>After</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    region = base.protected_regions[0]
    projection = base.markdown[region.base_markdown_range.start : region.base_markdown_range.end]
    if change == "edit":
        edited = base.markdown.replace("Value", "Changed", 1)
    elif change == "move":
        edited = base.markdown.replace(projection, "", 1).rstrip() + "\n\n" + projection + "\n"
    elif change == "delete":
        edited = base.markdown.replace(projection, "", 1)
    else:
        edited = base.markdown.rstrip() + "\n\n" + projection + "\n"

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "protected-region-edited" for item in candidate.diagnostics)
    assert candidate.protected_region_changes[0].kind in {"edit", "move", "delete", "ambiguous"}


def test_root_break_only_paragraph_is_preserved_during_unrelated_edit() -> None:
    storage = "<p>Before</p><p><br/></p><p>After</p>"

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Before", "Edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert base.unmapped_storage_leaves == ()
    assert candidate.push_safe is True
    assert candidate.xhtml == "<p>Edited</p><p><br/></p><p>After</p>"
    assert not any(
        item.code in {"semantic-source-map-incomplete", "managed-base-roundtrip-changed", "trailing-space-trimmed"}
        for item in base.diagnostics
    )


def test_attributed_root_break_paragraph_is_not_silently_collapsed() -> None:
    storage = '<p>Before</p><p class="meaningful"><br/></p><p>After</p>'

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Before", "Edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.xhtml == '<p>Edited</p><p class="meaningful"><br/></p><p>After</p>'


def test_duplicate_table_islands_use_ordered_identity_and_edit_fail_closed() -> None:
    table = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Same</p></td></tr></tbody></table>"
    )
    storage = table + "<p>Between</p>" + table

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    unchanged = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    edited = cfxmark.to_cfx_artifact(
        base.markdown.replace("Same", "Changed", 1),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert len(base.protected_regions) == 2
    assert unchanged.push_safe is True
    assert len(unchanged.protected_region_proofs) == 2
    assert edited.push_safe is False
    assert edited.protected_region_changes
    assert all(change.kind == "ambiguous" for change in edited.protected_region_changes)


def test_nested_table_inside_noncanonical_table_is_owned_by_outer_island() -> None:
    storage = (
        '<table class="confluenceTable"><thead><tr><th>Outer</th></tr></thead><tbody><tr><td>'
        "<table><tbody><tr><td>Nested</td></tr></tbody></table>"
        "</td></tr></tbody></table><p>After</p>"
    )

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "After edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert tuple(region.kind for region in base.protected_regions) == ("table-island",)
    assert candidate.push_safe is True
    assert candidate.splice_proof_complete is True
    assert "<table><tbody><tr><td>Nested</td></tr></tbody></table>" in candidate.xhtml


def test_protected_table_presentation_edit_is_blocked_and_not_reported_as_applied() -> None:
    storage = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th data-highlight-colour="#ffeeaa">Header</th></tr>'
        "<tr><td>Value</td></tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    colored = next(item for item in base.presentation.cells if item.background is not None)
    assert colored.background is not None
    changed_background = replace(colored.background, exact="#ff0000", equality_key="hex:#ff0000")
    changed_cell = replace(colored, background=changed_background)
    changed_presentation = replace(
        base.presentation,
        cells=tuple(changed_cell if item == colored else item for item in base.presentation.cells),
    )

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=changed_presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "table-style-edit-not-supported" for item in candidate.diagnostics)
    assert candidate.presentation == base.presentation
    assert 'data-highlight-colour="#ffeeaa"' in candidate.xhtml
    assert "#ff0000" not in candidate.xhtml


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("row", ["New | Row", "New | Row |", "| New | Row", "| New | Row |"])
def test_table_island_adjacent_row_insertion_is_blocked(position: str, row: str) -> None:
    storage = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Value</p></td></tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    region = base.protected_regions[0]
    projection = base.markdown[region.base_markdown_range.start : region.base_markdown_range.end]
    edited = f"{row}\n{projection}\n" if position == "before" else f"{projection}\n{row}\n"

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "protected-region-edited" for item in candidate.diagnostics)
    assert candidate.splice_proof_complete is False


def test_table_island_copy_inside_fence_does_not_create_identity_ambiguity() -> None:
    storage = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Value</p></td></tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    region = base.protected_regions[0]
    projection = base.markdown[region.base_markdown_range.start : region.base_markdown_range.end]

    candidate = cfxmark.to_cfx_artifact(
        f"{base.markdown}\n```md\n{projection}\n```\n",
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.splice_proof_complete is True


@pytest.mark.parametrize(
    "edited_template",
    [
        "```md\n{projection}\n```\n",
        "```md\n```notclose\n{projection}\n```\n",
        "~~~~md\n{projection}\n~~~~\n",
        "    {indented_projection}\n",
    ],
)
def test_table_island_projection_only_inside_code_is_blocked(edited_template: str) -> None:
    storage = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Value</p></td></tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    region = base.protected_regions[0]
    projection = base.markdown[region.base_markdown_range.start : region.base_markdown_range.end]
    indented_projection = projection.replace("\n", "\n    ")

    candidate = cfxmark.to_cfx_artifact(
        edited_template.format(projection=projection, indented_projection=indented_projection),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "protected-region-edited" for item in candidate.diagnostics)


@pytest.mark.parametrize("blank_separator, expected_safe", [("\n\n", True), ("\n", False)])
def test_table_island_requires_blank_boundary_before_a_second_table(blank_separator: str, expected_safe: bool) -> None:
    storage = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Header <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Value</p></td></tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    second_table = "| Other |\n| --- |\n| Added |\n"

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.rstrip() + blank_separator + second_table,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is expected_safe
    assert candidate.splice_proof_complete is expected_safe


def test_mixed_protected_and_editable_table_presentation_matches_rendered_storage() -> None:
    protected = (
        '<table class="confluenceTable"><colgroup><col/></colgroup><tbody>'
        '<tr><th><p>Protected <ac:emoticon ac:name="tick"/></p></th></tr>'
        "<tr><td><p>Keep</p></td></tr></tbody></table>"
    )
    editable = "<table><thead><tr><th>Editable</th></tr></thead><tbody><tr><td>Old</td></tr></tbody></table>"
    storage = protected + "<p>Between</p>" + editable
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Old", "New"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    read_back = cfxmark.to_md_artifact(candidate.xhtml, options=EDITABLE)

    assert candidate.push_safe is True
    assert "<td>New</td>" in candidate.xhtml
    assert candidate.presentation == read_back.presentation
    assert candidate.presentation.tables[0] == base.presentation.tables[0]
    assert candidate.presentation.tables[1] != base.presentation.tables[1]


def test_task_list_projects_to_gfm_and_allows_status_only_toggle() -> None:
    storage = (
        "<ac:task-list>"
        "<ac:task><ac:task-id>41</ac:task-id><ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>Keep this exact</ac:task-body></ac:task>"
        "<ac:task><ac:task-id>42</ac:task-id><ac:task-status>complete</ac:task-status>"
        "<ac:task-body><strong>Second task</strong></ac:task-body></ac:task>"
        "</ac:task-list>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert base.markdown == "- [ ] Keep this exact\n- [x] Second task\n"
    assert base.protected_regions[0].edit_policy == "task_status_only"
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("- [ ] Keep", "- [x] Keep"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert len(candidate.task_status_changes) == 1
    assert candidate.task_status_changes[0].before == "incomplete"
    assert candidate.task_status_changes[0].after == "complete"
    assert candidate.task_status_changes[0].task_index == 0
    assert "<ac:task-id>41</ac:task-id>" in candidate.xhtml
    assert "<ac:task-status>complete</ac:task-status>" in candidate.xhtml
    assert "<ac:task-body>Keep this exact</ac:task-body>" in candidate.xhtml


def test_task_text_edit_is_blocked() -> None:
    storage = (
        "<ac:task-list><ac:task><ac:task-id>41</ac:task-id>"
        "<ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>Keep this exact</ac:task-body></ac:task></ac:task-list>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Keep this exact", "Changed text"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert any(item.code == "protected-region-edited" for item in candidate.diagnostics)


def test_task_insert_or_reorder_is_ambiguous_instead_of_first_match() -> None:
    storage = (
        "<ac:task-list><ac:task><ac:task-id>41</ac:task-id>"
        "<ac:task-status>incomplete</ac:task-status>"
        "<ac:task-body>Same task</ac:task-body></ac:task></ac:task-list>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown + "- [ ] Same task\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.task_status_changes == ()
    assert candidate.protected_region_changes[0].kind == "ambiguous"


def test_duplicate_projections_preserve_by_order_during_unrelated_edit() -> None:
    storage = (
        "<p>Before</p>"
        '<ac:structured-macro ac:name="widget"><ac:parameter ac:name="value">one</ac:parameter></ac:structured-macro>'
        '<ac:structured-macro ac:name="widget"><ac:parameter ac:name="value">two</ac:parameter></ac:structured-macro>'
        "<p>After</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Before", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.xhtml.index(">one<") < candidate.xhtml.index(">two<")
    assert len(candidate.preserved_regions) == 2


def test_unmapped_attributes_on_unchanged_block_are_remote_spliced() -> None:
    storage = '<p class="legacy" data-custom="exact">Before</p><p>After</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert base.unmapped_storage_leaves == ()
    assert candidate.push_safe is True
    assert '<p class="legacy" data-custom="exact">Before</p>' in candidate.xhtml
    assert candidate.preserved_subtrees
    assert any(entry.source_kind == "protected-region" for entry in candidate.source_map_entries)
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


def test_unknown_inline_wrapper_is_localized_to_its_unchanged_remote_subtree() -> None:
    storage = '<p><span data-layout="legacy">Keep</span></p><p>Edit me</p>'

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    wrapper_diagnostic = next(item for item in base.diagnostics if item.code == "html-tag-stripped")
    assert wrapper_diagnostic.blocking is False
    outside_edit = cfxmark.to_cfx_artifact(
        base.markdown.replace("Edit me", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert outside_edit.push_safe is True
    assert '<span data-layout="legacy">Keep</span>' in outside_edit.xhtml

    inside_edit = cfxmark.to_cfx_artifact(
        base.markdown.replace("Keep", "Changed"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert inside_edit.push_safe is False
    assert any(item.code == "html-tag-stripped" and item.blocking for item in inside_edit.diagnostics)


def test_adjacent_inline_code_is_one_stable_visible_run() -> None:
    storage = "<p><code>dense_</code><code>path</code></p><p>After</p>"

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert "`dense_path`" in base.markdown
    assert candidate.push_safe is True
    assert candidate.xhtml == "<p><code>dense_</code><code>path</code></p><p>Edited</p>"


def test_soft_line_break_stays_owned_by_one_visible_text_run() -> None:
    storage = "<p>before\n<code>value</code> after</p><p>Edit me</p>"

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Edit me", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.unmapped_storage_leaves == ()
    assert "<p>before\n<code>value</code> after</p>" in candidate.xhtml


def test_transparent_wrapper_is_preserved_while_sibling_is_edited() -> None:
    storage = '<div class="legacy"><p>Before</p></div><p>After</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.xhtml == '<div class="legacy"><p>Before</p></div><p>Edited</p>'
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


def test_globally_unique_block_move_preserves_each_remote_subtree() -> None:
    storage = '<p class="first">A unique</p><p class="second">B unique</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        "B unique\n\nA unique\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert [operation.kind for operation in candidate.edit_operations] == ["move", "move"]
    assert candidate.xhtml == '<p class="second">B unique</p><p class="first">A unique</p>'
    assert len(candidate.preserved_subtrees) == 2
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


def test_noncanonical_remote_subtree_requires_matching_fresh_source() -> None:
    storage = '<p class="legacy">Before</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(base.markdown, base_artifact=base, options=EDITABLE)

    assert candidate.push_safe is False
    assert any(item.code == "splice-source-mismatch" for item in candidate.diagnostics)


def test_missing_required_remote_subtree_path_blocks_and_invalidates_proof() -> None:
    storage = '<p class="legacy" data-custom="exact">Before</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    corrupted_subtree = replace(base.remote_subtrees[0], remote_node_path=("missing[0]",))
    corrupted_base = replace(base, remote_subtrees=(corrupted_subtree,))

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=corrupted_base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.required_subtrees == (corrupted_subtree.node_id,)
    assert candidate.preserved_subtrees == ()
    assert candidate.unspliced_subtrees == candidate.required_subtrees
    assert candidate.splice_proof_complete is False
    assert any(item.code == "remote-subtree-splice-incomplete" for item in candidate.diagnostics)
    with pytest.raises(cfxmark.ConversionError, match="missing remote-subtree"):
        cfxmark.validate_cfx_artifact(candidate)


def test_remote_subtree_proof_rejects_fingerprint_tampering() -> None:
    storage = '<p class="legacy" data-custom="exact">Before</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.preserved_subtree_proofs[0]
    tampered = replace(
        candidate,
        preserved_subtree_proofs=(replace(proof, remote_subtree_fingerprint="0" * 64),),
    )

    with pytest.raises(cfxmark.ConversionError, match="fingerprint does not match XHTML"):
        cfxmark.validate_cfx_artifact(tampered)


def test_remote_subtree_proof_normalizes_local_namespace_declarations() -> None:
    storage = (
        '<p>A</p><p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier">'
        '<ri:attachment ri:filename="diagram.png"/></ac:image></p>'
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=base,
        splice_source=storage,
        presentation=base.presentation,
        options=EDITABLE,
    )

    assert candidate.xhtml == '<p>A</p><p><ac:image><ri:attachment ri:filename="diagram.png"/></ac:image></p>'
    assert candidate.splice_proof_complete is True
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


def test_duplicate_required_remote_subtree_identity_fails_closed() -> None:
    storage = '<p class="legacy" data-custom="exact">Before</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    subtree = base.remote_subtrees[0]
    conflicting = replace(subtree, remote_subtree_fingerprint="0" * 64)
    tampered_base = replace(base, remote_subtrees=(subtree, conflicting))

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=tampered_base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.required_subtrees == (subtree.node_id, subtree.node_id)
    assert candidate.preserved_subtrees == ()
    assert candidate.unspliced_subtrees == candidate.required_subtrees
    assert candidate.splice_proof_complete is False
    assert any(item.code == "remote-subtree-splice-incomplete" for item in candidate.diagnostics)
    with pytest.raises(cfxmark.ConversionError, match="duplicate required remote-subtree"):
        cfxmark.validate_cfx_artifact(candidate)


def test_code_macro_text_edit_preserves_macro_id() -> None:
    storage = (
        '<ac:structured-macro ac:name="code" ac:macro-id="code-41">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[print('before')]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("before", "after"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert 'ac:macro-id="code-41"' in candidate.xhtml
    assert "print('after')" in candidate.xhtml


def test_code_macro_copy_does_not_duplicate_macro_id() -> None:
    storage = (
        '<ac:structured-macro ac:name="code" ac:macro-id="code-41">'
        "<ac:plain-text-body><![CDATA[one]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    copied = base.markdown.rstrip() + "\n\n" + base.markdown.replace("one", "copy")

    candidate = cfxmark.to_cfx_artifact(
        copied,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.xhtml.count('ac:macro-id="code-41"') == 1


def test_common_time_and_cell_list_projections_are_readable_and_remote_backed() -> None:
    storage = (
        '<p>Due <ac:time ac:datetime="2026-07-17"/></p>'
        "<table><thead><tr><th>Items</th></tr></thead><tbody><tr><td>"
        "<ul><li>One</li><li>Two</li></ul>"
        "</td></tr></tbody></table>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert "Due 2026-07-17" in base.markdown
    assert "- One<br>- Two" in base.markdown
    assert "cfx:" not in base.markdown
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.push_safe is True
    assert candidate.xhtml == storage


def test_adjacent_styled_lists_are_readable_remote_backed_islands() -> None:
    storage = (
        '<ul style="list-style-type: square;"><li>First</li></ul>'
        '<ul style="list-style-type: square;"><li>Second</li></ul>'
        "<p>After</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert "- First" in base.markdown
    assert "- Second" in base.markdown
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.xhtml == (
        '<ul style="list-style-type: square;"><li>First</li></ul>'
        '<ul style="list-style-type: square;"><li>Second</li></ul>'
        "<p>Edited</p>"
    )


def test_eof_append_anchors_short_projection_nested_inside_larger_remote_island() -> None:
    storage = (
        '<p class="preserve-break"><br/></p><ul style="list-style-type: square;"><li>A<br/>B</li></ul><p>After</p>'
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert [region.display_label for region in base.protected_regions] == ["<br>", "- A<br>B"]
    candidate = cfxmark.to_cfx_artifact(
        base.markdown + "\nTEST\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.splice_proof_complete is True
    assert candidate.xhtml == storage + "<p>TEST</p>"


def test_unchanged_prefix_and_suffix_regions_skip_global_projection_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = "<p>" + " ".join(["repeat"] * 8_000) + '</p><p class="preserve-break"><br/></p>'

    def fail_match(*_args: object, **_kwargs: object) -> None:
        pytest.fail("byte-anchored protected regions must not invoke the quadratic semantic matcher")

    monkeypatch.setattr(protected, "match_edit_operations", fail_match)
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown + "\nTEST\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.splice_proof_complete is True
    assert candidate.xhtml == storage + "<p>TEST</p>"


def test_edit_before_suffix_anchored_region_preserves_exact_remote_subtree() -> None:
    storage = '<p>Before</p><p class="preserve-break"><br/></p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Before", "Changed before"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.splice_proof_complete is True
    assert candidate.xhtml == '<p>Changed before</p><p class="preserve-break"><br/></p>'


@pytest.mark.parametrize(
    "cell_content",
    [
        "<strong>X </strong>Y",
        "<strong>X&nbsp;</strong>Y",
        "<strong>X</strong><strong>Y</strong>",
        "<span><strong>X </strong>Y</span>",
    ],
)
def test_remote_base_map_never_claims_renderer_only_leaves(cell_content: str) -> None:
    storage = f"<table><tbody><tr><td>{cell_content}</td></tr></tbody></table>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    raw_leaves = {
        storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in extract_storage_source(storage).leaves
    }
    base_leaves = {storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in base.base_storage_to_node}
    candidate_leaves = {
        storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in candidate.candidate_storage_to_operation
    }

    assert len(base_leaves) == len(base.base_storage_to_node)
    assert base_leaves == raw_leaves
    assert candidate_leaves == raw_leaves


def test_empty_paragraph_and_jira_macro_identity_survive_unrelated_edit() -> None:
    storage = (
        "<p/>"
        '<ac:structured-macro ac:name="jira" ac:macro-id="macro-stable-1">'
        '<ac:parameter ac:name="key">EXAMPLE-1</ac:parameter>'
        "</ac:structured-macro>"
        "<p>After</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert base.status == "ready_to_edit"
    assert base.markdown.startswith("<!-- cfxmark:blank -->")
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert '<ac:structured-macro ac:name="jira" ac:macro-id="macro-stable-1">' in candidate.xhtml
    assert candidate.xhtml.startswith("<p/>")
    assert candidate.xhtml.endswith("<p>Edited</p>")


def test_strong_before_korean_particle_keeps_source_map_identity() -> None:
    # A Korean particle directly after the closing delimiter used to force raw
    # ``<strong>`` HTML. Asterisks re-parse fine there, so the Markdown is now
    # delimited — the point of this test is that the source-map identity and
    # the exact candidate XHTML survive either spelling.
    storage = "<p>Scope <strong>motion_state migration</strong>이다.</p><p>After</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert r"**motion\_state migration**이다." in base.markdown
    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.xhtml == "<p>Scope <strong>motion_state migration</strong>이다.</p><p>Edited</p>"


def test_strong_boundary_nbsp_is_a_canonical_separator_and_remote_preserved() -> None:
    storage = "<p><strong>Label&nbsp;</strong></p><p>After</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert base.markdown.startswith("**Label**&nbsp;")
    assert candidate.push_safe is True
    assert candidate.xhtml == "<p><strong>Label\u00a0</strong></p><p>Edited</p>"


def test_mixed_alignment_table_allows_outside_edit_and_preserves_exact_style() -> None:
    storage = (
        "<p>Before</p><table><thead><tr>"
        '<th style="text-align:left">H</th></tr></thead><tbody><tr>'
        '<td style="text-align:right">X</td></tr></tbody></table><p>After</p>'
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Before", "Edited"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert 'style="text-align:left"' in candidate.xhtml
    assert 'style="text-align:right"' in candidate.xhtml
    assert "Edited" in candidate.xhtml


# ---------------------------------------------------------------------------
# Directive-macro identity round-trip (expand / admonition / inline jira).
#
# A directive macro carrying ac:macro-id or a non-default ac:schema-version used
# to become a preserve-only island, so editing its body, its parameters, or even
# an unrelated block on the same page was rejected. It now shares the CodeBlock
# identity lifecycle: the remote identity is captured, reapplied on unique
# correspondence, and the body/parameters stay editable.
# ---------------------------------------------------------------------------


def _expand(schema: str = "", macro_id: str = "", title: str = "T", body: str = "Inner") -> str:
    attrs = ' ac:schema-version="1"' if schema else ""
    attrs += f' ac:macro-id="{macro_id}"' if macro_id else ""
    return (
        f'<ac:structured-macro ac:name="expand"{attrs}>'
        f'<ac:parameter ac:name="title">{title}</ac:parameter>'
        f"<ac:rich-text-body><p>{body}</p></ac:rich-text-body></ac:structured-macro>"
    )


@pytest.mark.parametrize("schema", [False, True], ids=["no-schema", "schema"])
@pytest.mark.parametrize("macro_id", ["", "e-1"], ids=["no-id", "id"])
def test_expand_body_edit_preserves_identity_across_combinations(schema: bool, macro_id: str) -> None:
    # AC-1/AC-2: the four schema-version x macro-id combinations must all round-
    # trip a body edit. The schema-less cases (no-schema) are the witness-
    # condition regression guard: without recording an identity for a macro whose
    # macro-id is absent, cfxmark would re-add ac:schema-version="1" and reject.
    storage = _expand(schema="1" if schema else "", macro_id=macro_id)
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Inner", "INNER EDITED"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.ownership_proof is not None and not candidate.ownership_proof.fatal
    assert "<p>INNER EDITED</p>" in candidate.xhtml
    # schema-version is restored exactly: present iff the remote had it.
    assert ('ac:schema-version="1"' in candidate.xhtml) is schema
    if macro_id:
        assert f'ac:macro-id="{macro_id}"' in candidate.xhtml
    else:
        assert "ac:macro-id" not in candidate.xhtml


def test_expand_parameter_edit_is_editable_and_preserves_macro_id() -> None:
    # AC-2b: the title parameter appears in the Markdown projection, so editing
    # it is a natural user action that must round-trip too.
    storage = _expand(schema="1", macro_id="e-2", title="Old")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace('title="Old"', 'title="New"'),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert 'ac:macro-id="e-2"' in candidate.xhtml
    assert '<ac:parameter ac:name="title">New</ac:parameter>' in candidate.xhtml


def test_admonition_body_edit_preserves_macro_id() -> None:
    # AC-3: info/note/warning/tip share the DirectiveMacro path.
    storage = (
        '<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="i-1">'
        "<ac:rich-text-body><p>Heads up</p></ac:rich-text-body></ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Heads up", "HEADS UP EDITED"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert 'ac:macro-id="i-1"' in candidate.xhtml
    assert "HEADS UP EDITED" in candidate.xhtml


def _jira(schema: str = "", macro_id: str = "", key: str = "P-1") -> str:
    attrs = ' ac:schema-version="1"' if schema else ""
    attrs += f' ac:macro-id="{macro_id}"' if macro_id else ""
    return (
        f'<p>See <ac:structured-macro ac:name="jira"{attrs}>'
        f'<ac:parameter ac:name="key">{key}</ac:parameter></ac:structured-macro> now</p>'
    )


@pytest.mark.parametrize("schema", [False, True], ids=["no-schema", "schema"])
@pytest.mark.parametrize("macro_id", ["", "j-1"], ids=["no-id", "id"])
def test_inline_jira_adjacent_edit_preserves_identity_across_combinations(schema: bool, macro_id: str) -> None:
    # AC-4: inline jira is a separate InlineDirectiveMacro path. Across all four
    # schema x macro-id combinations, editing neighbouring text must keep the
    # macro, its key, and its identity intact.
    storage = _jira(schema="1" if schema else "", macro_id=macro_id)
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("now", "LATER"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.ownership_proof is not None and not candidate.ownership_proof.fatal
    assert 'ac:name="jira"' in candidate.xhtml
    assert '<ac:parameter ac:name="key">P-1</ac:parameter>' in candidate.xhtml
    assert "LATER" in candidate.xhtml
    assert ('ac:schema-version="1"' in candidate.xhtml) is schema
    if macro_id:
        assert f'ac:macro-id="{macro_id}"' in candidate.xhtml
    else:
        assert "ac:macro-id" not in candidate.xhtml


def test_freshly_authored_directive_keeps_default_schema_version() -> None:
    # AC-5: a directive written directly in Markdown has no remote counterpart;
    # its output must not change (schema-version stays "1"), unlike a code block.
    result = cfxmark.to_cfx('::: expand title="Fresh"\nHi\n:::\n')

    assert 'ac:name="expand" ac:schema-version="1"' in result.xhtml
    assert "ac:macro-id" not in result.xhtml
    assert "<p>Hi</p>" in result.xhtml


def test_expand_copy_does_not_duplicate_macro_id() -> None:
    # AC-6: copying a directive macro is push-safe and never duplicates its
    # macro-id; the copy regenerates as a new macro. Mirrors the code-block
    # contract in test_code_macro_copy_does_not_duplicate_macro_id.
    storage = _expand(schema="1", macro_id="e-3", body="One")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    copied = base.markdown.rstrip() + "\n\n" + base.markdown.replace("One", "Two")

    candidate = cfxmark.to_cfx_artifact(
        copied,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.xhtml.count('ac:macro-id="e-3"') == 1


def test_expand_identical_duplicate_is_fail_closed() -> None:
    # AC-6 (ambiguity): a byte-identical duplicate has no unique correspondence
    # to the single base macro, so the identity is not reattached and the
    # candidate is not push-safe -- fail-closed rather than guessing which copy
    # owns the macro-id. Contrast with the distinguishable copy above, which is
    # push-safe with the id kept exactly once.
    storage = _expand(schema="1", macro_id="e-6", body="Same")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    duplicated = base.markdown.rstrip() + "\n\n" + base.markdown

    candidate = cfxmark.to_cfx_artifact(
        duplicated,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.xhtml.count('ac:macro-id="e-6"') == 0


def test_expand_with_nested_code_block_preserves_both_identities() -> None:
    # A directive body commonly nests another identity macro (here a code block).
    # Editing only the outer text must keep BOTH the outer directive's macro-id
    # and the inner code block's macro-id: the identity reattachment recurses
    # into replacement bodies instead of stranding descendants at default identity.
    storage = (
        '<ac:structured-macro ac:name="expand" ac:schema-version="1" ac:macro-id="e-nest">'
        '<ac:parameter ac:name="title">T</ac:parameter><ac:rich-text-body>'
        "<p>outer text</p>"
        '<ac:structured-macro ac:name="code" ac:schema-version="1" ac:macro-id="c-nest">'
        "<ac:plain-text-body><![CDATA[print(1)]]></ac:plain-text-body></ac:structured-macro>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("outer text", "OUTER EDITED"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.ownership_proof is not None and not candidate.ownership_proof.fatal
    assert 'ac:macro-id="e-nest"' in candidate.xhtml
    assert 'ac:macro-id="c-nest"' in candidate.xhtml
    assert "OUTER EDITED" in candidate.xhtml


def test_expand_with_nested_inline_jira_preserves_both_identities() -> None:
    # Same nested-identity guarantee for an inline jira macro inside a directive
    # body: editing the surrounding text keeps both macro identities.
    storage = (
        '<ac:structured-macro ac:name="expand" ac:schema-version="1" ac:macro-id="e-j">'
        '<ac:parameter ac:name="title">T</ac:parameter><ac:rich-text-body>'
        '<p>See <ac:structured-macro ac:name="jira" ac:schema-version="1" ac:macro-id="j-in">'
        '<ac:parameter ac:name="key">P-1</ac:parameter></ac:structured-macro> here</p>'
        "</ac:rich-text-body></ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("here", "THERE"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.ownership_proof is not None and not candidate.ownership_proof.fatal
    assert 'ac:macro-id="e-j"' in candidate.xhtml
    assert 'ac:macro-id="j-in"' in candidate.xhtml
    assert '<ac:parameter ac:name="key">P-1</ac:parameter>' in candidate.xhtml
    assert "THERE" in candidate.xhtml


def test_schema_only_code_macro_edit_preserves_schema_version() -> None:
    # A code macro carrying ac:schema-version but no macro-id must round-trip an
    # edit without losing the schema-version: the witness records schema-only
    # code, symmetric to the directive witness. A code block renders without
    # schema-version by default, so "present at all" is the witness condition.
    storage = (
        '<ac:structured-macro ac:name="code" ac:schema-version="1">'
        "<ac:plain-text-body><![CDATA[print(1)]]></ac:plain-text-body></ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("print(1)", "print(2)"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert 'ac:schema-version="1"' in candidate.xhtml
    assert "print(2)" in candidate.xhtml


def test_schema_only_code_nested_in_expand_body_is_editable() -> None:
    # The schema-only code witness also covers a code macro nested in an edited
    # directive body: editing the outer text keeps both the expand identity and
    # the inner code's schema-version.
    storage = (
        '<ac:structured-macro ac:name="expand" ac:schema-version="1" ac:macro-id="e-sc">'
        '<ac:parameter ac:name="title">T</ac:parameter><ac:rich-text-body>'
        "<p>outer</p>"
        '<ac:structured-macro ac:name="code" ac:schema-version="1">'
        "<ac:plain-text-body><![CDATA[x=1]]></ac:plain-text-body></ac:structured-macro>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("outer", "OUTER"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert 'ac:macro-id="e-sc"' in candidate.xhtml
    # both the expand and the nested code keep their schema-version.
    assert candidate.xhtml.count('ac:schema-version="1"') == 2


def test_freshly_authored_code_block_stays_schema_less() -> None:
    # A code block authored directly in Markdown has no remote identity; the
    # widened code witness must not start emitting a schema-version for it.
    result = cfxmark.to_cfx("```python\nhi\n```\n")

    assert "ac:schema-version" not in result.xhtml


def test_expand_with_nested_opaque_allows_editing_the_editable_sibling() -> None:
    # AC-7: an opaque child inside the body stays protected while a sibling
    # editable block in the same body is edited. The boundary is leaf-level, not
    # "any nested opaque forbids all edits".
    storage = (
        '<ac:structured-macro ac:name="expand" ac:schema-version="1" ac:macro-id="e-4">'
        '<ac:parameter ac:name="title">Outer</ac:parameter><ac:rich-text-body>'
        "<p>text before</p>"
        '<ac:structured-macro ac:name="unknownmacro"><ac:rich-text-body>'
        "<p>opaque inner</p></ac:rich-text-body></ac:structured-macro>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("text before", "TEXT AFTER"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert "TEXT AFTER" in candidate.xhtml


def test_edited_expand_passes_managed_validation() -> None:
    # AC-8: the edited candidate survives the full source-bound publish preflight.
    storage = _expand(schema="1", macro_id="e-5")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("Inner", "EDITED"),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


# ---------------------------------------------------------------------------
# Identity carry across a tie somewhere else on the page.
#
# `apply_macro_identities` ran inside the same `try` as candidate generation and
# called the matcher bare, so a tie ANYWHERE raised before a single identity was
# applied and the retry rebuilt from the document that never received them. The
# macro did not have to be near the tie, or touched by it, to lose its `macro-id`.
# ---------------------------------------------------------------------------


def _tie_source(macro_id: str) -> str:
    """An identity-bearing code macro, then two identical paragraphs."""

    return (
        f'<ac:structured-macro ac:name="code" ac:macro-id="{macro_id}">'
        "<ac:plain-text-body><![CDATA[old]]></ac:plain-text-body>"
        "</ac:structured-macro><p>Dup</p><p>Dup</p>"
    )


def _edit_body_and_drop_one_duplicate(markdown: str, *, drop: bool) -> str:
    seen = 0
    kept = []
    for line in markdown.replace("old", "new").splitlines(keepends=True):
        if drop and "Dup" in line:
            seen += 1
            if seen == 2:
                continue
        kept.append(line)
    return "".join(kept)


@pytest.mark.parametrize("with_tie", [False, True], ids=["no-tie", "tie-elsewhere"])
def test_macro_identity_survives_a_tie_it_is_not_part_of(with_tie: bool) -> None:
    """A controlled pair: the macro and its edit are identical in both runs, and
    the only variable is whether an unrelated paragraph tie exists. Measured
    before the fix, `with_tie=True` regenerated the macro without its id while
    the body edit landed unchanged -- so the loss was invisible in the output.

    The tie must still block the publish. Carrying the identity says the walk
    proved this pairing, not that the candidate is safe to send.
    """

    storage = _tie_source("keep-me")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        _edit_body_and_drop_one_duplicate(base.markdown, drop=with_tie),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert "new" in candidate.xhtml  # the edit itself landed either way
    assert candidate.xhtml.count('ac:macro-id="keep-me"') == 1
    tied = "semantic-mapping-ambiguous" in {item.code for item in candidate.diagnostics}
    assert tied is with_tie
    # Confining a tie must never be mistaken for resolving it.
    assert candidate.push_safe is not with_tie


def test_identity_is_never_read_off_a_synthesized_insert_id() -> None:
    """Stable ids for inserted nodes are derived from content, so a node
    inserted as a byte-identical copy of a base node is handed that base node's
    id. Reading identities out of that map would reattach a remote `macro-id` to
    a duplicate the matcher explicitly refused to pair.

    This was latent: the bare matcher raised first, so nothing downstream was
    ever reached. Taking a confined tie removes that accident, which makes the
    proven-pairs-only rule load-bearing rather than incidental.
    """

    storage = _expand(schema="1", macro_id="e-7", body="Same")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    # Duplicate the macro AND tie elsewhere, so the confined-tie path is taken.
    duplicated = base.markdown.rstrip() + "\n\n" + base.markdown.rstrip() + "\n\nx\n\nx\n"

    candidate = cfxmark.to_cfx_artifact(
        duplicated,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.xhtml.count('ac:macro-id="e-7"') == 0


def test_a_confined_tie_leaves_no_unattributed_leaf_outside_itself() -> None:
    """Identity carry and attribution are two different questions, and the tie
    used to lose both. Measured on this fixture: before, the macro regenerated
    without its id AND left one leaf with no source-bound owner; after, it keeps
    the id and the proof attributes every leaf it touches.

    The remaining refusal is the tie itself, which is the honest answer -- the
    two readings of the duplicate pair really are undecided.
    """

    storage = _tie_source("keep-me")
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        _edit_body_and_drop_one_duplicate(base.markdown, drop=True),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = candidate.ownership_proof
    assert proof is not None
    assert proof.unclassified == ()
    assert proof.fatal is True
    assert "semantic-mapping-ambiguous" in proof.fatal_diagnostic_codes
