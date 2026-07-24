from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

import cfxmark
import cfxmark.api as api_module
from cfxmark.change_ownership import OwnedChange
from cfxmark.diagnostics import DiagnosticLocation, make_diagnostic
from cfxmark.source_map import operation_id

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def _full_migration_base(storage: str) -> cfxmark.MarkdownArtifact:
    """Return a public pull artifact without the legacy exact-splice escape hatch."""

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    return replace(base, protected_regions=(), remote_subtrees=())


def _candidate(storage: str, markdown: str) -> cfxmark.CfxArtifact:
    base = _full_migration_base(storage)
    return cfxmark.to_cfx_artifact(
        markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )


def _proof(candidate: cfxmark.CfxArtifact) -> Any:
    proof = candidate.ownership_proof
    assert proof is not None
    return proof


def test_baseline_attribute_drop_is_named_and_owned_never_silent() -> None:
    """A remote-only attribute the round-trip drops must never pass silently.

    Historically this failed closed as unclassified-storage-change. It is now
    a *named* per-attribute occurrence (element-attribute-omitted, effect
    "removed", so publish still requires explicit consent downstream) whose
    atomic witness owns the leaf change — strictly more informative, and still
    not silent.
    """

    storage = '<p data-silent-migration="secret">Visible</p>'
    base = _full_migration_base(storage)
    assert base.diagnostics == ()

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    assert proof.fatal is False
    assert not proof.unclassified
    assert any(change.identity[2] == "data-silent-migration" for change in proof.migration)
    report = candidate.source_migration_report
    assert report is not None
    named = [occurrence for occurrence in report.occurrences if occurrence.code == "element-attribute-omitted"]
    assert named and all(occurrence.effect == "removed" for occurrence in named)


def test_real_occurrence_cannot_own_adjacent_silent_migration_loss() -> None:
    storage = '<p><foo data-secret="x">Visible</foo> <ac:emoticon ac:name="smile"/></p>'
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    assert candidate.source_migration_report is not None
    occurrence_ids = {item.occurrence_id for item in candidate.source_migration_report.occurrences}
    foo_changes = tuple(change for change in proof.unclassified if "foo[0]" in change.identity[0])

    assert {diagnostic.code for diagnostic in base.diagnostics} == {"emoticon-to-unicode"}
    assert candidate.push_safe is False
    assert proof.blocking_code == "unclassified-storage-change"
    assert foo_changes
    assert not any(change.identity[0][-1] == "foo[0]" and change.identity[1] != "tail" for change in proof.migration)
    assert {change.owner for change in proof.migration} <= occurrence_ids


def test_adjacent_parent_text_normalization_is_named_composed_and_still_blocked() -> None:
    """The parent-text value mixes an emoticon conversion with a trailing-space
    trim. Historically the trim was unnamed, so attributing the text to the
    emoticon occurrence would have hidden it and the leaf had to stay
    unclassified. The trim is now a *named* collateral occurrence and the
    ordered-composition witness reproduces the exact text value, so the leaf is
    legitimately owned by real, report-projected occurrences — while the
    genuinely ambiguous tail deletion (two occurrences produce the identical
    change) still fails closed as multiple-change-owners."""

    storage = '<p>prefix<ac:emoticon ac:name="smile"/>   </p>'
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    paragraph_text = (("p[0]",), "text", None, 0)

    assert {diagnostic.code for diagnostic in base.diagnostics} == {"emoticon-to-unicode"}
    assert candidate.xhtml == "<p>prefix🙂</p>"
    assert candidate.push_safe is False
    assert proof.blocking_code == "multiple-change-owners"
    assert not any(change.identity == paragraph_text for change in proof.unclassified)
    owned = [change for change in proof.migration if change.identity == paragraph_text]
    assert owned and isinstance(owned[0].owner, str)
    report = candidate.source_migration_report
    assert report is not None
    assert owned[0].owner in {occurrence.occurrence_id for occurrence in report.occurrences}
    assert proof.multiple_owners


