"""Artifact models and deterministic AST/source-map construction."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, TypeGuard, cast

import lxml.etree as ET

from cfxmark.ast import (
    CellBackground,
    CodeBlock,
    DirectiveMacro,
    Document,
    Image,
    InlineDirectiveMacro,
    Node,
    TableCell,
)
from cfxmark.change_ownership import LeafIdentity, OwnershipResult
from cfxmark.diagnostics import (
    ConversionDiagnostic,
    DiagnosticCategory,
    DiagnosticLocation,
    DiagnosticSeverity,
    make_diagnostic,
)
from cfxmark.exceptions import (
    PUBLIC_CONVERSION_REASON_CODES,
    ConversionError,
    DiagnosticSummary,
    OwnershipProofError,
    OwnershipProofSummary,
)
from cfxmark.migration_comments import render_migration_marker
from cfxmark.migration_report import MigrationReport, occurrences_from_diagnostics
from cfxmark.parsers.cfx import _parse_fragment_to_element, _serialize_element
from cfxmark.presentation import (
    PresentationArtifact,
    apply_presentation_with_authority,
    presentation_cell_occurrences,
)
from cfxmark.renderers.cfx import render_cfx_with_source_map
from cfxmark.source_map import (
    EditOperation,
    SemanticMappingAmbiguous,
    SemanticNode,
    SourceMapEntry,
    StorageLeafRef,
    build_base_source_maps,
    build_candidate_source_maps,
    canonical_move_operation_ids,
    fingerprint_semantic_value,
    match_edit_operations,
    operation_id,
    stable_node_id,
)
from cfxmark.storage_map import extract_storage_source, storage_leaf_identity
from cfxmark.xml_ns import storage_node_name, storage_semantic_path, strip_ns

if TYPE_CHECKING:
    from cfxmark.api import ConversionOptions

ARTIFACT_SCHEMA_VERSION = 4
_PROTECTED_SEMANTIC_FIELDS = {"macro_id", "macro_schema_version", "source_path"}


def _is_semantic_field(node: Node, field_name: str) -> bool:
    if field_name in _PROTECTED_SEMANTIC_FIELDS:
        return False
    # A marker-bound image keeps its local transport path in ``src`` and its
    # storage identity in ``attachment_filename``. Rebasing the sidecar path
    # changes asset I/O only; it does not describe a Confluence body edit.
    return not (isinstance(node, Image) and node.attachment_filename is not None and field_name == "src")


@dataclass(frozen=True)
class MarkdownRange:
    """Half-open source range in the managed Markdown projection."""

    start: int
    end: int


@dataclass(frozen=True)
class ProtectedRegionArtifact:
    """Identity and edit policy for a remote-backed Markdown projection."""

    occurrence_id: str
    kind: str
    remote_node_path: tuple[str, ...]
    base_markdown_range: MarkdownRange
    display_label: str
    projection_fingerprint: str
    remote_subtree_fingerprint: str
    edit_policy: Literal["preserve_only", "task_status_only"]
    next_action: str
    editable: Literal[False] = False


@dataclass(frozen=True)
class ProtectedRegionChange:
    """A fail-closed edit, move, delete, or ambiguous protected projection."""

    occurrence_id: str
    kind: Literal["edit", "move", "delete", "ambiguous"]
    base_markdown_range: MarkdownRange
    current_markdown_range: MarkdownRange | None
    display_label: str
    reason: str
    next_action: str


@dataclass(frozen=True)
class TaskStatusChange:
    """One uniquely matched Confluence task status toggle."""

    occurrence_id: str
    task_index: int
    before: Literal["incomplete", "complete"]
    after: Literal["incomplete", "complete"]
    base_markdown_range: MarkdownRange
    current_markdown_range: MarkdownRange
    remote_node_path: tuple[str, ...]


@dataclass(frozen=True)
class RemoteSubtreeArtifact:
    """Fresh-remote-backed identity for an editable top-level Markdown block."""

    node_id: str
    remote_node_path: tuple[str, ...]
    base_markdown_range: MarkdownRange
    projection_fingerprint: str
    remote_subtree_fingerprint: str
    requires_fresh_source: bool = False


@dataclass(frozen=True)
class RemoteSubtreeProof:
    """Final candidate path and value fingerprint for one spliced subtree."""

    node_id: str
    candidate_node_path: tuple[str, ...]
    remote_subtree_fingerprint: str


@dataclass(frozen=True)
class ProtectedRegionProof:
    """Final candidate path and value fingerprint for one protected occurrence."""

    occurrence_id: str
    candidate_node_path: tuple[str, ...]
    remote_subtree_fingerprint: str


@dataclass(frozen=True)
class MacroIdentity:
    node_id: str
    macro_id: str | None
    schema_version: str | None = None


def _table_block_starts(lines: list[str]) -> list[int]:
    """Line index where each Markdown table begins, in document order.

    Fenced code is skipped: a pipe table inside a fence is documentation the
    user wrote, and anchoring a marker to it would put the annotation somewhere
    the reader cannot act on.
    """
    starts: list[int] = []
    in_fence = False
    previous_was_row = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            previous_was_row = False
            continue
        is_row = not in_fence and stripped.startswith("|")
        if is_row and not previous_was_row:
            starts.append(index)
        previous_was_row = is_row
    return starts


def _annotate_blocks(
    markdown: str,
    report: MigrationReport,
    presentation: PresentationArtifact,
) -> tuple[str, list[str]]:
    """Insert each migration marker above the block it describes.

    Returns the annotated body and the markers that had nowhere to go. Only
    table scopes can be located today, because a table is the one block whose
    identity survives into the rendered Markdown well enough to anchor against.
    Anything else is handed back for the header rather than guessed at -- a
    marker pointing at the wrong paragraph is worse than one the reader has to
    look up, since it would attribute a loss to content that did not suffer it.
    """
    groups = report.grouped()
    if not groups:
        return markdown, []

    ordinal_by_table = {table.table_id: index for index, table in enumerate(presentation.tables)}
    lines = markdown.split("\n")
    starts = _table_block_starts(lines)

    insertions: dict[int, list[str]] = {}
    orphans: list[str] = []
    for group in groups:
        marker = render_migration_marker(group)
        scope = group.scope.name if group.scope is not None else None
        ordinal = ordinal_by_table.get(scope[len("table:") :]) if scope and scope.startswith("table:") else None
        if ordinal is None or ordinal >= len(starts):
            orphans.append(marker)
            continue
        insertions.setdefault(starts[ordinal], []).append(marker)

    if not insertions:
        return markdown, orphans

    out: list[str] = []
    for index, line in enumerate(lines):
        for marker in insertions.get(index, ()):
            out.append(marker)
        if index in insertions:
            # The blank line the comment needs so it does not absorb the table
            # below it. strip_control_comments consumes exactly this one.
            out.append("")
        out.append(line)
    return "\n".join(out), orphans


@dataclass(frozen=True)
class MarkdownArtifact:
    schema_version: int
    status: Literal["ready_to_edit", "needs_migration"]
    markdown: str
    presentation: PresentationArtifact
    diagnostics: tuple[ConversionDiagnostic, ...]
    document: Document
    push_safe: bool
    base_storage_to_node: dict[StorageLeafRef, str]
    base_node_to_storage: dict[str, tuple[StorageLeafRef, ...]]
    semantic_nodes: tuple[SemanticNode, ...]
    source_map_entries: tuple[SourceMapEntry, ...]
    unmapped_storage_leaves: tuple[StorageLeafRef, ...]
    source_storage_sha256: str
    attachments: tuple[str, ...] = ()
    protected_regions: tuple[ProtectedRegionArtifact, ...] = ()
    remote_subtrees: tuple[RemoteSubtreeArtifact, ...] = ()
    macro_identities: tuple[MacroIdentity, ...] = ()
    disclosure: str = ""

    @property
    def managed_markdown(self) -> str:
        """The document a user edits: disclosure header, then annotated body.

        `markdown` stays the body alone because every range this artifact
        carries is measured against it. Composing here rather than storing a
        prefixed string keeps those ranges meaningful and keeps one obvious
        answer to "what do I write to disk".

        Markers are placed next to the block they describe rather than piled in
        the header. A reader scrolling to a table needs to see there that its
        colours will not survive; a list of scopes at the top is a summary they
        have to resolve by hand. Markers whose scope cannot be located in the
        body fall back to the header, where the scope field still names them.
        """
        body, orphans = _annotate_blocks(self.markdown, self.migration_report, self.presentation)
        header = [self.disclosure] if self.disclosure else []
        header.extend(orphans)
        if not header:
            return body
        return "\n".join(header) + "\n\n" + body

    @property
    def migration_report(self) -> MigrationReport:
        """What this conversion did to the remote page, as a classified report.

        Derived rather than stored: the diagnostics and the source hash are
        already the report's only inputs, so keeping a second copy in the
        dataclass would just create something that can disagree with them.
        """
        return MigrationReport.build(
            occurrences=occurrences_from_diagnostics(
                self.diagnostics,
                source_storage_sha256=self.source_storage_sha256,
            )
        )


@dataclass(frozen=True)
class CfxArtifact:
    schema_version: int
    xhtml: str
    candidate_storage_sha256: str
    presentation: PresentationArtifact
    diagnostics: tuple[ConversionDiagnostic, ...]
    document: Document
    push_safe: bool
    edited_operation_to_storage: dict[str, tuple[StorageLeafRef, ...]]
    candidate_storage_to_operation: dict[StorageLeafRef, str]
    edit_operations: tuple[EditOperation, ...]
    serializer_ancestor_node_ids: tuple[str, ...]
    semantic_nodes: tuple[SemanticNode, ...]
    source_map_entries: tuple[SourceMapEntry, ...]
    unmapped_storage_leaves: tuple[StorageLeafRef, ...]
    semantic_match_base_nodes: tuple[SemanticNode, ...] = ()
    attachments: tuple[str, ...] = ()
    preserved_regions: tuple[str, ...] = ()
    required_regions: tuple[str, ...] = ()
    unspliced_regions: tuple[str, ...] = ()
    protected_region_proofs: tuple[ProtectedRegionProof, ...] = ()
    preserved_subtrees: tuple[str, ...] = ()
    required_subtrees: tuple[str, ...] = ()
    unspliced_subtrees: tuple[str, ...] = ()
    preserved_subtree_proofs: tuple[RemoteSubtreeProof, ...] = ()
    protected_region_changes: tuple[ProtectedRegionChange, ...] = ()
    task_status_changes: tuple[TaskStatusChange, ...] = ()
    splice_proof_complete: bool = False
    ownership_proof_required: bool = False
    source_storage_sha256: str | None = None
    source_migration_report: MigrationReport | None = None
    ownership_proof: OwnershipResult | None = None
    ownership_proof_sha256: str | None = None
    ownership_final_change_count: int | None = None
    ownership_base_source_map_entries: tuple[SourceMapEntry, ...] | None = None
    ownership_base_semantic_nodes: tuple[SemanticNode, ...] | None = None
    ownership_baseline_source_map_entries: tuple[SourceMapEntry, ...] | None = None
    ownership_baseline_semantic_nodes: tuple[SemanticNode, ...] | None = None
    ownership_baseline_matches_source_storage: bool | None = None
    ownership_attachment_filename_map: tuple[tuple[str, str], ...] | None = None
    ownership_remote_subtrees: tuple[RemoteSubtreeArtifact, ...] | None = None
    ownership_presentation_authority: PresentationArtifact | None = None
    ownership_options: ConversionOptions | None = None
    ownership_options_sha256: str | None = None

    @property
    def migration_report(self) -> MigrationReport:
        """What publishing this candidate would do, anchored to fresh S0.

        Managed candidates carry the exact source-bound report assembled by
        the public conversion path. Bare Markdown conversions have no S0, so
        they retain the legacy candidate-bound diagnostic view.
        """
        if self.source_migration_report is not None:
            return self.source_migration_report
        return MigrationReport.build(
            occurrences=occurrences_from_diagnostics(
                self.diagnostics,
                source_storage_sha256=self.candidate_storage_sha256,
            )
        )


@dataclass(frozen=True)
class ManagedCfxArtifact(CfxArtifact):
    """A source-bound candidate whose proof requirement cannot be demoted by field replacement."""


def _validate_cfx_artifact_core(
    artifact: CfxArtifact,
    *,
    source_storage: str | None = None,
    macros: Any | None = None,
) -> None:
    """Verify artifact structure and a managed envelope against fresh S0."""

    if (artifact.preserved_regions or artifact.preserved_subtrees) and not artifact.splice_proof_complete:
        raise ConversionError("candidate artifact has preserved remote content without a complete splice proof")
    if len(set(artifact.required_regions)) != len(artifact.required_regions):
        raise ConversionError("candidate artifact has duplicate required protected-region IDs")
    if len(set(artifact.preserved_regions)) != len(artifact.preserved_regions):
        raise ConversionError("candidate artifact has duplicate preserved protected-region IDs")
    if artifact.required_regions != artifact.preserved_regions or artifact.unspliced_regions:
        raise ConversionError("candidate artifact has missing protected-region preservation")
    protected_proof_ids = tuple(region_proof.occurrence_id for region_proof in artifact.protected_region_proofs)
    if protected_proof_ids != artifact.preserved_regions:
        raise ConversionError("candidate artifact protected-region proofs do not match preserved regions")
    proof_root = _parse_fragment_to_element(artifact.xhtml)
    for region_proof in artifact.protected_region_proofs:
        element = _element_at_storage_path(proof_root, region_proof.candidate_node_path)
        if element is None:
            raise ConversionError("candidate artifact protected-region proof path is missing")
        raw = _serialize_element(element)
        fingerprint = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        if fingerprint != region_proof.remote_subtree_fingerprint:
            raise ConversionError("candidate artifact protected-region proof fingerprint does not match XHTML")
    if len(set(artifact.required_subtrees)) != len(artifact.required_subtrees):
        raise ConversionError("candidate artifact has duplicate required remote-subtree IDs")
    if len(set(artifact.preserved_subtrees)) != len(artifact.preserved_subtrees):
        raise ConversionError("candidate artifact has duplicate preserved remote-subtree IDs")
    if artifact.required_subtrees != artifact.preserved_subtrees or artifact.unspliced_subtrees:
        raise ConversionError("candidate artifact has missing remote-subtree preservation")
    if (
        tuple(subtree_proof.node_id for subtree_proof in artifact.preserved_subtree_proofs)
        != artifact.preserved_subtrees
    ):
        raise ConversionError("candidate artifact remote-subtree proof IDs do not match preserved subtrees")
    for subtree_proof in artifact.preserved_subtree_proofs:
        element = _element_at_storage_path(proof_root, subtree_proof.candidate_node_path)
        if element is None:
            raise ConversionError("candidate artifact remote-subtree proof path is missing")
        raw = _serialize_element(element)
        if hashlib.sha256(raw.encode()).hexdigest() != subtree_proof.remote_subtree_fingerprint:
            raise ConversionError("candidate artifact remote-subtree proof fingerprint does not match XHTML")
    expected_digest = hashlib.sha256(artifact.xhtml.encode()).hexdigest()
    if artifact.candidate_storage_sha256 != expected_digest:
        raise ConversionError("candidate artifact digest does not match its XHTML bytes")
    try:
        operation_to_storage, storage_to_operation = build_candidate_source_maps(artifact.source_map_entries)
    except Exception as error:
        raise ConversionError("candidate artifact source map is not uniquely invertible") from error
    if (
        operation_to_storage != artifact.edited_operation_to_storage
        or storage_to_operation != artifact.candidate_storage_to_operation
    ):
        raise ConversionError("candidate artifact source-map indexes do not match their entries")
    actual = Counter(extract_storage_source(artifact.xhtml).leaves)
    reported = Counter(leaf for entry in artifact.source_map_entries for leaf in entry.leaves)
    if actual != reported or artifact.unmapped_storage_leaves:
        if artifact.ownership_proof is not None and artifact.ownership_proof.fatal:
            raise OwnershipProofError(
                "publish preflight rejects a fatal ownership proof with incomplete source mapping",
                summary=_ownership_proof_summary(artifact.ownership_proof, artifact.diagnostics),
            )
        raise ConversionError("candidate artifact does not prove exactly one owner for every XHTML leaf")
    source_bound_fields = (
        artifact.source_storage_sha256,
        artifact.source_migration_report,
        artifact.ownership_proof,
        artifact.ownership_proof_sha256,
        artifact.ownership_final_change_count,
        artifact.ownership_base_source_map_entries,
        artifact.ownership_base_semantic_nodes,
        artifact.ownership_baseline_source_map_entries,
        artifact.ownership_baseline_semantic_nodes,
        artifact.ownership_baseline_matches_source_storage,
        artifact.ownership_attachment_filename_map,
        artifact.ownership_remote_subtrees,
        artifact.ownership_presentation_authority,
        artifact.ownership_options,
        artifact.ownership_options_sha256,
    )
    managed = isinstance(artifact, ManagedCfxArtifact)
    if artifact.ownership_proof_required != managed:
        raise ConversionError("candidate ownership marker does not match its managed artifact type")
    if not managed and any(item is not None for item in source_bound_fields):
        raise ConversionError("unmanaged candidate cannot carry a source-bound ownership proof envelope")
    if managed and source_storage is None:
        raise ConversionError("managed candidate validation requires fresh source storage")
    if managed and not all(item is not None for item in source_bound_fields):
        raise ConversionError("candidate source-bound ownership proof is required but incomplete")
    if any(item is not None for item in source_bound_fields) and not all(
        item is not None for item in source_bound_fields
    ):
        raise ConversionError("candidate source-bound ownership proof envelope is incomplete")
    ownership = artifact.ownership_proof
    if ownership is None:
        return
    assert artifact.source_storage_sha256 is not None
    assert artifact.source_migration_report is not None
    assert artifact.ownership_proof_sha256 is not None
    assert artifact.ownership_final_change_count is not None
    assert source_storage is not None
    fresh_source_sha256 = hashlib.sha256(source_storage.encode()).hexdigest()
    if fresh_source_sha256 != artifact.source_storage_sha256:
        raise ConversionError("candidate source storage is stale or does not match its ownership proof")
    if ownership.artifacts.source_storage_sha256 != f"sha256:{artifact.source_storage_sha256}":
        raise ConversionError("candidate ownership proof is not bound to its source storage")
    if ownership.artifacts.final_candidate_sha256 != f"sha256:{expected_digest}":
        raise ConversionError("candidate ownership proof is not bound to its XHTML bytes")
    assert artifact.ownership_base_source_map_entries is not None
    assert artifact.ownership_base_semantic_nodes is not None
    assert artifact.ownership_baseline_source_map_entries is not None
    assert artifact.ownership_baseline_semantic_nodes is not None
    assert artifact.ownership_baseline_matches_source_storage is not None
    assert artifact.ownership_attachment_filename_map is not None
    assert artifact.ownership_remote_subtrees is not None
    assert artifact.ownership_presentation_authority is not None
    assert artifact.ownership_options_sha256 is not None
    from cfxmark.ownership_proof import attribution_witness_sha256

    witness_sha256 = attribution_witness_sha256(
        base_source_map_entries=artifact.ownership_base_source_map_entries,
        base_semantic_nodes=artifact.ownership_base_semantic_nodes,
        baseline_source_map_entries=artifact.ownership_baseline_source_map_entries,
        baseline_semantic_nodes=artifact.ownership_baseline_semantic_nodes,
        baseline_matches_source_storage=artifact.ownership_baseline_matches_source_storage,
        attachment_filename_map=artifact.ownership_attachment_filename_map,
        remote_subtrees=artifact.ownership_remote_subtrees,
        presentation_authority=artifact.ownership_presentation_authority,
        options_sha256=artifact.ownership_options_sha256,
    )
    if ownership.artifacts.attribution_witness_sha256 != witness_sha256:
        raise ConversionError("candidate ownership proof is not bound to its attribution witness")
    try:
        independent_document, _presentation_messages, presentation_occurrences = apply_presentation_with_authority(
            artifact.document,
            artifact.ownership_presentation_authority,
        )
        if independent_document != artifact.document:
            raise ConversionError("candidate presentation cannot be independently reconstructed")
        (
            independent_semantic_nodes,
            independent_operation_to_storage,
            _independent_storage_to_operation,
            independent_edit_operations,
            independent_xhtml,
            _independent_attachments,
            _independent_warnings,
            independent_source_map_entries,
            _independent_unmapped,
        ) = candidate_maps(
            independent_document,
            base_nodes=artifact.ownership_base_semantic_nodes,
            base_source_map_entries=artifact.ownership_base_source_map_entries,
            registry=macros,
            attachment_filename_map=artifact.ownership_attachment_filename_map,
            presentation_occurrences=presentation_occurrences,
        )
    except SemanticMappingAmbiguous as error:
        reason_code = error.code if error.code in PUBLIC_CONVERSION_REASON_CODES else None
        raise ConversionError(f"{error.code}: {error}", reason_code=reason_code) from error
    except ConversionError:
        raise
    except Exception as error:
        raise ConversionError("candidate attribution cannot be independently reconstructed") from error
    if independent_edit_operations != artifact.edit_operations:
        if artifact.ownership_proof is not None and artifact.ownership_proof.fatal:
            raise OwnershipProofError(
                "publish preflight rejects an independently unreproducible fatal ownership proof",
                summary=_ownership_proof_summary(artifact.ownership_proof, artifact.diagnostics),
            )
        raise ConversionError("candidate edit operations do not match the independent semantic matcher")
    if independent_semantic_nodes != artifact.semantic_nodes:
        raise ConversionError("candidate semantic nodes do not match the independent semantic matcher")

    replayed_xhtml = independent_xhtml
    replayed_source_map_entries = independent_source_map_entries
    replayed_unmapped = _independent_unmapped
    replayed_preserved: tuple[str, ...] = ()
    replayed_required: tuple[str, ...] = ()
    replayed_unspliced: tuple[str, ...] = ()
    replayed_proofs: tuple[RemoteSubtreeProof, ...] = ()
    if artifact.ownership_remote_subtrees:
        from cfxmark.remote_splice import apply_remote_subtree_splice

        (
            replayed_xhtml,
            replayed_source_map_entries,
            replayed_unmapped,
            replayed_preserved,
            replayed_required,
            replayed_unspliced,
            replayed_proofs,
        ) = apply_remote_subtree_splice(
            independent_xhtml,
            splice_source=source_storage,
            remote_subtrees=artifact.ownership_remote_subtrees,
            base_nodes=artifact.ownership_base_semantic_nodes,
            candidate_nodes=independent_semantic_nodes,
            edit_operations=independent_edit_operations,
            source_map_entries=independent_source_map_entries,
        )
    if (
        replayed_xhtml != artifact.xhtml
        or replayed_source_map_entries != artifact.source_map_entries
        or replayed_unmapped != artifact.unmapped_storage_leaves
        or replayed_preserved != artifact.preserved_subtrees
        or replayed_required != artifact.required_subtrees
        or replayed_unspliced != artifact.unspliced_subtrees
        or replayed_proofs != artifact.preserved_subtree_proofs
    ):
        raise ConversionError("candidate source map does not match an independent remote-splice replay")
    independent_operation_to_storage, _replayed_storage_to_operation = build_candidate_source_maps(
        replayed_source_map_entries
    )

    protected_paths = {proof.node_id: proof.candidate_node_path for proof in artifact.preserved_subtree_proofs} | {
        proof.occurrence_id: proof.candidate_node_path for proof in artifact.protected_region_proofs
    }
    for entry in artifact.source_map_entries:
        if entry.source_kind != "protected-region":
            continue
        expected_path = protected_paths.get(entry.node_or_occurrence_id)
        if expected_path is None or entry.operation_id != f"preserve:{entry.node_or_occurrence_id}":
            raise ConversionError("candidate protected source-map owner has no exact preservation proof")
        if any(leaf.semantic_path[: len(expected_path)] != expected_path for leaf in entry.leaves):
            raise ConversionError("candidate protected source-map leaf is outside its proven subtree")
    actual_operation_ids = {operation_id(operation) for operation in independent_edit_operations}
    if not set(ownership.intended_operation_ids) <= actual_operation_ids:
        raise ConversionError("candidate ownership proof names a non-existent edit operation")
    report_occurrence_ids = {occurrence.occurrence_id for occurrence in artifact.source_migration_report.occurrences}
    if not set(ownership.migration_occurrence_ids) <= report_occurrence_ids:
        raise ConversionError("candidate ownership proof names a non-existent migration occurrence")
    if len(ownership.final_changes) != artifact.ownership_final_change_count:
        raise ConversionError("candidate ownership proof final change count does not match its artifact witness")
    final_change_keys = Counter(
        (change.identity, change.before_fingerprint, change.after_fingerprint) for change in ownership.final_changes
    )
    if any(count != 1 for count in final_change_keys.values()):
        raise ConversionError("candidate ownership proof has duplicate final storage changes")
    source_leaf_fingerprints = {snapshot.identity: snapshot.value_fingerprint for snapshot in ownership.source_leaves}
    if len(source_leaf_fingerprints) != len(ownership.source_leaves):
        raise ConversionError("candidate ownership proof has duplicate source leaf identities")
    fresh_source_leaf_fingerprints = {
        storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in extract_storage_source(source_storage).leaves
    }
    if source_leaf_fingerprints != fresh_source_leaf_fingerprints:
        raise ConversionError("candidate ownership proof source snapshot does not match fresh source storage")
    final_leaf_fingerprints = {
        storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in extract_storage_source(artifact.xhtml).leaves
    }
    derived_final_change_keys = Counter(
        (
            identity,
            source_leaf_fingerprints.get(identity),
            final_leaf_fingerprints.get(identity),
        )
        for identity in source_leaf_fingerprints.keys() | final_leaf_fingerprints.keys()
        if source_leaf_fingerprints.get(identity) != final_leaf_fingerprints.get(identity)
    )
    if final_change_keys != derived_final_change_keys:
        raise ConversionError("candidate ownership proof does not classify the complete final storage diff")
    classified_change_keys = Counter(
        (change.identity, change.before_fingerprint, change.after_fingerprint)
        for changes in (
            ownership.intended,
            ownership.migration,
            ownership.unclassified,
            ownership.multiple_owners,
            ownership.overlapping,
        )
        for change in changes
    )
    if any(classified_change_keys[key] != 1 for key in final_change_keys):
        raise ConversionError("candidate ownership proof does not classify the complete final storage diff")
    if any(
        (change.identity, change.before_fingerprint, change.after_fingerprint) not in final_change_keys
        for change in (*ownership.intended, *ownership.migration)
    ):
        raise ConversionError("candidate ownership proof assigns an owner outside the final storage diff")
    canonical_move_ids = canonical_move_operation_ids(
        artifact.ownership_baseline_semantic_nodes,
        independent_semantic_nodes,
        independent_edit_operations,
    )
    independent_operations = {operation_id(operation): operation for operation in independent_edit_operations}
    direct_after_owners: defaultdict[tuple[tuple[str, ...], str, str | None, int], set[str]] = defaultdict(set)
    for owner, leaves in independent_operation_to_storage.items():
        operation = independent_operations.get(owner)
        if operation is None or (operation.kind == "move" and owner not in canonical_move_ids):
            continue
        for leaf in leaves:
            direct_after_owners[storage_leaf_identity(leaf)].add(owner)
    delete_before_owners: defaultdict[tuple[tuple[str, ...], str, str | None, int], set[str]] = defaultdict(set)
    relocation_before_owners: defaultdict[tuple[tuple[str, ...], str, str | None, int], set[str]] = defaultdict(set)
    independent_relocated_scope: dict[str, frozenset[tuple[tuple[str, ...], str, str | None, int]]] = {}
    for owner, operation in independent_operations.items():
        if operation.kind == "move" and owner not in canonical_move_ids:
            continue
        independent_relocated_scope[owner] = frozenset(
            storage_leaf_identity(leaf) for leaf in operation.relocated_leaf_tombstones
        )
        for leaf in operation.deleted_leaf_tombstones:
            delete_before_owners[storage_leaf_identity(leaf)].add(owner)
        for leaf in operation.relocated_leaf_tombstones:
            relocation_before_owners[storage_leaf_identity(leaf)].add(owner)

    from cfxmark.ownership_proof import _most_specific_relocation_owners

    def independently_bound_owner(change: Any) -> str | tuple[str, ...] | None:
        direct = direct_after_owners.get(change.identity, set()) if change.after_fingerprint is not None else set()
        # Mirror _user_owners: when several ancestor relocations claim the same
        # leaf as fallback, keep only the most specific one. Generation and this
        # independent re-derivation must apply the identical rule, or a
        # legitimately owned leaf reads as multiply owned here and is rejected.
        relocation = set(relocation_before_owners.get(change.identity, set()))
        if len(relocation) > 1:
            relocation = _most_specific_relocation_owners(relocation, independent_relocated_scope)
        owners = delete_before_owners.get(change.identity, set()) or direct or relocation
        ordered = tuple(sorted(owners))
        if not ordered:
            return None
        return ordered[0] if len(ordered) == 1 else ordered

    expected_user_owners = {change.identity: independently_bound_owner(change) for change in ownership.final_changes}
    if any(expected_user_owners.get(change.identity) != change.owner for change in ownership.intended):
        raise ConversionError("candidate ownership proof does not bind its leaf to the named edit operation")
    if any(expected_user_owners.get(change.identity) is not None for change in ownership.migration):
        raise ConversionError("candidate ownership proof hides a user owner behind a migration occurrence")
    if ownership.sha256() != artifact.ownership_proof_sha256:
        raise ConversionError("candidate ownership proof does not match its immutable envelope fingerprint")
    if artifact.push_safe and ownership.fatal:
        raise ConversionError("candidate is marked push-safe despite a fatal ownership proof")


def validate_cfx_artifact(
    artifact: CfxArtifact,
    *,
    source_storage: str | None = None,
    macros: Any | None = None,
) -> None:
    """Validate a conversion artifact at its strongest available boundary.

    Managed candidates carry their exact conversion options, so this public
    entry point performs the same fresh-S0/C0 replay as
    :func:`validate_managed_cfx_artifact` instead of exposing a weaker path.
    """

    if isinstance(artifact, ManagedCfxArtifact):
        if source_storage is None:
            _validate_cfx_artifact_core(artifact, source_storage=None, macros=macros)
            return
        if artifact.ownership_options is None:
            raise ConversionError("candidate source-bound ownership proof envelope is incomplete")
        validate_managed_cfx_artifact(
            artifact,
            source_storage=source_storage,
            options=artifact.ownership_options,
            macros=macros,
        )
        return
    _validate_cfx_artifact_core(artifact, source_storage=source_storage, macros=macros)


_MAX_SUMMARY_IDENTITIES = 10


def _ownership_proof_summary(
    proof: OwnershipResult,
    diagnostics: tuple[ConversionDiagnostic, ...],
) -> OwnershipProofSummary:
    """Project a fatal ownership proof to a value-free public summary.

    Counts and canonical leaf identities come from the proof's fatal groups;
    diagnostic ``code``/``resolution`` come from the artifact's blocking
    diagnostics. No fingerprint, leaf value, or raw source crosses this boundary.
    """

    ordered_identities: list[LeafIdentity] = []
    seen: set[LeafIdentity] = set()
    for change in (*proof.unclassified, *proof.multiple_owners, *proof.overlapping):
        if change.identity not in seen:
            seen.add(change.identity)
            ordered_identities.append(change.identity)
    top_identities = tuple(sorted(ordered_identities)[:_MAX_SUMMARY_IDENTITIES])

    diagnostic_summaries: list[DiagnosticSummary] = []
    seen_diagnostics: set[tuple[str, str, int | None]] = set()
    for diagnostic in diagnostics:
        if not diagnostic.blocking:
            continue
        resolution = diagnostic.resolutions[0].id if diagnostic.resolutions else ""
        table_ordinal = diagnostic.location.table_index
        # Dedup includes the position: two tables failing the same way are two
        # things to look at, and collapsing them hides the second one.
        key = (diagnostic.code, resolution, table_ordinal)
        if key not in seen_diagnostics:
            seen_diagnostics.add(key)
            diagnostic_summaries.append(
                DiagnosticSummary(code=diagnostic.code, resolution=resolution, table_ordinal=table_ordinal)
            )

    return OwnershipProofSummary(
        fatal_class=proof.blocking_code or "fatal",
        unclassified_count=len(proof.unclassified),
        multiple_owners_count=len(proof.multiple_owners),
        overlap_count=len(proof.overlapping),
        top_identities=top_identities,
        diagnostics=tuple(diagnostic_summaries),
    )


def validate_managed_cfx_artifact(
    artifact: CfxArtifact,
    *,
    source_storage: str,
    options: ConversionOptions,
    macros: Any | None = None,
) -> None:
    """Validate a managed candidate against fresh S0 and a replayed C0.

    Candidate attribution is replayed from ``artifact.document`` by
    :func:`validate_cfx_artifact`.  The baseline witness needs the symmetric
    check: reconstruct M0 and C0 from fresh storage plus the caller's exact
    conversion profile instead of trusting serialized witness tuples.
    """

    if not isinstance(artifact, ManagedCfxArtifact):
        raise ConversionError("publish preflight requires a managed source-bound candidate")
    _validate_cfx_artifact_core(artifact, source_storage=source_storage, macros=macros)
    assert artifact.ownership_proof is not None
    if artifact.ownership_proof.fatal:
        raise OwnershipProofError(
            "publish preflight rejects a fatal ownership proof",
            summary=_ownership_proof_summary(artifact.ownership_proof, artifact.diagnostics),
        )

    from cfxmark.api import (
        _conversion_options_sha256,
        _presentation_without_protected_tables,
        _to_cfx_artifact_core,
        to_cfx_artifact,
        to_md_artifact,
    )

    assert artifact.ownership_options_sha256 is not None
    assert artifact.ownership_options is not None
    if artifact.ownership_options != options:
        raise ConversionError("publish preflight options do not match the source-bound candidate")
    if artifact.ownership_options_sha256 != _conversion_options_sha256(options):
        raise ConversionError("publish preflight options do not match the source-bound candidate")

    fresh_base = to_md_artifact(source_storage, options=options, macros=macros)
    migration_base = replace(fresh_base, protected_regions=(), remote_subtrees=())
    assert artifact.ownership_presentation_authority is not None
    # Two derivations are valid, both deterministic functions of fresh storage:
    # the splice path computes the authority against the base with its protected
    # regions intact, while the managed full-migration path strips protected
    # regions and remote subtrees before building the candidate.
    expected_presentation_authority = _presentation_without_protected_tables(fresh_base.presentation, fresh_base)
    stripped_presentation_authority = _presentation_without_protected_tables(fresh_base.presentation, migration_base)
    if artifact.ownership_presentation_authority not in (
        expected_presentation_authority,
        stripped_presentation_authority,
    ):
        raise ConversionError("publish preflight presentation authority does not match fresh source storage")
    assert artifact.ownership_baseline_source_map_entries is not None
    assert artifact.ownership_baseline_semantic_nodes is not None
    assert artifact.ownership_baseline_matches_source_storage is not None
    matching_bases: list[MarkdownArtifact] = []
    bases = (fresh_base,) if migration_base == fresh_base else (fresh_base, migration_base)
    for base in bases:
        replayed = _to_cfx_artifact_core(
            base.markdown,
            presentation=base.presentation,
            base_artifact=base,
            splice_source=source_storage,
            options=options,
            macros=macros,
        )
        baseline_sha256 = "sha256:" + hashlib.sha256(replayed.xhtml.encode()).hexdigest()
        if (
            replayed.source_map_entries == artifact.ownership_baseline_source_map_entries
            and replayed.semantic_nodes == artifact.ownership_baseline_semantic_nodes
            and (replayed.xhtml == source_storage) == artifact.ownership_baseline_matches_source_storage
            and artifact.ownership_proof.artifacts.baseline_candidate_sha256 == baseline_sha256
        ):
            matching_bases.append(base)
    if not matching_bases:
        raise ConversionError("publish preflight baseline witness does not match fresh C0 replay")

    # Re-derive the source-side report entries and migration ownership from
    # fresh S0.  The complete report can additionally contain diagnostics from
    # M1/C1 (for example, a user-authored unsupported Markdown construct), so
    # it need not equal a no-edit C0 report.  Every source-side occurrence must
    # nevertheless be present byte-for-byte; otherwise a forged report could
    # hide a disclosed source loss while preserving a self-consistent envelope.
    assert artifact.source_migration_report is not None
    expected_migration_signature = _migration_owner_signature(artifact.ownership_proof.migration)
    migration_witness_matches = False
    matched_fresh_candidate: CfxArtifact | None = None
    for base in matching_bases:
        fresh_candidate = to_cfx_artifact(
            base.markdown,
            presentation=base.presentation,
            base_artifact=base,
            splice_source=source_storage,
            options=options,
            macros=macros,
        )
        if fresh_candidate.source_migration_report is None or fresh_candidate.ownership_proof is None:
            continue
        replayed_occurrences = {
            occurrence.occurrence_id: occurrence for occurrence in fresh_candidate.source_migration_report.occurrences
        }
        reported_occurrences = {
            occurrence.occurrence_id: occurrence for occurrence in artifact.source_migration_report.occurrences
        }
        if len(reported_occurrences) != len(artifact.source_migration_report.occurrences):
            continue
        if any(
            reported_occurrences.get(occurrence_id) != occurrence
            for occurrence_id, occurrence in replayed_occurrences.items()
        ):
            continue
        # Subset, not equality: a user may edit away a migration-introduced leaf
        # (delete the empty-paragraph <br> the round-trip produced), so a
        # migration change realised in a no-edit C0 need not survive into the
        # edited C1. Every migration ownership the artifact DOES claim must still
        # reproduce from fresh S0, which forbids forging migration ownership; a
        # dropped one cannot hide a loss because the final diff must still be
        # covered by migration or a real user operation or it is unclassified.
        if expected_migration_signature <= _migration_owner_signature(fresh_candidate.ownership_proof.migration):
            migration_witness_matches = True
            matched_fresh_candidate = fresh_candidate
            break
    if not migration_witness_matches:
        raise ConversionError(
            "publish preflight source migration report or ownership does not match fresh source storage"
        )

    # Independently re-derive every supersession witness from fresh S0 and reject
    # any user attribution that hides a migration owner without one.
    from cfxmark.ownership_proof import _verify_supersession_witnesses

    assert matched_fresh_candidate is not None
    _verify_supersession_witnesses(
        artifact=artifact,
        fresh_candidate=matched_fresh_candidate,
        source_storage=source_storage,
    )


def _migration_owner_signature(
    changes: Any,
) -> frozenset[tuple[Any, str | None, str | None, Any]]:
    """Order-independent identity of a migration classification's leaf owners."""

    return frozenset(
        (change.identity, change.before_fingerprint, change.after_fingerprint, change.owner) for change in changes
    )


