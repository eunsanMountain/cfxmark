from __future__ import annotations

import hashlib
from dataclasses import fields, replace

import pytest

import cfxmark
from cfxmark.migration_report import MigrationReport
from cfxmark.ownership_proof import attribution_witness_sha256
from cfxmark.source_map import build_candidate_source_maps

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def test_public_candidate_report_and_proof_share_real_source_bound_occurrence_ids() -> None:
    storage = '<p><ac:emoticon ac:name="smile"/></p>'
    base = replace(cfxmark.to_md_artifact(storage, options=EDITABLE), protected_regions=(), remote_subtrees=())
    candidate = cfxmark.to_cfx_artifact(
        "changed\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    proof = candidate.ownership_proof
    assert proof is not None
    report_ids = {occurrence.occurrence_id for occurrence in candidate.migration_report.occurrences}
    assert proof.migration_occurrence_ids
    assert set(proof.migration_occurrence_ids) <= report_ids
    assert set(proof.migration_occurrence_ids) <= {
        occurrence.occurrence_id for occurrence in base.migration_report.occurrences
    }


def test_public_validator_rejects_a_candidate_with_a_rebound_ownership_hash() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof
    assert proof is not None
    forged = replace(
        candidate,
        ownership_proof=replace(
            proof,
            artifacts=replace(proof.artifacts, final_candidate_sha256="sha256:" + "0" * 64),
        ),
    )

    with pytest.raises(cfxmark.ConversionError, match="ownership proof is not bound"):
        cfxmark.validate_cfx_artifact(forged, source_storage=storage)


def test_public_validator_requires_the_source_bound_proof_envelope() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    stripped = replace(
        candidate,
        ownership_proof_required=False,
        source_storage_sha256=None,
        source_migration_report=None,
        ownership_proof=None,
        ownership_proof_sha256=None,
    )

    with pytest.raises(cfxmark.ConversionError, match="managed artifact type"):
        cfxmark.validate_cfx_artifact(stripped, source_storage=storage)


def test_public_validator_rejects_a_proof_rebound_to_another_source() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof
    assert proof is not None
    forged_proof = replace(
        proof,
        artifacts=replace(proof.artifacts, source_storage_sha256="sha256:" + "0" * 64),
    )

    with pytest.raises(cfxmark.ConversionError, match="not bound to its source storage"):
        cfxmark.validate_cfx_artifact(
            replace(candidate, ownership_proof=forged_proof, ownership_proof_sha256=forged_proof.sha256()),
            source_storage=storage,
        )


def test_public_validator_rejects_an_emptied_ownership_proof() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof
    assert proof is not None
    assert proof.intended
    emptied = replace(
        proof,
        intended=(),
        migration=(),
        unclassified=(),
        multiple_owners=(),
        overlapping=(),
        fatal_diagnostic_codes=(),
    )

    with pytest.raises(cfxmark.ConversionError, match="does not classify the complete final storage diff"):
        cfxmark.validate_cfx_artifact(
            replace(candidate, ownership_proof=emptied, ownership_proof_sha256=emptied.sha256()),
            source_storage=storage,
        )

    fully_emptied = replace(emptied, final_changes=())
    with pytest.raises(cfxmark.ConversionError, match="complete final storage diff"):
        cfxmark.validate_cfx_artifact(
            replace(
                candidate,
                ownership_proof=fully_emptied,
                ownership_proof_sha256=fully_emptied.sha256(),
                ownership_final_change_count=0,
            ),
            source_storage=storage,
        )


def test_public_validator_rejects_an_intended_leaf_rebound_to_another_real_operation() -> None:
    storage = "<p>A</p><p>B</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "A1\n\nB1\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    assert len(proof.intended) == 2
    first, second = proof.intended
    assert first.owner != second.owner
    forged_proof = replace(proof, intended=(replace(first, owner=second.owner), second))
    forged = replace(
        candidate,
        ownership_proof=forged_proof,
        ownership_proof_sha256=forged_proof.sha256(),
    )

    with pytest.raises(
        cfxmark.ConversionError,
        match="independent remote-splice replay|independent semantic owners|named edit operation",
    ):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_public_validator_rejects_a_migration_report_that_hides_a_disclosed_loss() -> None:
    # An XML comment is a disclosure-type migration loss: it is reported for
    # consent but owns no change in the ownership proof, so the subset check on
    # migration occurrence ids passes vacuously for an emptied report.  The
    # report itself must be re-derived from fresh source storage, otherwise a
    # candidate could hide a disclosed loss and publish it without consent.
    storage = "<p>a<!-- INTERNAL NOTE do-not-publish --></p><p>b</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.source_migration_report is not None
    assert [occurrence.code for occurrence in candidate.source_migration_report.occurrences] == ["xml-comment-dropped"]

    emptied = MigrationReport.build(occurrences=())
    forged = replace(candidate, source_migration_report=emptied)
    assert forged.source_migration_report.sha256() != candidate.source_migration_report.sha256()

    with pytest.raises(
        cfxmark.ConversionError, match="source migration report or ownership does not match fresh source"
    ):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_public_validator_rejects_a_migration_leaf_relabelled_to_another_real_occurrence() -> None:
    # The table background is a real reported occurrence but owns no storage
    # migration leaf.  Rebinding a layout leaf to it keeps the owner a genuine
    # report ID, so the old subset check accepted it after envelope rehashing.
    storage = (
        "<ac:layout><ac:layout-section><ac:layout-cell><p>A</p>"
        "</ac:layout-cell></ac:layout-section></ac:layout>"
        '<table><tbody><tr><td style="background-color: red">B</td></tr></tbody></table>'
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.ownership_proof is not None
    assert candidate.source_migration_report is not None
    replacement_owner = next(
        occurrence.occurrence_id
        for occurrence in candidate.source_migration_report.occurrences
        if occurrence.code == "table-cell-background-omitted"
    )
    relabelled_proof = replace(
        candidate.ownership_proof,
        migration=(
            replace(candidate.ownership_proof.migration[0], owner=replacement_owner),
            *candidate.ownership_proof.migration[1:],
        ),
    )
    forged = replace(
        candidate,
        ownership_proof=relabelled_proof,
        ownership_proof_sha256=relabelled_proof.sha256(),
    )

    with pytest.raises(
        cfxmark.ConversionError, match="source migration report or ownership does not match fresh source"
    ):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_public_validator_reconstructs_candidate_attribution_instead_of_trusting_a_self_consistent_forgery() -> None:
    storage = "<p>A</p><p>B</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "A1\n\nB1\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    first, second = proof.intended

    def identity(leaf: cfxmark.StorageLeafRef) -> tuple[tuple[str, ...], str, str | None, int]:
        return leaf.semantic_path, leaf.field, leaf.attribute_name, leaf.occurrence_ordinal

    entries = list(candidate.source_map_entries)
    first_index = next(index for index, entry in enumerate(entries) if first.identity in map(identity, entry.leaves))
    second_index = next(index for index, entry in enumerate(entries) if second.identity in map(identity, entry.leaves))
    first_entry = entries[first_index]
    second_entry = entries[second_index]
    entries[first_index] = replace(
        first_entry,
        operation_id=second_entry.operation_id,
        node_or_occurrence_id=second_entry.node_or_occurrence_id,
    )
    entries[second_index] = replace(
        second_entry,
        operation_id=first_entry.operation_id,
        node_or_occurrence_id=first_entry.node_or_occurrence_id,
    )
    source_map_entries = tuple(entries)
    operation_to_storage, storage_to_operation = build_candidate_source_maps(source_map_entries)
    forged_proof = replace(
        proof,
        intended=(replace(first, owner=second.owner), replace(second, owner=first.owner)),
    )
    forged = replace(
        candidate,
        source_map_entries=source_map_entries,
        edited_operation_to_storage=operation_to_storage,
        candidate_storage_to_operation=storage_to_operation,
        ownership_proof=forged_proof,
        ownership_proof_sha256=forged_proof.sha256(),
    )

    with pytest.raises(
        cfxmark.ConversionError,
        match="independent remote-splice replay|independent semantic owners|named edit operation",
    ):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_publish_preflight_reconstructs_baseline_instead_of_trusting_its_witness() -> None:
    storage = "<p>A</p><p>B</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    assert candidate.ownership_baseline_source_map_entries
    truncated = candidate.ownership_baseline_source_map_entries[:-1]
    witness_sha256 = attribution_witness_sha256(
        base_source_map_entries=candidate.ownership_base_source_map_entries or (),
        base_semantic_nodes=candidate.ownership_base_semantic_nodes or (),
        baseline_source_map_entries=truncated,
        baseline_semantic_nodes=candidate.ownership_baseline_semantic_nodes or (),
        baseline_matches_source_storage=bool(candidate.ownership_baseline_matches_source_storage),
        attachment_filename_map=candidate.ownership_attachment_filename_map or (),
        remote_subtrees=candidate.ownership_remote_subtrees or (),
        presentation_authority=candidate.ownership_presentation_authority or cfxmark.PresentationArtifact(),
        options_sha256=candidate.ownership_options_sha256 or "",
    )
    forged_proof = replace(
        proof,
        artifacts=replace(proof.artifacts, attribution_witness_sha256=witness_sha256),
    )
    forged = replace(
        candidate,
        ownership_baseline_source_map_entries=truncated,
        ownership_proof=forged_proof,
        ownership_proof_sha256=forged_proof.sha256(),
    )

    with pytest.raises(cfxmark.ConversionError, match="baseline witness does not match fresh C0 replay"):
        cfxmark.validate_cfx_artifact(forged, source_storage=storage)
    with pytest.raises(cfxmark.ConversionError, match="baseline witness does not match fresh C0 replay"):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_public_validator_does_not_let_an_unrelated_protected_splice_weaken_leaf_identity() -> None:
    raw_macro = (
        '<ac:structured-macro ac:name="mystery">'
        "<ac:plain-text-body><![CDATA[secret <x>]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    storage = raw_macro + "<p>A</p><p>B</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.managed_markdown.replace("A\n\nB", "X\n\nX"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    assert candidate.preserved_regions or candidate.preserved_subtrees
    assert len(proof.intended) == 2
    first, second = proof.intended
    assert first.owner != second.owner
    assert first.identity != second.identity

    def identity(leaf: cfxmark.StorageLeafRef) -> tuple[tuple[str, ...], str, str | None, int]:
        return leaf.semantic_path, leaf.field, leaf.attribute_name, leaf.occurrence_ordinal

    entries = list(candidate.source_map_entries)
    first_index = next(index for index, entry in enumerate(entries) if first.identity in map(identity, entry.leaves))
    second_index = next(index for index, entry in enumerate(entries) if second.identity in map(identity, entry.leaves))
    first_entry = entries[first_index]
    second_entry = entries[second_index]
    first_leaf = next(leaf for leaf in first_entry.leaves if identity(leaf) == first.identity)
    second_leaf = next(leaf for leaf in second_entry.leaves if identity(leaf) == second.identity)
    entries[first_index] = replace(
        first_entry,
        leaves=tuple(
            replace(leaf, semantic_path=second_leaf.semantic_path) if leaf is first_leaf else leaf
            for leaf in first_entry.leaves
        ),
    )
    entries[second_index] = replace(
        second_entry,
        leaves=tuple(
            replace(leaf, semantic_path=first_leaf.semantic_path) if leaf is second_leaf else leaf
            for leaf in second_entry.leaves
        ),
    )
    source_map_entries = tuple(entries)
    operation_to_storage, storage_to_operation = build_candidate_source_maps(source_map_entries)
    forged_proof = replace(
        proof,
        intended=(replace(first, owner=second.owner), replace(second, owner=first.owner)),
    )
    forged = replace(
        candidate,
        source_map_entries=source_map_entries,
        edited_operation_to_storage=operation_to_storage,
        candidate_storage_to_operation=storage_to_operation,
        ownership_proof=forged_proof,
        ownership_proof_sha256=forged_proof.sha256(),
    )

    with pytest.raises(cfxmark.ConversionError, match="independent remote-splice replay|independent semantic owners"):
        cfxmark.validate_managed_cfx_artifact(forged, source_storage=storage, options=EDITABLE)


def test_public_ownership_source_snapshot_contains_only_fingerprints() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    assert proof.source_leaves
    assert all(snapshot.value_fingerprint.startswith("sha256:") for snapshot in proof.source_leaves)
    assert "Before" not in proof.canonical_json()
    assert "After" not in proof.canonical_json()


def test_public_validator_rejects_source_snapshot_rebound_to_candidate() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    candidate_leaf_fingerprints = {
        leaf.identity: leaf.after_fingerprint for leaf in proof.final_changes if leaf.after_fingerprint is not None
    }
    rebound_snapshots = tuple(
        replace(
            snapshot, value_fingerprint=candidate_leaf_fingerprints.get(snapshot.identity, snapshot.value_fingerprint)
        )
        for snapshot in proof.source_leaves
    )
    forged_proof = replace(
        proof,
        source_leaves=rebound_snapshots,
        final_changes=(),
        intended=(),
        migration=(),
        unclassified=(),
        multiple_owners=(),
        overlapping=(),
        fatal_diagnostic_codes=(),
        preserved_count=0,
    )
    forged = replace(
        candidate,
        ownership_proof=forged_proof,
        ownership_proof_sha256=forged_proof.sha256(),
        ownership_final_change_count=0,
    )

    with pytest.raises(cfxmark.ConversionError, match="source snapshot does not match fresh source"):
        cfxmark.validate_cfx_artifact(forged, source_storage=storage)


def test_publish_preflight_rejects_manual_artifact_demotion() -> None:
    storage = "<p>Before</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    managed = cfxmark.to_cfx_artifact(
        "After\n",
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    demoted_fields = {item.name: getattr(managed, item.name) for item in fields(cfxmark.CfxArtifact)}
    demoted_fields.update(
        ownership_proof_required=False,
        source_storage_sha256=None,
        source_migration_report=None,
        ownership_proof=None,
        ownership_proof_sha256=None,
        ownership_final_change_count=None,
        ownership_base_source_map_entries=None,
        ownership_base_semantic_nodes=None,
        ownership_baseline_source_map_entries=None,
        ownership_baseline_semantic_nodes=None,
        ownership_baseline_matches_source_storage=None,
        ownership_attachment_filename_map=None,
        ownership_remote_subtrees=None,
        ownership_presentation_authority=None,
        ownership_options=None,
        ownership_options_sha256=None,
    )
    demoted = cfxmark.CfxArtifact(**demoted_fields)

    cfxmark.validate_cfx_artifact(demoted)
    with pytest.raises(cfxmark.ConversionError, match="publish preflight requires a managed source-bound candidate"):
        cfxmark.validate_managed_cfx_artifact(demoted, source_storage=storage, options=EDITABLE)


def test_publish_preflight_requires_exact_fresh_source_and_five_hashes() -> None:
    storage = "<p>Before</p>"
    edited = "After\n"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    baseline = cfxmark.to_cfx_artifact(
        base.markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    candidate = cfxmark.to_cfx_artifact(
        edited,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof

    assert proof is not None
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)
    with pytest.raises(TypeError):
        cfxmark.validate_managed_cfx_artifact(candidate)  # type: ignore[call-arg]
    with pytest.raises(cfxmark.ConversionError, match="stale or does not match"):
        cfxmark.validate_managed_cfx_artifact(
            candidate,
            source_storage="<p>Stale</p>",
            options=EDITABLE,
        )
    assert proof.artifacts.source_storage_sha256 == "sha256:" + hashlib.sha256(storage.encode()).hexdigest()
    assert proof.artifacts.base_markdown_sha256 == "sha256:" + hashlib.sha256(base.markdown.encode()).hexdigest()
    assert proof.artifacts.edited_markdown_sha256 == "sha256:" + hashlib.sha256(edited.encode()).hexdigest()
    assert proof.artifacts.final_candidate_sha256 == "sha256:" + hashlib.sha256(candidate.xhtml.encode()).hexdigest()
    assert proof.artifacts.baseline_candidate_sha256 == "sha256:" + hashlib.sha256(baseline.xhtml.encode()).hexdigest()
    # Derived, not pinned: a release must not need a test edit here, and a
    # stale literal turns every bump into a failure that has to be dismissed
    # by hand — with whatever real regression is hiding behind it.
    assert proof.artifacts.converter == f"cfxmark/{cfxmark.__version__}"
    assert proof.artifacts.profile == "editable"


def test_publish_preflight_rejects_a_fatal_source_bound_proof() -> None:
    # An unknown *element* dropped next to a real occurrence stays ambiguous
    # and fatal (the attribute-only drop is now a named consentable occurrence,
    # so it no longer serves as this gate's fixture).
    storage = '<p><foo data-secret="x">Visible</foo> <ac:emoticon ac:name="smile"/></p>'
    base = replace(cfxmark.to_md_artifact(storage, options=EDITABLE), protected_regions=(), remote_subtrees=())
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is False
    assert candidate.ownership_proof is not None
    assert candidate.ownership_proof.fatal is True
    with pytest.raises(cfxmark.ConversionError, match="fatal ownership proof"):
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


@pytest.mark.parametrize(
    "edited",
    (
        "A\n\nB\n\nC\n\nD\n\nE\n\nF\n",
        "A\n\nB\n",
        "A\n\nB changed\n\nC\n\nD\n\nE\n",
    ),
)
def test_public_publish_preflight_accepts_common_multi_block_edits(edited: str) -> None:
    storage = "<p>A</p><p>B</p><p>C</p><p>D</p>"
    base = replace(cfxmark.to_md_artifact(storage, options=EDITABLE), protected_regions=(), remote_subtrees=())
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
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


@pytest.mark.parametrize(
    "edited",
    (
        "_ital_\n",
        "__bold__\n",
        "* one\n",
        "1) one\n",
        "Title\n=====\n",
        "~~~\ncode\n~~~\n",
        "<https://a.example>\n",
        "# T #\n",
    ),
)
def test_public_structural_replacements_have_a_unique_delete_insert_mapping(edited: str) -> None:
    storage = "<p>Base</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
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
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


def test_managed_header_does_not_rebase_body_source_ranges() -> None:
    storage = (
        '<ac:structured-macro ac:name="mystery">'
        "<ac:plain-text-body><![CDATA[secret <x>]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)
    region = artifact.protected_regions[0]
    projection = artifact.markdown[region.base_markdown_range.start : region.base_markdown_range.end]

    assert projection == "[Unsupported Confluence content]"
    assert artifact.managed_markdown != artifact.markdown
    assert artifact.managed_markdown.endswith(artifact.markdown)


def test_unknown_macro_is_verbatim_spliced_and_publicly_validated() -> None:
    raw_macro = (
        '<ac:structured-macro ac:name="mystery">'
        "<ac:plain-text-body><![CDATA[secret <x>]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    storage = raw_macro + "<p>After</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.managed_markdown.replace("After", "Edited"),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert raw_macro in candidate.xhtml
    assert candidate.preserved_subtrees or candidate.preserved_regions
    assert candidate.splice_proof_complete is True
    assert candidate.ownership_proof is not None
    assert candidate.ownership_proof.fatal is False
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


def test_table_background_notice_matches_exact_source_bound_preservation() -> None:
    storage = '<table><tbody><tr><td style="background-color:#ff0000">A</td></tr></tbody></table>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.managed_markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    occurrence = next(
        item for item in base.migration_report.occurrences if item.code == "table-cell-background-omitted"
    )
    assert occurrence.effect == "preserved"
    assert "effect=preserved" in base.managed_markdown
    assert candidate.xhtml == storage
    assert candidate.migration_report.consent_required is False
    assert '"occurrences":[]' in candidate.migration_report.canonical_json()
    cfxmark.validate_cfx_artifact(candidate, source_storage=storage)


def test_ownership_models_are_public_for_preflight_consumers() -> None:
    assert cfxmark.ChangeArtifacts.__module__ == "cfxmark.change_ownership"
    assert cfxmark.OwnershipResult.__module__ == "cfxmark.change_ownership"
    assert cfxmark.LeafChange.__module__ == "cfxmark.change_ownership"
    assert cfxmark.OwnedChange.__module__ == "cfxmark.change_ownership"
    assert cfxmark.LeafSnapshot.__module__ == "cfxmark.change_ownership"
    assert cfxmark.ManagedCfxArtifact.__module__ == "cfxmark.artifacts"
    assert cfxmark.validate_managed_cfx_artifact.__module__ == "cfxmark.artifacts"


def test_storage_semantically_equal_ignores_serialization_differences() -> None:
    # Confluence re-serializes storage on save: quote characters come back as
    # &quot; entities and void elements gain a space before the slash. These are
    # serialization-only and must compare equal, so a byte-hash read-back check
    # would wrongly reject an unchanged save.
    assert cfxmark.storage_semantically_equal('<p>say "hi" now</p>', "<p>say &quot;hi&quot; now</p>") is True
    assert cfxmark.storage_semantically_equal("<p>a</p><hr/><p>b</p>", "<p>a</p><hr /><p>b</p>") is True


def test_storage_semantically_equal_detects_real_changes() -> None:
    # Content, structure, and attribute changes must break equality, or the
    # read-back check would accept a server-side modification of the candidate.
    assert cfxmark.storage_semantically_equal("<p>hello</p>", "<p>HELLO</p>") is False
    assert (
        cfxmark.storage_semantically_equal(
            '<p>a</p><ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[x]]></ac:plain-text-body></ac:structured-macro>",
            "<p>a</p>",
        )
        is False
    )


def _jira_macro(inner: str, *, schema: str = "1", macro_id: str | None = None, name: str = "jira") -> str:
    mid = f' ac:macro-id="{macro_id}"' if macro_id is not None else ""
    return f'<ac:structured-macro ac:name="{name}" ac:schema-version="{schema}"{mid}>{inner}</ac:structured-macro>'


def test_readback_tolerates_server_added_macro_id_but_never_a_changed_one() -> None:
    # storage_readback_equivalent(expected=candidate, observed=server_readback).
    # Confluence stamps a fresh ac:macro-id on save; a Markdown-first candidate
    # carries none, so read-back must tolerate that ADDED id (the concrete cause
    # of page_*_readback_mismatch on jira-macro Story pages). But a managed
    # round-trip that reuses an existing macro DOES carry its id, and that id is
    # remote-backed instance identity (comments, permissions, attachments), so
    # the tolerance is one-directional: an existing id must survive unchanged.
    key = '<ac:parameter ac:name="key">DOC-1</ac:parameter>'
    server_id = "7f3ac0de-0dd6-46bb-b08e-0001"
    candidate_no_id = _jira_macro(key)
    candidate_with_id = _jira_macro(key, macro_id=server_id)
    server_with_id = _jira_macro(key, macro_id=server_id)
    server_other_id = _jira_macro(key, macro_id="7f3ac0de-0dd6-46bb-b08e-9999")
    server_no_id = _jira_macro(key)

    # (1) candidate set no id, server added one -> tolerated (but the STRICT
    # proof comparator, where macro-id is remote-backed identity, never is).
    assert cfxmark.storage_readback_equivalent(candidate_no_id, server_with_id) is True
    assert cfxmark.storage_semantically_equal(candidate_no_id, server_with_id) is False
    # (2) existing id preserved unchanged -> tolerated.
    assert cfxmark.storage_readback_equivalent(candidate_with_id, server_with_id) is True
    # (3) existing id CHANGED by the server -> rejected (instance rebind).
    assert cfxmark.storage_readback_equivalent(candidate_with_id, server_other_id) is False
    # (4) existing id REMOVED by the server -> rejected (instance lost).
    assert cfxmark.storage_readback_equivalent(candidate_with_id, server_no_id) is False

    # Every other genuine difference still breaks equality even alongside the
    # tolerated server-added id:
    key2 = '<ac:parameter ac:name="key">DOC-2</ac:parameter>'
    assert cfxmark.storage_readback_equivalent(candidate_no_id, _jira_macro(key2, macro_id=server_id)) is False  # param
    assert (
        cfxmark.storage_readback_equivalent(candidate_no_id, _jira_macro(key, macro_id=server_id, schema="2")) is False
    )  # schema
    assert (
        cfxmark.storage_readback_equivalent(candidate_no_id, _jira_macro(key, macro_id=server_id, name="info")) is False
    )  # name
    # list start is visible content (numbering), not server transport metadata.
    assert cfxmark.storage_readback_equivalent('<ol start="2"><li>a</li></ol>', "<ol><li>a</li></ol>") is False


def test_adjacent_plain_inline_code_runs_are_a_serialization_equivalence() -> None:
    # Markdown cannot represent two adjacent inline code spans without a separator,
    # so a managed round-trip coalesces <code>a</code><code>b</code> into
    # <code>ab</code>. The rendered code text is identical, so this is a
    # serialization equivalence that must compare equal.
    assert cfxmark.storage_semantically_equal("<p><code>a</code><code>b</code></p>", "<p><code>ab</code></p>") is True
    # A separator (even a single space) makes them distinct visible runs, which
    # markdown *can* represent, so they must NOT be coalesced.
    assert cfxmark.storage_semantically_equal("<p><code>a</code> <code>b</code></p>", "<p><code>ab</code></p>") is False


def test_attributed_adjacent_inline_code_is_not_coalesced() -> None:
    # The storage extractor treats attributes as their own proof leaves, so
    # coalescing an attributed span would silently drop one. Attributed adjacent
    # code is therefore never a serialization equivalence.
    attributed = '<p><code data-x="1">a</code><code data-y="2">b</code></p>'
    assert cfxmark.storage_semantically_equal(attributed, "<p><code>ab</code></p>") is False
    # A full-migration round-trip of attributed adjacent code cannot preserve
    # the attributes in markdown. The losses are now *named* per-attribute
    # occurrences (effect "removed", so publish stays consent-gated) and only
    # then is the attribute-free coalescing a legitimate serialization
    # equivalence — the loss is surfaced, never laundered.
    base = replace(cfxmark.to_md_artifact(attributed, options=EDITABLE), protected_regions=(), remote_subtrees=())
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=attributed,
        options=EDITABLE,
    )
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is False and not proof.unclassified
    report = candidate.source_migration_report
    assert report is not None
    omitted = [o for o in report.occurrences if o.code == "element-attribute-omitted"]
    assert len(omitted) == 2 and all(o.effect == "removed" for o in omitted)