@pytest.mark.parametrize(
    ("storage", "code", "expected"),
    [
        ("<p>A<!--secret-->B</p>", "xml-comment-dropped", "<p>AB</p>"),
        ("<p>A</p><!--secret-->tail", "xml-comment-dropped", "<p>A</p><p>tail</p>"),
        ('<p>A<span data-x="y">B</span>C</p>', "html-tag-stripped", "<p>ABC</p>"),
        (
            "<ac:layout><ac:layout-section><ac:layout-cell><p>A</p></ac:layout-cell></ac:layout-section></ac:layout>",
            "layout-flattened",
            "<p>A</p>",
        ),
        ("<p>A</p><p></p><p>B</p>", "empty-paragraph-dropped", "<p>A</p><p><br/></p><p>B</p>"),
        ("<ul><li><p>A</p></li></ul>", "list-item-paragraph-unwrapped", "<ul><li>A</li></ul>"),
        ("<p>A   </p>", "trailing-space-trimmed", "<p>A</p>"),
    ],
)
def test_consentable_full_migration_is_owned_by_its_atomic_occurrence(
    storage: str,
    code: str,
    expected: str,
) -> None:
    candidate = _candidate(storage, _full_migration_base(storage).markdown)
    proof = _proof(candidate)
    report = candidate.source_migration_report

    assert report is not None
    code_occurrence_ids = {item.occurrence_id for item in report.occurrences if item.code == code}
    assert candidate.xhtml == expected
    assert candidate.push_safe is True
    assert code_occurrence_ids
    assert {change.owner for change in proof.migration} == code_occurrence_ids
    assert proof.unclassified == ()
    assert proof.multiple_owners == ()


def test_composed_migration_report_has_one_id_per_physical_source_occurrence() -> None:
    storage = (
        '<ac:layout><ac:layout-section><ac:layout-cell><p>A<!--x--><span data-x="1">B</span>   </p>'
        "<p></p><ul><li><p>C</p></li></ul></ac:layout-cell></ac:layout-section></ac:layout>"
    )
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    report = candidate.source_migration_report

    assert report is not None
    source_keys = [
        (item.code, tuple(component.name for component in item.location_identity))
        for item in report.occurrences
        if item.code in {"trailing-space-trimmed", "empty-paragraph-dropped", "list-item-paragraph-unwrapped"}
    ]
    assert len(source_keys) == 3
    assert len(set(source_keys)) == 3
    assert len({item.occurrence_id for item in report.occurrences}) == len(report.occurrences)


def test_duplicate_diagnostic_identity_is_fatal_through_the_public_candidate_path() -> None:
    storage = (
        '<table><tbody><tr><td style="background-color:#ff0000">A</td>'
        '<td style="background-color:#00ff00">B</td></tr></tbody></table>'
    )
    base = _full_migration_base(storage)
    first, second = base.diagnostics
    duplicated = replace(base, diagnostics=(first, replace(second, occurrence_id=first.occurrence_id)))

    candidate = cfxmark.to_cfx_artifact(
        duplicated.markdown,
        presentation=duplicated.presentation,
        base_artifact=duplicated,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)

    assert candidate.push_safe is False
    assert proof.fatal is True
    assert "semantic-mapping-ambiguous" in proof.fatal_diagnostic_codes