def _element_at_storage_path(root: ET._Element, path: tuple[str, ...]) -> ET._Element | None:
    current = root
    for component in path:
        name, separator, ordinal_text = component.rpartition("[")
        if not separator or not ordinal_text.endswith("]"):
            return None
        try:
            ordinal = int(ordinal_text[:-1])
        except ValueError:
            return None
        matches = [child for child in current if storage_node_name(child) == name]
        if ordinal >= len(matches):
            return None
        current = matches[ordinal]
    return current


def _semantic_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Node):
        return {
            "type": type(value).__name__,
            "fields": {
                item.name: _semantic_value(getattr(value, item.name))
                for item in fields(value)
                if _is_semantic_field(value, item.name)
            },
        }
    if is_dataclass(value):
        return {item.name: _semantic_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_semantic_value(item) for item in value]
    return value


def _subtree_fingerprint(node: Node) -> str:
    payload = json.dumps(_semantic_value(node), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return fingerprint_semantic_value(payload)


def _direct_leaves(node: Node, path: tuple[str, ...]) -> tuple[StorageLeafRef, ...]:
    leaves: list[StorageLeafRef] = []
    ordinal = 0
    for item in fields(node):
        if not _is_semantic_field(node, item.name):
            continue
        value = getattr(node, item.name)
        if isinstance(value, Node) or (isinstance(value, tuple) and any(isinstance(member, Node) for member in value)):
            continue
        if value is None or value == ():
            continue
        serialized = json.dumps(_semantic_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        field_kind: Literal["text", "tail", "attribute", "element", "topology"] = "attribute"
        if item.name in {"content", "code"}:
            field_kind = "text"
        elif item.name in {"colspan", "rowspan", "kind", "alignments", "header", "body"}:
            field_kind = "topology"
        leaves.append(
            StorageLeafRef(
                semantic_path=path + (item.name,),
                field=field_kind,
                attribute_name=item.name if field_kind == "attribute" else None,
                occurrence_ordinal=ordinal,
                value_fingerprint=fingerprint_semantic_value(serialized),
            )
        )
        ordinal += 1
    if not leaves:
        leaves.append(
            StorageLeafRef(
                semantic_path=path,
                field="element",
                attribute_name=None,
                occurrence_ordinal=0,
                value_fingerprint=fingerprint_semantic_value(type(node).__name__),
            )
        )
    return tuple(leaves)


def _local_fingerprint(node: Node) -> str:
    values = {
        item.name: _semantic_value(value)
        for item in fields(node)
        if _is_semantic_field(node, item.name)
        if not isinstance((value := getattr(node, item.name)), Node)
        and not (isinstance(value, tuple) and any(isinstance(member, Node) for member in value))
    }
    return fingerprint_semantic_value(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


# Node types whose whole subtree carries no remote-backed identity: no
# macro-id, attachment, opaque payload, presentation (cell background / color /
# alignment span), link target, or unsupported-occurrence id. This is an
# allowlist on purpose -- any new or unrecognised node type is treated as
# payload-bearing, so a tied alignment over it stays fail-closed rather than
# silently relaxing. Types with conditional identity (CodeBlock, Image,
# DirectiveMacro, Link, Table*, OpaqueBlock, ...) are deliberately absent.
_OWNERSHIP_NEUTRAL_NODE_TYPES = frozenset(
    {
        "Text",
        "Paragraph",
        "Heading",
        "List",
        "ListItem",
        "InlineCode",
        "Emphasis",
        "Strong",
        "Strikethrough",
        "Subscript",
        "Superscript",
        "Underline",
        "SoftBreak",
        "HardBreak",
        "HorizontalRule",
        "BlockQuote",
    }
)


def _subtree_payload_free(node: Node) -> bool:
    """True only if the node and every descendant is ownership-neutral.

    A delete tombstones the whole subtree and an insert re-emits it, so the
    guarantee has to hold for the entire subtree, not just the root.
    """

    if type(node).__name__ not in _OWNERSHIP_NEUTRAL_NODE_TYPES:
        return False
    for item in fields(node):
        value = getattr(node, item.name)
        if isinstance(value, Node):
            if not _subtree_payload_free(value):
                return False
        elif isinstance(value, tuple):
            for member in value:
                if isinstance(member, Node) and not _subtree_payload_free(member):
                    return False
    return True


def _semantic_snapshot_with_objects(
    document: Document,
    *,
    base: bool,
) -> tuple[tuple[SemanticNode, ...], tuple[SourceMapEntry, ...], dict[int, str], dict[int, Node]]:
    nodes: list[SemanticNode] = []
    entries: list[SourceMapEntry] = []
    object_to_node: dict[int, str] = {}
    object_values: dict[int, Node] = {}

    def visit(node: Node, parent_id: str | None, ordinal: int, path: tuple[str, ...]) -> None:
        fingerprint = _subtree_fingerprint(node)
        node_type = type(node).__name__
        node_id = (
            stable_node_id(parent_id, node_type, ordinal, fingerprint)
            if base
            else "edited-"
            + hashlib.sha256(
                json.dumps([path, node_type, ordinal, fingerprint], ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
        )
        current_path = path + (f"{node_type}[{ordinal}]",)
        leaves = _direct_leaves(node, current_path)
        topology = ""
        if node_type in {"Table", "TableRow", "TableCell", "List", "ListItem"}:
            topology = fingerprint_semantic_value(
                json.dumps(
                    {
                        item.name: _semantic_value(getattr(node, item.name))
                        for item in fields(node)
                        if item.name in {"kind", "colspan", "rowspan", "alignments", "ordered", "start"}
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        semantic_node = SemanticNode(
            node_id=node_id,
            node_type=node_type,
            parent_id=parent_id,
            ordinal=ordinal,
            subtree_fingerprint=fingerprint,
            local_fingerprint=_local_fingerprint(node),
            topology_fingerprint=topology,
            leaves=leaves,
            payload_free=_subtree_payload_free(node),
        )
        nodes.append(semantic_node)
        object_to_node[id(node)] = node_id
        object_values[id(node)] = node
        entries.append(SourceMapEntry(node_id, "ast-node", node_id, leaves))
        child_ordinal = 0
        for item in fields(node):
            value = getattr(node, item.name)
            if isinstance(value, Node):
                visit(value, node_id, child_ordinal, current_path + (item.name,))
                child_ordinal += 1
            elif isinstance(value, tuple):
                for member in value:
                    if isinstance(member, Node):
                        visit(member, node_id, child_ordinal, current_path + (item.name,))
                        child_ordinal += 1

    visit(document, None, 0, ())
    return tuple(nodes), tuple(entries), object_to_node, object_values


def semantic_snapshot(document: Document, *, base: bool) -> tuple[tuple[SemanticNode, ...], tuple[SourceMapEntry, ...]]:
    nodes, entries, _object_to_node, _object_values = _semantic_snapshot_with_objects(document, base=base)
    return nodes, entries


def _macro_identity_witnessed(node: Node) -> TypeGuard[CodeBlock | DirectiveMacro | InlineDirectiveMacro]:
    """Whether a node carries remote identity the Markdown projection drops.

    A node needs a witness whenever it carries a ``macro-id`` or a
    ``schema-version`` that its fresh Markdown re-render would not reproduce.
    The fresh default differs by node type: a code block renders *without*
    ``ac:schema-version`` (default ``None``), while a directive macro renders
    with ``ac:schema-version="1"`` (see
    :func:`cfxmark.xml_ns.build_structured_macro`). So a code block is witnessed
    when its schema-version is present at all, and a directive when its
    schema-version differs from ``"1"`` -- including the absent (``None``) case,
    which the re-parsed edit would otherwise regenerate as ``"1"`` and reject.
    """

    if isinstance(node, CodeBlock):
        return node.macro_id is not None or node.macro_schema_version is not None
    if isinstance(node, (DirectiveMacro, InlineDirectiveMacro)):
        return node.macro_id is not None or node.macro_schema_version != "1"
    return False


def macro_identities(document: Document) -> tuple[MacroIdentity, ...]:
    """Extract remote macro identities keyed by their stable semantic node.

    Covers code blocks and directive macros (block ``expand``/``info``/... and
    inline ``jira``).  ``ac:macro-id`` and ``ac:schema-version`` never appear in
    the Markdown projection, so they are recorded here and reapplied to the
    edited document by :func:`apply_macro_identities`.
    """

    _nodes, _entries, object_to_node, object_values = _semantic_snapshot_with_objects(document, base=True)
    return tuple(
        MacroIdentity(
            node_id=object_to_node[object_id],
            macro_id=node.macro_id,
            schema_version=node.macro_schema_version,
        )
        for object_id, node in object_values.items()
        if _macro_identity_witnessed(node)
    )


def apply_macro_identities(
    document: Document,
    *,
    identities: tuple[MacroIdentity, ...],
    base_nodes: tuple[SemanticNode, ...],
) -> Document:
    """Reapply unique remote macro identities after Markdown parsing.

    Only nodes that ``match_edit_operations`` maps to exactly one base node get
    their identity back; copies and genuinely ambiguous correspondences are left
    to regenerate as new macros, so an ID is never duplicated or misattributed.
    """

    if not identities:
        return document
    edited_nodes, _entries, object_to_edited, object_values = _semantic_snapshot_with_objects(document, base=False)
    correspondence = match_edit_operations(base_nodes, edited_nodes)
    identity_by_node = {item.node_id: item for item in identities}
    replacements: dict[int, Node] = {}
    for object_id, node in object_values.items():
        if not isinstance(node, (CodeBlock, DirectiveMacro, InlineDirectiveMacro)):
            continue
        edited_id = object_to_edited[object_id]
        stable_id = correspondence.edited_source_to_stable.get(edited_id)
        identity = identity_by_node.get(stable_id or "")
        if identity is not None:
            replacements[object_id] = replace(
                node,
                macro_id=identity.macro_id,
                macro_schema_version=identity.schema_version,
            )

    def rewrite(current: Any) -> Any:
        # A node can be both an identity replacement and a container of nested
        # identity macros (e.g. a code block or inline jira inside an expand
        # body). Adopt the replacement's own identity, then keep recursing into
        # its children -- ``replace`` shares the unchanged child objects, so a
        # nested replacement is still found by ``id`` -- rather than returning
        # the replacement early and stranding its descendants at default identity.
        base = replacements.get(id(current), current)
        if not isinstance(base, Node):
            return base
        changes: dict[str, Any] = {}
        for item in fields(base):
            child = getattr(base, item.name)
            if isinstance(child, Node):
                updated = rewrite(child)
            elif isinstance(child, tuple):
                updated = tuple(rewrite(member) if isinstance(member, Node) else member for member in child)
            else:
                continue
            if updated != child:
                changes[item.name] = updated
        return replace(base, **changes) if changes else base

    return cast(Document, rewrite(document))


def _presentation_owners(
    object_to_stable: dict[int, str],
    object_values: dict[int, Node],
    document: Document,
    authority_occurrences: dict[int, str] | None = None,
    operation_owners: dict[int, tuple[str, str]] | None = None,
    actual_operation_ids: set[str] | None = None,
) -> dict[int, tuple[str, str]]:
    owners: dict[int, tuple[str, str]] = {}
    occurrences = (
        authority_occurrences if authority_occurrences is not None else presentation_cell_occurrences(document)
    )
    for object_id, node in object_values.items():
        if not isinstance(node, TableCell) or node.background is None:
            continue
        stable_id = object_to_stable[object_id]
        payload = json.dumps(
            ["cfxmark-presentation-owner-v1", stable_id, "table-cell-background"],
            separators=(",", ":"),
        ).encode()
        operation_owner = (operation_owners or {}).get(object_id)
        owner = (
            operation_owner[0]
            if operation_owner is not None and operation_owner[0] in (actual_operation_ids or set())
            else f"presentation-{hashlib.sha256(payload).hexdigest()}"
        )
        owners[object_id] = (owner, occurrences[object_id])
    return owners


def base_maps(
    document: Document,
    source_xhtml: str,
    *,
    registry: Any | None = None,
) -> tuple[
    tuple[SemanticNode, ...],
    dict[StorageLeafRef, str],
    dict[str, tuple[StorageLeafRef, ...]],
    tuple[SourceMapEntry, ...],
    tuple[StorageLeafRef, ...],
]:
    nodes, _ast_entries, object_to_node, object_values = _semantic_snapshot_with_objects(document, base=True)
    _xhtml, _attachments, _warnings, rendered = render_cfx_with_source_map(
        document,
        registry=registry,
        source_owners={object_id: (node_id, node_id) for object_id, node_id in object_to_node.items()},
        presentation_owners=_presentation_owners(object_to_node, object_values, document),
    )
    canonical_owner = {
        storage_leaf_identity(leaf): entry.operation_id for entry in rendered.entries for leaf in entry.leaves
    }
    canonical_metadata = {
        entry.operation_id: (entry.source_kind, entry.node_or_occurrence_id) for entry in rendered.entries
    }
    raw = extract_storage_source(source_xhtml)
    grouped: dict[str, list[StorageLeafRef]] = {}
    rendered_unowned = {storage_leaf_identity(leaf) for leaf in (*rendered.unmapped, *rendered.ambiguous)}
    unmapped: list[StorageLeafRef] = []
    for leaf in raw.leaves:
        identity = storage_leaf_identity(leaf)
        if identity in rendered_unowned:
            unmapped.append(leaf)
            continue
        owner = canonical_owner.get(identity)
        if owner is None:
            unmapped.append(leaf)
            continue
        grouped.setdefault(owner, []).append(leaf)
    entries = tuple(
        SourceMapEntry(owner, canonical_metadata[owner][0], canonical_metadata[owner][1], tuple(leaves))
        for owner, leaves in sorted(grouped.items())
    )
    storage_to_node, node_to_storage = build_base_source_maps(entries)
    nodes = tuple(replace(node, leaves=node_to_storage.get(node.node_id, ())) for node in nodes)
    return nodes, storage_to_node, node_to_storage, entries, tuple(unmapped)


def candidate_maps(
    document: Document,
    *,
    base_nodes: tuple[SemanticNode, ...] | None = None,
    base_source_map_entries: tuple[SourceMapEntry, ...] | None = None,
    registry: Any | None = None,
    attachment_filename_map: tuple[tuple[str, str], ...] = (),
    presentation_occurrences: dict[int, str] | None = None,
) -> tuple[
    tuple[SemanticNode, ...],
    dict[str, tuple[StorageLeafRef, ...]],
    dict[StorageLeafRef, str],
    tuple[EditOperation, ...],
    str,
    list[str],
    list[str],
    tuple[SourceMapEntry, ...],
    tuple[StorageLeafRef, ...],
]:
    nodes, _entries, object_to_edited, object_values = _semantic_snapshot_with_objects(document, base=False)
    if base_nodes is not None and base_source_map_entries is not None:
        _storage_to_node, node_to_storage = build_base_source_maps(base_source_map_entries)
        base_nodes = tuple(replace(node, leaves=node_to_storage.get(node.node_id, ())) for node in base_nodes)
    if base_nodes is None:
        edited_to_stable = {node.node_id: node.node_id for node in nodes}
        delta: tuple[EditOperation, ...] = ()
    else:
        result = match_edit_operations(base_nodes, nodes)
        edited_to_stable = dict(result.edited_source_to_stable)
        delta = result.operations
        if base_source_map_entries is not None:
            source_leaves = tuple(leaf for entry in base_source_map_entries for leaf in entry.leaves)
            expanded_delta: list[EditOperation] = []
            for operation in delta:
                relocated = operation.relocated_leaf_tombstones
                if relocated:
                    shortest = tuple(
                        leaf.semantic_path
                        for leaf in relocated
                        if not any(
                            other.semantic_path != leaf.semantic_path
                            and leaf.semantic_path[: len(other.semantic_path)] == other.semantic_path
                            for other in relocated
                        )
                    )
                    relocated = tuple(
                        sorted(
                            {
                                *relocated,
                                *(
                                    leaf
                                    for leaf in source_leaves
                                    if any(leaf.semantic_path[: len(prefix)] == prefix for prefix in shortest)
                                ),
                            },
                            key=lambda leaf: (
                                leaf.semantic_path,
                                leaf.field,
                                leaf.attribute_name or "",
                                leaf.occurrence_ordinal,
                                leaf.value_fingerprint,
                            ),
                        )
                    )
                expanded_delta.append(replace(operation, relocated_leaf_tombstones=relocated))
            delta = tuple(expanded_delta)

    stable_nodes = tuple(
        replace(
            node,
            node_id=edited_to_stable[node.node_id],
            parent_id=edited_to_stable[node.parent_id] if node.parent_id is not None else None,
        )
        for node in nodes
    )
    base_by_stable = {node.node_id: node for node in base_nodes or ()}
    stable_by_id = {node.node_id: node for node in stable_nodes}
    canonical_move_ids = (
        canonical_move_operation_ids(base_nodes, stable_nodes, delta) if base_nodes is not None else set()
    )
    canonical_moves_by_parent: defaultdict[str, list[EditOperation]] = defaultdict(list)
    for operation in delta:
        if operation_id(operation) not in canonical_move_ids:
            continue
        for parent_id in {operation.old_parent_id, operation.new_parent_id} - {None}:
            assert parent_id is not None
            canonical_moves_by_parent[parent_id].append(operation)
    base_children: defaultdict[str | None, list[str]] = defaultdict(list)
    for node in base_nodes or ():
        base_children[node.parent_id].append(node.node_id)
    stable_children: defaultdict[str | None, list[str]] = defaultdict(list)
    for node in stable_nodes:
        stable_children[node.parent_id].append(node.node_id)

    delta_by_stable = {item.edited_node_id: item for item in delta if item.edited_node_id is not None}
    nodes_by_edited_id = {node.node_id: node for node in nodes}
    object_to_operation: dict[int, tuple[str, str]] = {}
    object_to_stable: dict[int, str] = {}
    edited_to_object = {edited_id: object_id for object_id, edited_id in object_to_edited.items()}
    for node in nodes:
        stable_id = edited_to_stable[node.node_id]
        owner = delta_by_stable.get(stable_id)
        if owner is not None and owner.kind == "move" and operation_id(owner) not in canonical_move_ids:
            canonical_siblings = {
                sibling
                for parent_id in {owner.old_parent_id, owner.new_parent_id} - {None}
                for sibling in canonical_moves_by_parent.get(cast(str, parent_id), ())
            }
            if len(canonical_siblings) == 1:
                owner = canonical_siblings.pop()
        ancestor_edited_id = node.parent_id
        while owner is None and ancestor_edited_id is not None:
            ancestor_stable_id = edited_to_stable[ancestor_edited_id]
            ancestor_owner = delta_by_stable.get(ancestor_stable_id)
            if (
                ancestor_owner is not None
                and ancestor_owner.kind == "move"
                and operation_id(ancestor_owner) not in canonical_move_ids
            ):
                canonical_siblings = {
                    sibling
                    for parent_id in {ancestor_owner.old_parent_id, ancestor_owner.new_parent_id} - {None}
                    for sibling in canonical_moves_by_parent.get(cast(str, parent_id), ())
                }
                if len(canonical_siblings) == 1:
                    ancestor_owner = canonical_siblings.pop()
            if ancestor_owner is not None and (ancestor_owner.kind in {"insert", "move"}):
                owner = ancestor_owner
                break
            ancestor_edited_id = nodes_by_edited_id[ancestor_edited_id].parent_id
        shifted_edited_id: str | None = node.node_id
        while owner is None and shifted_edited_id is not None:
            shifted_stable_id = edited_to_stable[shifted_edited_id]
            before_node = base_by_stable.get(shifted_stable_id)
            after_node = stable_by_id[shifted_stable_id]
            if before_node is not None and (
                before_node.parent_id != after_node.parent_id or before_node.ordinal != after_node.ordinal
            ):
                topology_owners = {
                    delta_by_stable[parent_id]
                    for parent_id in {before_node.parent_id, after_node.parent_id} - {None}
                    if parent_id in delta_by_stable and delta_by_stable[parent_id].topology_only
                }
                if len(topology_owners) == 1:
                    owner = topology_owners.pop()
                    break
            shifted_edited_id = nodes_by_edited_id[shifted_edited_id].parent_id
        if owner is None and base_children.get(stable_id) != stable_children.get(stable_id):
            parent_moves = canonical_moves_by_parent.get(stable_id, ())
            if len(parent_moves) == 1:
                owner = parent_moves[0]
        if owner is None:
            stable_parent = edited_to_stable.get(node.parent_id) if node.parent_id is not None else None
            owner = EditOperation(
                "update",
                stable_id,
                stable_id,
                stable_parent,
                stable_parent,
                node.ordinal,
                node.ordinal,
            )
        owner_id = operation_id(owner)
        object_id = edited_to_object[node.node_id]
        object_to_operation[object_id] = (owner_id, stable_id)
        object_to_stable[object_id] = stable_id
    xhtml, attachments, warnings, rendered = render_cfx_with_source_map(
        document,
        registry=registry,
        attachment_filename_map=attachment_filename_map,
        source_owners=object_to_operation,
        presentation_owners=_presentation_owners(
            object_to_stable,
            object_values,
            document,
            presentation_occurrences,
            object_to_operation,
            {operation_id(operation) for operation in delta},
        ),
    )
    operation_to_storage, storage_to_operation = build_candidate_source_maps(rendered.entries)
    actual = extract_storage_source(xhtml)
    reported_leaves = tuple(leaf for entry in rendered.entries for leaf in entry.leaves)
    actual_counts = Counter(actual.leaves)
    reported_counts = Counter(reported_leaves)
    integrity_mismatches = tuple((actual_counts - reported_counts).elements()) + tuple(
        (reported_counts - actual_counts).elements()
    )
    unmapped = (*rendered.unmapped, *rendered.ambiguous, *integrity_mismatches)
    return (
        stable_nodes,
        operation_to_storage,
        storage_to_operation,
        delta,
        xhtml,
        attachments,
        warnings,
        rendered.entries,
        unmapped,
    )


_STYLE_COLOR_PROP_RE = re.compile(r"^\s*(?P<prop>color|background-color)\s*:\s*(?P<value>[^;]+);?\s*$")


def normalized_attribute_value(name: str, value: str) -> str | None:
    """The canonical form the Markdown round-trip re-renders for an attribute.

    Only deterministic, value-free-by-construction normalizations are known:
    a single color/background-color style rule is coarsened to its palette
    token and re-rendered exactly like the cfx renderer does. Returns None
    when no canonical form is known (the caller must then fail closed).
    """

    if not name.endswith("style"):
        return None
    match = _STYLE_COLOR_PROP_RE.match(value)
    if match is None:
        return None
    from cfxmark.colors import coarse_color
    from cfxmark.renderers.cfx import _cfx_color_value

    token = coarse_color(match.group("value").strip())
    if token is None:
        return None
    return f"{match.group('prop')}:{_cfx_color_value(token, match.group('prop') == 'background-color')}"


def collateral_diagnostics(source: str, candidate: str) -> tuple[ConversionDiagnostic, ...]:
    if source == candidate:
        return ()
    source_root = _parse_fragment_to_element(source)
    candidate_root = _parse_fragment_to_element(candidate)
    source_fingerprint = hashlib.sha256(source.encode()).hexdigest()
    candidate_by_path = {
        storage_semantic_path(candidate_root, element): element for element in candidate_root.iterdescendants()
    }
    candidate_tags = {element.tag for element in candidate_root.iterdescendants() if isinstance(element.tag, str)}
    specs: list[tuple[str, DiagnosticCategory, DiagnosticSeverity, str, tuple[str, ...]]] = []

    text_hosts = {"p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
    for element in source_root.iterdescendants():
        path = storage_semantic_path(source_root, element)
        candidate_element = candidate_by_path.get(path)
        if isinstance(element.tag, str) and strip_ns(element.tag) in text_hosts:
            source_text = element.text
            candidate_text = candidate_element.text if candidate_element is not None else None
            if source_text and source_text.endswith((" ", "\t")) and source_text != candidate_text:
                specs.append(
                    (
                        "trailing-space-trimmed",
                        "content_loss",
                        "decision_required",
                        "Trailing endpoint whitespace is trimmed; use an explicit <br> for a hard break",
                        (*path, "#text[0]"),
                    )
                )
        parent = element.getparent()
        if parent is not None and isinstance(parent.tag, str) and strip_ns(parent.tag) in text_hosts:
            source_tail = element.tail
            candidate_tail = candidate_element.tail if candidate_element is not None else None
            if source_tail and source_tail.endswith((" ", "\t")) and source_tail != candidate_tail:
                specs.append(
                    (
                        "trailing-space-trimmed",
                        "content_loss",
                        "decision_required",
                        "Trailing endpoint whitespace is trimmed; use an explicit <br> for a hard break",
                        (*path, "#tail[0]"),
                    )
                )

        if (
            isinstance(element.tag, str)
            and strip_ns(element.tag) == "p"
            and len(element) == 0
            and not "".join(map(str, element.itertext()))
            and not (
                candidate_element is not None
                and len(candidate_element) == 0
                and not "".join(map(str, candidate_element.itertext()))
            )
        ):
            specs.append(
                (
                    "empty-paragraph-dropped",
                    "structural_loss",
                    "blocking",
                    "An empty paragraph is replaced by a Markdown line-break paragraph",
                    path,
                )
            )

        if (
            isinstance(element.tag, str)
            and strip_ns(element.tag) == "li"
            and len(element) == 1
            and isinstance(element[0].tag, str)
            and strip_ns(element[0].tag) == "p"
            and not (
                candidate_element is not None
                and len(candidate_element) == 1
                and isinstance(candidate_element[0].tag, str)
                and strip_ns(candidate_element[0].tag) == "p"
            )
        ):
            specs.append(
                (
                    "list-item-paragraph-unwrapped",
                    "structural_loss",
                    "decision_required",
                    "A list-item paragraph wrapper is removed",
                    (*path, "p[0]"),
                )
            )

        # A table cell whose only child is one paragraph loses that wrapper in
        # the Markdown round-trip, exactly like the list-item case above.
        if (
            isinstance(element.tag, str)
            and strip_ns(element.tag) in {"td", "th"}
            and len(element) == 1
            and isinstance(element[0].tag, str)
            and strip_ns(element[0].tag) == "p"
            and not (
                candidate_element is not None
                and len(candidate_element) == 1
                and isinstance(candidate_element[0].tag, str)
                and strip_ns(candidate_element[0].tag) == "p"
            )
        ):
            specs.append(
                (
                    "table-cell-paragraph-unwrapped",
                    "structural_loss",
                    "decision_required",
                    "A table-cell paragraph wrapper is removed",
                    (*path, "p[0]"),
                )
            )

        # A table cell holding several paragraphs collapses to one line-broken
        # cell: Markdown table cells cannot carry block paragraphs.
        if (
            isinstance(element.tag, str)
            and strip_ns(element.tag) in {"td", "th"}
            and len(element) >= 2
            and all(isinstance(child.tag, str) and strip_ns(child.tag) == "p" for child in element)
            and (candidate_element is not None or element.tag in candidate_tags)
            and not (
                candidate_element is not None
                and any(isinstance(child.tag, str) and strip_ns(child.tag) == "p" for child in candidate_element)
            )
        ):
            specs.append(
                (
                    "table-cell-paragraphs-collapsed",
                    "structural_loss",
                    "decision_required",
                    "Multiple cell paragraphs collapse into one line-broken cell",
                    path,
                )
            )

        # A body-only table (header cells living in the first tbody row) gets a
        # synthesized <thead> in the Markdown round-trip; the header row moves.
        if isinstance(element.tag, str) and strip_ns(element.tag) == "table" and candidate_element is not None:
            source_children = {strip_ns(child.tag) for child in element if isinstance(child.tag, str)}
            candidate_children = {strip_ns(child.tag) for child in candidate_element if isinstance(child.tag, str)}
            if "thead" not in source_children and "thead" in candidate_children:
                specs.append(
                    (
                        "body-only-table-header-synthesized",
                        "structural_loss",
                        "decision_required",
                        "The first body row of header cells moves into a synthesized table header",
                        path,
                    )
                )

        # Remote-only decoration/metadata attributes (li data-uuid, heading and
        # list style, table class, ...) have no Markdown projection; the
        # round-trip candidate silently drops them. Name each drop as an exact,
        # per-attribute occurrence so the ownership proof can attribute the
        # resulting leaf change instead of failing closed on it.
        if isinstance(element.tag, str) and (
            # A missing correspondence only means "moved" when the tag still
            # exists somewhere in the candidate; a tag the round-trip removed
            # outright (emoticon, stripped span) must not spawn attribute
            # candidates that poison the composition's consent-surface guard.
            (candidate_element is None and element.tag in candidate_tags)
            or (
                candidate_element is not None
                and isinstance(candidate_element.tag, str)
                and candidate_element.tag == element.tag
            )
        ):
            # When the candidate has no path-corresponding element the element
            # likely moved (e.g. a synthesized thead relocated its row); shape-
            # based candidates are still emitted — a wrong guess never gains
            # ownership because the atomic/composed witness must reproduce the
            # exact leaf value, so it merely stays a deferred occurrence.
            # SIM118 does not apply: iterating an lxml Element yields its child
            # elements, not its attribute names, so `.keys()` is the correct API.
            for raw_name in element.keys():  # noqa: SIM118
                if not isinstance(raw_name, str):  # lxml types keys as str | bytes
                    continue
                source_value = element.get(raw_name)
                candidate_value = candidate_element.get(raw_name) if candidate_element is not None else None
                normalized = normalized_attribute_value(raw_name, source_value) if source_value is not None else None
                if candidate_element is not None and candidate_value is None:
                    # The element corresponds and the attribute is gone: an
                    # observed drop. Name it an omission even when the value is
                    # normalizable — the normalization witness can only *set* the
                    # canonical value on the node, never reproduce a removal, so
                    # naming an observed drop "normalized" leaves the leaf unowned
                    # and fails closed on a change that is in fact nameable.
                    code = "element-attribute-omitted"
                elif candidate_value is None and normalized is not None and normalized != source_value:
                    # No path correspondence (the element moved), so the attribute
                    # is unobservable here. Keep the shape-based guess: it may well
                    # survive re-rendered at its new location. A wrong guess never
                    # gains ownership — the witness must still reproduce the leaf.
                    code = "element-attribute-normalized"
                elif candidate_value is None:
                    code = "element-attribute-omitted"
                elif candidate_value != source_value and normalized == candidate_value:
                    code = "element-attribute-normalized"
                else:
                    continue
                message = (
                    "A remote-only attribute value is re-rendered in its canonical form"
                    if code == "element-attribute-normalized"
                    else "A remote-only element attribute is not represented in Markdown"
                )
                specs.append((code, "presentation_loss", "decision_required", message, (*path, f"@{raw_name}[0]")))

        # Markdown tables carry no column-group definition; the whole
        # <colgroup> subtree (column widths) disappears from the candidate.
        if isinstance(element.tag, str) and strip_ns(element.tag) == "colgroup" and candidate_element is None:
            specs.append(
                (
                    "table-colgroup-omitted",
                    "presentation_loss",
                    "decision_required",
                    "A table column-group definition is not represented in Markdown",
                    path,
                )
            )
    return tuple(
        make_diagnostic(
            code=code,
            category=category,
            severity=severity,
            message=message,
            source_fingerprint=source_fingerprint,
            ordinal=ordinal,
            location=DiagnosticLocation(path),
        )
        for ordinal, (code, category, severity, message, path) in enumerate(specs)
    )


def unrenderable_background_diagnostics(
    document: Document,
    *,
    source_fingerprint: str,
    string_only: bool,
) -> tuple[ConversionDiagnostic, ...]:
    backgrounds: list[CellBackground] = []

    def visit(value: Any) -> None:
        if isinstance(value, Node):
            if hasattr(value, "background") and isinstance(value.background, CellBackground):
                backgrounds.append(value.background)
            for item in fields(value):
                child = getattr(value, item.name)
                if isinstance(child, Node):
                    visit(child)
                elif isinstance(child, tuple):
                    for member in child:
                        visit(member)

    visit(document)
    diagnostics: list[ConversionDiagnostic] = []
    for ordinal, background in enumerate(backgrounds):
        if string_only:
            code = "table-cell-background-untracked"
            message = "table cell background is not represented by the string-only editable API"
        elif background.render_strategy is None:
            code = "table-cell-background-unrenderable"
            message = "table cell background has no verified Confluence renderer"
        else:
            continue
        diagnostics.append(
            make_diagnostic(
                code=code,
                category="presentation_loss",
                severity="blocking",
                message=message,
                source_fingerprint=source_fingerprint,
                ordinal=ordinal,
            )
        )
    return tuple(diagnostics)


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "CfxArtifact",
    "ManagedCfxArtifact",
    "MacroIdentity",
    "MarkdownArtifact",
    "MarkdownRange",
    "ProtectedRegionArtifact",
    "apply_macro_identities",
    "base_maps",
    "candidate_maps",
    "macro_identities",
    "collateral_diagnostics",
    "semantic_snapshot",
    "unrenderable_background_diagnostics",
    "validate_cfx_artifact",
    "validate_managed_cfx_artifact",
]