def test_public_producer_collision_is_preserved_and_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    real_make_diagnostic = api_module.make_diagnostic

    def colliding_table_diagnostic(**kwargs: Any) -> Any:
        diagnostic = real_make_diagnostic(**kwargs)
        if diagnostic.code == "table-cell-background-omitted":
            return replace(diagnostic, occurrence_id="occ-forced-producer-collision")
        return diagnostic

    monkeypatch.setattr(api_module, "make_diagnostic", colliding_table_diagnostic)
    storage = (
        '<table><tbody><tr><td style="background-color:#ff0000">A</td>'
        '<td style="background-color:#00ff00">B</td></tr></tbody></table>'
    )

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    colliding = [item for item in base.diagnostics if item.occurrence_id == "occ-forced-producer-collision"]

    assert len(colliding) == 2
    assert {item.location.table_column for item in colliding} == {0, 1}
    assert base.push_safe is False
    assert any(item.code == "semantic-mapping-ambiguous" and item.blocking for item in base.diagnostics)

    candidate = cfxmark.to_cfx_artifact(
        base.managed_markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    assert candidate.push_safe is False
    assert proof.fatal is True
    assert "semantic-mapping-ambiguous" in proof.fatal_diagnostic_codes


def test_table_cell_anchor_is_part_of_the_diagnostic_occurrence_identity() -> None:
    common = {
        "code": "table-cell-background-omitted",
        "category": "informational",
        "severity": "informational",
        "message": "cell background",
        "source_fingerprint": "sha256:source",
        "ordinal": 0,
    }
    first = make_diagnostic(
        **common,
        location=DiagnosticLocation(semantic_path=("table:t1",), table_row=0, table_column=0),
    )
    second = make_diagnostic(
        **common,
        location=DiagnosticLocation(semantic_path=("table:t1",), table_row=0, table_column=1),
    )

    assert first.occurrence_id != second.occurrence_id


def test_overlapping_comment_tail_and_trimming_cannot_drop_a_report_occurrence() -> None:
    storage = "<p>A<!--c-->   </p>"
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    report = candidate.source_migration_report

    assert report is not None
    assert {item.code for item in report.occurrences} >= {"xml-comment-dropped", "trailing-space-trimmed"}
    assert candidate.xhtml == "<p>A</p>"
    assert candidate.push_safe is False
    assert proof.blocking_code == "multiple-change-owners"
    assert proof.multiple_owners


def test_collateral_candidate_change_without_markdown_edit_operation_is_fatal() -> None:
    storage = '<table><tbody><tr><td style="background-color:#ff0000">A</td></tr></tbody></table>'
    base = _full_migration_base(storage)
    cell = base.presentation.cells[0]
    assert cell.background is not None
    changed_background = replace(cell.background, exact="#0000ff", equality_key="hex:#0000ff")
    changed_cell = replace(cell, background=changed_background, source_style="background-color:#0000ff")
    changed_presentation = replace(base.presentation, cells=(changed_cell,))

    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=changed_presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    assert candidate.edit_operations == ()
    assert proof.fatal is True
    assert proof.blocking_code == "unclassified-storage-change"


def test_one_final_leaf_owned_by_migration_and_user_edit_is_fatal_overlap() -> None:
    storage = '<p><ac:emoticon ac:name="smile"/></p>'
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        "changed\n",
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    assert candidate.push_safe is False
    assert proof.blocking_code == "migration-user-overlap-ambiguous"
    assert proof.overlapping
    report_ids = {item.occurrence_id for item in base.migration_report.occurrences}
    assert {change.owner for change in proof.migration} <= report_ids
    assert all(not change.owner.startswith(("mig:", "edit:")) for change in (*proof.migration, *proof.intended))


def test_empty_paragraph_deletion_is_c0_owned_and_publishable() -> None:
    # A remote empty paragraph round-trips to a Markdown <br> (C0 == <p><br/></p>).
    # Deleting it is a genuine user edit. The C0-bound operation base makes the
    # user delete op's tombstones cover the migration-introduced br leaf, so the
    # deletion is user-owned and publishable instead of unclassified -- and the
    # managed artifact validates, so the migration-witness subset accepts a
    # transient C0 leaf the user edited away.
    storage = "<p>Before</p><p/><p>After</p>"
    base = _full_migration_base(storage)
    markdown = base.markdown.replace("<br>", "").strip()
    candidate = cfxmark.to_cfx_artifact(
        markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    assert candidate.push_safe is True
    assert proof.unclassified == ()
    assert proof.overlapping == ()
    assert candidate.xhtml == "<p>Before</p><p>After</p>"
    # Publish preflight (which regenerates a fresh C0 from source) accepts it.
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


def test_publish_preflight_rejects_dropped_migration_occurrence() -> None:
    # The migration-witness subset relaxation must not let a forged report hide a
    # disclosed source loss: every source-side occurrence is still reproduced
    # byte-for-byte from fresh S0. Deleting the empty-paragraph occurrence from
    # the stored report is rejected even though the ownership proof is otherwise
    # self-consistent.
    from cfxmark.migration_report import MigrationReport

    storage = "<p>Before</p><p/><p>After</p>"
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.source_migration_report is not None
    assert any(item.code == "empty-paragraph-dropped" for item in candidate.source_migration_report.occurrences)
    forged_report = MigrationReport.build(
        occurrences=tuple(
            item for item in candidate.source_migration_report.occurrences if item.code != "empty-paragraph-dropped"
        )
    )
    forged = replace(candidate, source_migration_report=forged_report)
    with pytest.raises(cfxmark.ConversionError):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_presentation_only_change_under_full_migration_c0_path_fails_closed() -> None:
    # A cell background edit with no Markdown edit takes the C0 operation path
    # (full migration strips protected regions, so use_c0_ops is True) together
    # with the presentation-changed operation rebuild. The collateral storage
    # change has no user operation and no migration occurrence, so it must fail
    # closed -- and the publish-preflight replay must reject it consistently
    # rather than accepting a push-safe artifact whose maps it cannot reproduce.
    storage = '<table><tbody><tr><td style="background-color:#ff0000">A</td></tr></tbody></table>'
    base = _full_migration_base(storage)
    assert base.remote_subtrees == () and base.protected_regions == ()
    cell = base.presentation.cells[0]
    assert cell.background is not None
    changed_background = replace(cell.background, exact="#0000ff", equality_key="hex:#0000ff")
    changed_cell = replace(cell, background=changed_background, source_style="background-color:#0000ff")
    changed_presentation = replace(base.presentation, cells=(changed_cell,))
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=changed_presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    assert candidate.push_safe is False
    assert proof.blocking_code == "unclassified-storage-change"
    with pytest.raises(cfxmark.ConversionError):
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


def test_missing_source_map_on_public_base_artifact_is_fatal() -> None:
    storage = "<p>Before</p>"
    base = _full_migration_base(storage)
    forged = replace(
        base,
        base_storage_to_node={},
        base_node_to_storage={},
        source_map_entries=(),
    )

    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=forged,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    assert candidate.push_safe is False
    assert "semantic-source-map-incomplete" in proof.fatal_diagnostic_codes


def test_duplicate_sibling_identity_fails_closed_in_the_public_proof() -> None:
    storage = "<p>A</p><p>X</p><p>X</p><p>B</p>"
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        "A\n\nX\n\nB\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = _proof(candidate)
    assert candidate.push_safe is False
    assert "semantic-mapping-ambiguous" in proof.fatal_diagnostic_codes


@pytest.mark.parametrize(
    ("storage", "edited", "kind"),
    [
        ("<p>A</p>", "B\n", "update"),
        ("<p>A</p>", "A\n\nB\n", "insert"),
        ("<p>A</p><p>B</p>", "A\n", "delete"),
        ("<p>A</p><p>B</p>", "B\n\nA\n", "move"),
    ],
)
def test_final_changes_are_owned_by_actual_edit_operation_ids(storage: str, edited: str, kind: str) -> None:
    candidate = _candidate(storage, edited)
    proof = _proof(candidate)
    actual_ids = {operation_id(item) for item in candidate.edit_operations if item.kind == kind}
    all_actual_ids = {operation_id(item) for item in candidate.edit_operations}

    assert candidate.push_safe is True
    assert proof.fatal is False
    assert actual_ids
    assert {change.owner for change in proof.intended} <= all_actual_ids
    assert {change.owner for change in proof.intended} & actual_ids


@pytest.mark.parametrize(
    ("storage", "edited", "expected", "kind"),
    [
        ("<p>A</p><p>C</p>", "A\n\nB\n\nC\n", "<p>A</p><p>B</p><p>C</p>", "insert"),
        ("<p>A</p><p>B</p><p>C</p>", "A\n\nC\n", "<p>A</p><p>C</p>", "delete"),
    ],
)
def test_middle_structural_edit_owns_shifted_preserved_siblings(
    storage: str,
    edited: str,
    expected: str,
    kind: str,
) -> None:
    candidate = _candidate(storage, edited)
    proof = _proof(candidate)
    actual_ids = {operation_id(item) for item in candidate.edit_operations if item.kind == kind}
    all_actual_ids = {operation_id(item) for item in candidate.edit_operations}

    assert candidate.xhtml == expected
    assert candidate.push_safe is True
    assert proof.unclassified == ()
    assert actual_ids
    assert {change.owner for change in proof.intended} <= all_actual_ids
    assert {change.owner for change in proof.intended} & actual_ids


def test_unique_heterogeneous_reorder_has_one_real_move_owner() -> None:
    candidate = _candidate("<h1>A</h1><p>B</p>", "B\n\n# A\n")
    proof = _proof(candidate)
    move_ids = {operation_id(item) for item in candidate.edit_operations if item.kind == "move"}

    assert candidate.xhtml == "<p>B</p><h1>A</h1>"
    assert candidate.push_safe is True
    assert len({change.owner for change in proof.intended}) == 1
    assert proof.unclassified == ()
    assert {change.owner for change in proof.intended} <= move_ids


def test_three_sibling_reversal_uses_a_complete_actual_move_basis() -> None:
    candidate = _candidate("<p>A</p><p>B</p><p>C</p>", "C\n\nB\n\nA\n")
    proof = _proof(candidate)
    move_ids = {operation_id(item) for item in candidate.edit_operations if item.kind == "move"}
    proof_owner_ids = {change.owner for change in proof.intended}

    assert candidate.xhtml == "<p>C</p><p>B</p><p>A</p>"
    assert candidate.push_safe is True
    assert len(move_ids) == 2
    assert proof_owner_ids == move_ids
    assert proof.unclassified == ()
    assert proof.multiple_owners == ()


def test_rebased_protected_list_uses_the_exact_source_map_for_move_ownership() -> None:
    storage = "<ul><li>A</li><li>B</li></ul>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    baseline = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    rebased = replace(base, semantic_nodes=baseline.semantic_nodes)

    candidate = cfxmark.to_cfx_artifact(
        "- B\n- A\n",
        presentation=base.presentation,
        base_artifact=rebased,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    move_ids = {operation_id(item) for item in candidate.edit_operations if item.kind == "move"}

    assert candidate.xhtml == "<ul><li>B</li><li>A</li></ul>"
    assert candidate.push_safe is True
    assert proof.unclassified == ()
    assert len({change.owner for change in proof.intended}) == 1
    assert {change.owner for change in proof.intended} <= move_ids


def test_unique_styled_table_reorder_preserves_presentation_under_the_move_owner() -> None:
    storage = (
        "<table><thead><tr><th>H1</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#ff0000">A</td></tr></tbody></table>'
        "<table><thead><tr><th>H2</th></tr></thead><tbody><tr><td>B</td></tr></tbody></table>"
    )
    capability = cfxmark.ColorCapability(
        capability_id="synthetic-highlight-hex-v1",
        source_encoding="data-highlight-colour",
        value_kind="hex",
        render_strategy="highlight-attribute",
    )
    options = cfxmark.ConversionOptions(
        profile="editable",
        color_capabilities=cfxmark.ColorCapabilityMatrix((capability,)),
    )
    base = replace(cfxmark.to_md_artifact(storage, options=options), protected_regions=(), remote_subtrees=())
    first, second = base.markdown.strip().split("\n\n")
    candidate = cfxmark.to_cfx_artifact(
        f"{second}\n\n{first}\n",
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=options,
    )
    proof = _proof(candidate)
    move_ids = {operation_id(item) for item in candidate.edit_operations if item.kind == "move"}

    assert candidate.xhtml == (
        "<table><thead><tr><th>H2</th></tr></thead><tbody><tr><td>B</td></tr></tbody></table>"
        "<table><thead><tr><th>H1</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#ff0000">A</td></tr></tbody></table>'
    )
    assert candidate.push_safe is True
    assert len({change.owner for change in proof.intended}) == 1
    assert proof.unclassified == ()
    assert {change.owner for change in proof.intended} <= move_ids


def test_deleted_leaf_tombstones_remain_owned_by_the_delete_operation() -> None:
    candidate = _candidate("<p>A</p><p>B</p>", "A\n")
    proof = _proof(candidate)
    deletion = next(item for item in candidate.edit_operations if item.kind == "delete")
    deletion_id = operation_id(deletion)
    removed = [change for change in proof.intended if change.after_fingerprint is None]

    assert removed
    assert {change.owner for change in removed} == {deletion_id}
    assert {change.identity for change in removed} == {
        (
            leaf.semantic_path,
            leaf.field,
            leaf.attribute_name,
            leaf.occurrence_ordinal,
        )
        for leaf in deletion.deleted_leaf_tombstones
    }


def test_two_insert_operations_use_a_real_parent_topology_operation() -> None:
    candidate = _candidate("<p>A</p>", "A\n\nB\n\nC\n")
    proof = _proof(candidate)

    assert len([item for item in candidate.edit_operations if item.kind == "insert"]) == 2
    topology_updates = [item for item in candidate.edit_operations if item.topology_only]
    assert len(topology_updates) == 1
    assert candidate.push_safe is True
    assert proof.blocking_code is None
    assert proof.multiple_owners == ()
    topology_changes = [change for change in proof.intended if change.identity[1] == "topology"]
    assert topology_changes
    assert topology_changes[0].owner == operation_id(topology_updates[0])


def test_move_plus_edit_is_not_silently_split_into_independent_owners() -> None:
    candidate = _candidate("<p>A</p><p>B</p>", "B changed\n\nA\n")
    proof = _proof(candidate)

    assert candidate.push_safe is False
    assert "semantic-mapping-ambiguous" in proof.fatal_diagnostic_codes


@pytest.mark.parametrize(
    "source",
    [
        "<p>A</p>",
        '<p><ac:image ac:alt="A"><ri:attachment ri:filename="a.png"/></ac:image></p>',
    ],
)
def test_last_block_delete_is_owned_only_by_its_tombstone_operation(source: str) -> None:
    base = _full_migration_base(source)
    candidate = _candidate(source, "")
    proof = _proof(candidate)

    assert base.markdown
    assert candidate.xhtml == ""
    assert candidate.push_safe is True
    assert [operation.kind for operation in candidate.edit_operations] == ["update", "delete"]
    deletion = next(operation for operation in candidate.edit_operations if operation.kind == "delete")
    assert deletion.deleted_leaf_tombstones
    assert not proof.unclassified
    assert not proof.multiple_owners
    assert not proof.overlapping


def test_concurrent_public_conversions_each_receive_their_own_nested_proof() -> None:
    cases = (("<p>A</p>", "A1\n"), ("<p>B</p>", "B1\n"))

    def convert(case: tuple[str, str]) -> tuple[str, str]:
        source, edited = case
        proof = _proof(_candidate(source, edited))
        return proof.artifacts.source_storage_sha256, proof.artifacts.edited_markdown_sha256

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(convert, cases))

    assert {item[0] for item in results} == {
        "sha256:" + hashlib.sha256(source.encode()).hexdigest() for source, _edited in cases
    }
    assert len({item[1] for item in results}) == 2


def test_public_artifact_exposes_an_immutable_source_bound_proof() -> None:
    storage = "<p>Before</p>"
    candidate = _candidate(storage, "After\n")
    proof = _proof(candidate)

    assert proof.artifacts.source_storage_sha256 == "sha256:" + hashlib.sha256(storage.encode()).hexdigest()
    assert proof.artifacts.base_markdown_sha256 != proof.artifacts.edited_markdown_sha256
    assert proof.artifacts.converter == f"cfxmark/{cfxmark.__version__}"
    assert proof.artifacts.profile == "editable"
    assert isinstance(proof.intended, tuple)
    assert isinstance(proof.migration, tuple)
    assert isinstance(proof.fatal_diagnostic_codes, tuple)
    with pytest.raises(FrozenInstanceError):
        proof.artifacts.profile = "legacy"


def test_most_specific_relocation_owner_drops_broad_ancestor() -> None:
    # A large edit records an ancestor topology relocation whose tombstones
    # expand over a broad downstream region; a strictly narrower relocation on
    # the same leaf must win so the leaf is singly owned.
    from cfxmark.ownership_proof import _most_specific_relocation_owners

    scope = {
        "broad": frozenset({("l1",), ("l2",), ("l3",)}),
        "narrow": frozenset({("l1",)}),
    }

    assert _most_specific_relocation_owners({"broad", "narrow"}, scope) == {"narrow"}


def test_most_specific_relocation_owner_reduces_nested_chain_to_one() -> None:
    from cfxmark.ownership_proof import _most_specific_relocation_owners

    scope = {
        "outer": frozenset({("l1",), ("l2",), ("l3",)}),
        "mid": frozenset({("l1",), ("l2",)}),
        "inner": frozenset({("l1",)}),
    }

    assert _most_specific_relocation_owners({"outer", "mid", "inner"}, scope) == {"inner"}


def test_most_specific_relocation_owner_keeps_incomparable_owners() -> None:
    # Two relocations that genuinely overlap without one containing the other
    # remain ambiguous, so a real overlapping move still fails closed downstream.
    from cfxmark.ownership_proof import _most_specific_relocation_owners

    scope = {
        "a": frozenset({("l1",), ("l2",)}),
        "b": frozenset({("l2",), ("l3",)}),
    }

    assert _most_specific_relocation_owners({"a", "b"}, scope) == {"a", "b"}


def test_most_specific_relocation_owner_keeps_equal_scopes() -> None:
    from cfxmark.ownership_proof import _most_specific_relocation_owners

    scope = {"a": frozenset({("l1",)}), "b": frozenset({("l1",)})}

    assert _most_specific_relocation_owners({"a", "b"}, scope) == {"a", "b"}


def test_block_and_list_reorder_stay_singly_owned() -> None:
    # The relocation-specificity rule must be a no-op for a genuine reorder: a
    # single move/reorder has one relocation owner per leaf, so ownership and
    # push-safety are unchanged.
    storage = "<h2>Sec</h2><p>lead</p><ul><li>uno</li><li>dos</li><li>tres</li></ul>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        "## Sec\n\n- tres\n- uno\n- dos\n\nlead\n",
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.ownership_proof is not None
    assert candidate.ownership_proof.multiple_owners == ()


def test_payload_free_block_run_substitution_is_push_safe() -> None:
    # Replacing one placeholder paragraph with several payload-free blocks (two
    # of them paragraphs) used to fail with semantic-mapping-ambiguous. The tie
    # is ownership-neutral, so the managed edit now converts push-safe with the
    # edit reflected and no contested ownership.
    storage = "<h2>Section</h2><p>placeholder text here</p>"
    base = _full_migration_base(storage)
    edited = (
        "## Section\n\n"
        "first replacement paragraph\n\n"
        "### Sub heading\n\n"
        "- point one\n- point two\n\n"
        "second replacement paragraph\n"
    )

    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    assert candidate.ownership_proof is not None
    assert candidate.ownership_proof.fatal is False
    assert candidate.ownership_proof.multiple_owners == ()
    assert "first replacement paragraph" in candidate.xhtml
    assert "second replacement paragraph" in candidate.xhtml
    # The independent validator re-derives ownership with the same relocation
    # rule as generation, so the push-safe candidate also passes preflight.
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


# ---------------------------------------------------------------------------
# Supersession witness: a leading empty paragraph dropped by migration and then
# deleted by the user overlaps on the derived p[0] topology leaf. The witness
# proves, in C0 space, that the user delete removed exactly the C0 node the
# migration created, so the overlap dissolves to a user edit instead of a fatal
# ambiguity. See /tmp/supersession_witness_design.md.
# ---------------------------------------------------------------------------

_LEADING_EMPTY_PARA_MACRO = (
    '<p /><ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p></ac:rich-text-body></ac:structured-macro>'
)


def _delete_leading_br(markdown: str) -> str:
    return "\n".join(line for line in markdown.split("\n") if line.strip() != "<br>")


def test_leading_empty_para_deletion_before_macro_is_witnessed_and_publishable() -> None:
    storage = _LEADING_EMPTY_PARA_MACRO
    base = _full_migration_base(storage)
    edited = _delete_leading_br(base.markdown)
    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    # Without the witness this is a fatal migration-user-overlap on (p[0], topology).
    assert candidate.push_safe is True
    assert proof.fatal is False
    assert proof.overlapping == ()
    assert proof.blocking_code is None
    # The dissolved leaf is now a user-owned intended change, backed by exactly
    # one supersession witness naming the migration occurrence and the user op.
    assert len(proof.supersession_witnesses) == 1
    witness = proof.supersession_witnesses[0]
    assert witness.final_leaf == (("p[0]",), "topology", None, 0)
    assert witness.tombstone_kind == "delete"
    assert any(change.identity == (("p[0]",), "topology", None, 0) for change in proof.intended)
    # The C0 <br> paragraph is gone; the macro survived unchanged.
    assert candidate.xhtml == (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p></ac:rich-text-body></ac:structured-macro>'
    )
    # Publish preflight independently re-derives and byte-compares the witness.
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


def _witnessed_candidate() -> cfxmark.CfxArtifact:
    base = _full_migration_base(_LEADING_EMPTY_PARA_MACRO)
    return cfxmark.to_cfx_artifact(
        _delete_leading_br(base.markdown),
        presentation=base.presentation,
        base_artifact=base,
        splice_source=_LEADING_EMPTY_PARA_MACRO,
        options=EDITABLE,
    )


def _reseal(candidate: cfxmark.CfxArtifact, ownership: Any) -> cfxmark.CfxArtifact:
    # Re-hash the ownership envelope so the tampering survives the sha256 binding
    # and must be caught by the independent witness re-derivation instead.
    return replace(candidate, ownership_proof=ownership, ownership_proof_sha256=ownership.sha256())


def test_supersession_witness_forged_migration_occurrence_id_is_rejected() -> None:
    candidate = _witnessed_candidate()
    ownership = _proof(candidate)
    (witness,) = ownership.supersession_witnesses
    tampered = replace(
        ownership,
        supersession_witnesses=(replace(witness, migration_occurrence_id="mig_occ_sha256:forged"),),
    )
    forged = _reseal(candidate, tampered)
    with pytest.raises(cfxmark.ConversionError, match="supersession witness"):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=_LEADING_EMPTY_PARA_MACRO, options=EDITABLE)


def test_supersession_witness_forged_user_operation_id_is_rejected() -> None:
    candidate = _witnessed_candidate()
    ownership = _proof(candidate)
    (witness,) = ownership.supersession_witnesses
    tampered = replace(
        ownership,
        supersession_witnesses=(replace(witness, user_operation_id="op-forged"),),
    )
    forged = _reseal(candidate, tampered)
    with pytest.raises(cfxmark.ConversionError, match="supersession witness"):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=_LEADING_EMPTY_PARA_MACRO, options=EDITABLE)


def test_supersession_witness_forged_c0_fingerprint_is_rejected() -> None:
    candidate = _witnessed_candidate()
    ownership = _proof(candidate)
    (witness,) = ownership.supersession_witnesses
    tampered = replace(
        ownership,
        supersession_witnesses=(replace(witness, c0_intermediate_fingerprint="sha256:deadbeef"),),
    )
    forged = _reseal(candidate, tampered)
    with pytest.raises(cfxmark.ConversionError, match="supersession witness"):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=_LEADING_EMPTY_PARA_MACRO, options=EDITABLE)


def test_supersession_witness_removed_but_leaf_kept_intended_is_rejected() -> None:
    # Dropping the witness while leaving the dissolved leaf in `intended` must not
    # pass: the independent re-derivation still produces the witness, so the stored
    # (empty) set no longer matches.
    candidate = _witnessed_candidate()
    ownership = _proof(candidate)
    tampered = replace(ownership, supersession_witnesses=())
    forged = _reseal(candidate, tampered)
    with pytest.raises(cfxmark.ConversionError, match="supersession witness"):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=_LEADING_EMPTY_PARA_MACRO, options=EDITABLE)


def test_witnessless_content_overlap_forced_into_intended_is_rejected() -> None:
    # The trust-gap closure: a genuine content overlap (emoticon vs a user rewrite)
    # forged into `intended` with the real user operation as owner and no witness.
    # The classifier check alone accepts it (the op truly touches the leaf); only
    # the "migration-owned intended leaf needs a witness" rule rejects it.
    storage = '<p><ac:emoticon ac:name="smile"/></p>'
    base = _full_migration_base(storage)
    candidate = cfxmark.to_cfx_artifact(
        "changed\n",
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    ownership = _proof(candidate)
    (overlap,) = ownership.overlapping
    assert overlap.identity[1] == "text"
    assert ownership.supersession_witnesses == ()
    (update_op,) = candidate.edit_operations
    forged_owner = OwnedChange(
        identity=overlap.identity,
        owner=operation_id(update_op),
        before_fingerprint=overlap.before_fingerprint,
        after_fingerprint=overlap.after_fingerprint,
    )
    tampered = replace(
        ownership,
        intended=(*ownership.intended, forged_owner),
        overlapping=(),
    )
    forged = replace(_reseal(candidate, tampered), push_safe=True)
    with pytest.raises(cfxmark.ConversionError):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_emoticon_overlap_produces_no_supersession_witness() -> None:
    # A content-transform overlap is never witness-eligible: the migration code is
    # not a structural reshape and the user op edits the value in place.
    storage = '<p><ac:emoticon ac:name="smile"/></p>'
    candidate = _candidate(storage, "changed\n")
    proof = _proof(candidate)
    assert proof.blocking_code == "migration-user-overlap-ambiguous"
    assert proof.supersession_witnesses == ()


def test_content_transform_adjacent_topology_overlap_is_not_witnessed() -> None:
    # A leading empty paragraph (structurally droppable) sits before an emoticon
    # paragraph. Deleting the blank line and rewriting the emoticon text is a real
    # content conflict that surfaces on a topology leaf whose migration owner is a
    # CONTENT transform (emoticon-to-unicode), not a structural reshape. The code
    # gate must refuse to witness it, so the candidate stays fatal.
    storage = '<p /><p><ac:emoticon ac:name="smile"/></p>'
    base = _full_migration_base(storage)
    edited = _delete_leading_br(base.markdown).replace("🙂", "changed")
    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = _proof(candidate)
    assert candidate.push_safe is False
    assert proof.blocking_code == "migration-user-overlap-ambiguous"
    # The overlap stays on a topology leaf owned by the emoticon content transform.
    assert any(change.identity[1] == "topology" for change in proof.overlapping)
    # Every minted witness is a delete over a topology leaf; the emoticon-owned
    # topology overlap is never among them (its migration code is not structural).
    assert all(witness.final_leaf[1] == "topology" for witness in proof.supersession_witnesses)
    assert all(witness.tombstone_kind == "delete" for witness in proof.supersession_witnesses)
    overlap_identities = {change.identity for change in proof.overlapping}
    witnessed_identities = {witness.final_leaf for witness in proof.supersession_witnesses}
    assert overlap_identities & witnessed_identities == set()


def test_witness_user_operation_is_always_a_real_delete() -> None:
    # The witness is deliberately delete-only: a relocation would leave the
    # migration-created C0 node alive elsewhere in C1, a separate ownership
    # question. Pin the contract against the actual operation objects, not just the
    # witness field -- every witness must name a real edit operation whose kind is
    # "delete" and whose deleted tombstones cover the dissolved leaf.
    candidate = _witnessed_candidate()
    proof = _proof(candidate)
    assert proof.supersession_witnesses  # the fixture does mint one
    operations = {operation_id(operation): operation for operation in candidate.edit_operations}
    for witness in proof.supersession_witnesses:
        assert witness.tombstone_kind == "delete"
        operation = operations[witness.user_operation_id]
        assert operation.kind == "delete"
        deleted = {
            (tombstone.semantic_path, tombstone.field, tombstone.attribute_name, tombstone.occurrence_ordinal)
            for tombstone in operation.deleted_leaf_tombstones
        }
        assert witness.final_leaf in deleted


def test_duplicate_supersession_witness_for_one_leaf_is_rejected() -> None:
    # A forged witness paired with the valid one for the SAME leaf must not slip
    # through: the validator compares the exact canonical ordered tuple, not a
    # final_leaf-keyed dict that would silently collapse the duplicate.
    candidate = _witnessed_candidate()
    ownership = _proof(candidate)
    (valid,) = ownership.supersession_witnesses
    forged = replace(valid, migration_occurrence_id="mig_occ_sha256:forged")
    for pair in ((forged, valid), (valid, forged)):
        tampered = replace(ownership, supersession_witnesses=pair)
        artifact = _reseal(candidate, tampered)
        with pytest.raises(cfxmark.ConversionError, match="duplicate final leaf"):
            cfxmark.validate_managed_cfx_artifact(artifact, source_storage=_LEADING_EMPTY_PARA_MACRO, options=EDITABLE)
