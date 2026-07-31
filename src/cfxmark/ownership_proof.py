"""Source-bound ownership proof for managed Markdown publish candidates.

The proof keeps the three storage diffs separate from their owners.  A changed
leaf is accepted only when a producer source map binds it to one real migration
occurrence ID or the Markdown matcher binds it to one real edit operation ID.
Membership in a stage diff never creates an owner.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from cfxmark.artifacts import base_maps
from cfxmark.change_ownership import (
    ChangeArtifacts,
    LeafChange,
    LeafIdentity,
    LeafSnapshot,
    OwnerRef,
    OwnershipResult,
    classify_changes,
)
from cfxmark.exceptions import ConversionError
from cfxmark.migration_report import effect_for_code, occurrences_from_diagnostics
from cfxmark.presentation import PresentationArtifact, presentation_cell_occurrence_id
from cfxmark.source_map import (
    SemanticNode,
    SourceMapEntry,
    StorageLeafRef,
    canonical_move_operation_ids,
    fingerprint_semantic_value,
    operation_id,
)
from cfxmark.storage_map import extract_storage_source, storage_leaf_identity

if TYPE_CHECKING:
    from cfxmark.artifacts import CfxArtifact, MarkdownArtifact, RemoteSubtreeArtifact


_MIGRATION_EFFECTS = frozenset({"converted", "normalized", "removed", "unsupported"})
_SOURCE_MAP_FATAL_CODES = frozenset({"semantic-mapping-ambiguous", "semantic-source-map-incomplete"})


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _identity(leaf: StorageLeafRef) -> LeafIdentity:
    path, field, attribute_name, ordinal = storage_leaf_identity(leaf)
    return path, field, attribute_name, ordinal


def _fingerprints(storage: str) -> dict[LeafIdentity, str]:
    return {_identity(leaf): leaf.value_fingerprint for leaf in extract_storage_source(storage).leaves}


def storage_semantically_equal(left: str, right: str) -> bool:
    """Whether two storage documents parse to the same semantic leaf set.

    Serialization-only differences -- entity re-encoding (``&quot;`` vs ``"``),
    self-closing whitespace (``<hr/>`` vs ``<hr />``) -- disappear during
    parsing, so only content, structure, or attribute changes break equality.
    This is exactly the basis the ownership proof uses to define a change.
    Confluence re-serializes storage on save, so a publish read-back check
    should compare with this instead of a byte hash.
    """

    return _fingerprints(left) == _fingerprints(right)


# The bounded server-envelope allowlist. Confluence stamps transport-only
# metadata onto ``ac:structured-macro`` elements on save; these are the ONLY
# attributes the post-write and skip-PUT comparators may ignore, and only when
# the candidate did not already carry them. The strict proof / source-map
# contract (``storage_semantically_equal``) never applies this allowlist.
_SERVER_ENVELOPE_POLICY = "server-envelope-allowlist:v1"
_SCHEMA_VERSION_ALLOWED_VALUE = "1"
_SCHEMA_VERSION_ALLOWED_FINGERPRINT = fingerprint_semantic_value(_SCHEMA_VERSION_ALLOWED_VALUE)
_ENVELOPE_MISS_RESOLUTION = "keep-remote"


@dataclass(frozen=True)
class StorageComparisonSummary:
    """Value-free explanation of a storage-comparison mismatch.

    Carries a stable ``code``, the canonical leaf ``identity`` that failed, the
    ``policy`` identifier that governs the allowlist, and a ``resolution`` code.
    It NEVER carries the observed schema value, a fingerprint, or raw storage.
    Not exported; reached only through :attr:`StorageComparison.public_summary`.
    """

    code: str
    identity: LeafIdentity
    policy: str
    resolution: str


@dataclass(frozen=True)
class StorageComparison:
    """Immutable result of a server-envelope-aware storage comparison.

    ``equivalent`` is the boolean verdict; ``public_summary`` is a value-free
    explanation of the first disallowed mismatch (``None`` when equivalent).
    """

    equivalent: bool
    public_summary: StorageComparisonSummary | None = None


def _is_structured_macro_attribute(identity: LeafIdentity, attribute_name: str) -> bool:
    path, field, name, _ordinal = identity
    return (
        field == "attribute" and name == attribute_name and bool(path) and path[-1].startswith("ac:structured-macro[")
    )


def compare_server_envelope(expected: str, observed: str, mode: str) -> StorageComparison:
    """Private structured-macro server-envelope classifier for both comparators.

    Scoped to ``ac:structured-macro`` attributes. Asymmetric allowlist: when the
    candidate side (``expected``) does NOT carry the attribute, a server side
    (``observed``) that ADDS ``ac:macro-id`` (any value) or ``ac:schema-version``
    with value ``"1"`` is tolerated. A candidate-carried id/schema the server
    changed or removed, a server-added ``ac:schema-version`` whose value is not
    ``"1"``, and every non-envelope difference (macro name, parameters, body,
    structure, list ``start``) all reject. The observed schema value is inspected
    internally but never leaks into the public summary. Internal only.
    """

    expected_leaves = _fingerprints(expected)
    observed_leaves = _fingerprints(observed)
    tolerated: set[LeafIdentity] = set()
    for identity, value in observed_leaves.items():
        if identity in expected_leaves:
            # Only a server-ADDED envelope leaf is tolerable; a candidate-carried
            # id/schema that changed keeps its identity here and must reject.
            continue
        if _is_structured_macro_attribute(identity, "ac:macro-id"):
            tolerated.add(identity)
        elif _is_structured_macro_attribute(identity, "ac:schema-version"):
            if value == _SCHEMA_VERSION_ALLOWED_FINGERPRINT:
                tolerated.add(identity)
    pruned_observed = {identity: value for identity, value in observed_leaves.items() if identity not in tolerated}
    if expected_leaves == pruned_observed:
        return StorageComparison(equivalent=True)

    mismatched = sorted(
        identity
        for identity in expected_leaves.keys() | pruned_observed.keys()
        if expected_leaves.get(identity) != pruned_observed.get(identity)
    )
    first = mismatched[0]
    if _is_structured_macro_attribute(first, "ac:macro-id") or _is_structured_macro_attribute(
        first, "ac:schema-version"
    ):
        code = "server_envelope_allowlist_miss"
    else:
        code = "storage_leaf_mismatch"
    return StorageComparison(
        equivalent=False,
        public_summary=StorageComparisonSummary(
            code=code,
            identity=first,
            policy=f"{_SERVER_ENVELOPE_POLICY}:{mode}",
            resolution=_ENVELOPE_MISS_RESOLUTION,
        ),
    )


def storage_readback_comparison(expected: str, observed: str) -> StorageComparison:
    """Post-write read-back comparison: does the server read-back match what was written?

    Value-free :class:`StorageComparison`: tolerates the server stamping
    ``ac:macro-id`` or ``ac:schema-version="1"`` that the candidate did not carry,
    and rejects every other difference, including a candidate-carried id/schema the
    server changed or removed. Use only for the post-PUT read-back; the ownership
    proof and source map keep the strict ``storage_semantically_equal`` contract.
    """

    return compare_server_envelope(expected, observed, mode="readback")


def storage_noop_comparison(candidate: str, remote: str) -> StorageComparison:
    """Skip-PUT no-op comparison: is publishing ``candidate`` a no-op against ``remote``?

    Applies the same asymmetric structured-macro server-envelope allowlist as
    :func:`storage_readback_comparison`, but preflight: a fresh candidate that lacks
    the ``ac:macro-id`` / ``ac:schema-version="1"`` the remote already carries is
    still a no-op, so the PUT can be skipped. Every real difference is a change.
    """

    return compare_server_envelope(candidate, remote, mode="noop")


def storage_readback_equivalent(expected: str, observed: str) -> bool:
    """Whether a server read-back matches the written candidate for publish safety.

    Stricter than a byte hash but looser than :func:`storage_semantically_equal`
    in exactly one **asymmetric** way: it tolerates the server *stamping* an
    ``ac:macro-id`` onto a ``ac:structured-macro`` that the candidate left
    unstamped. A fresh Markdown-first candidate carries no macro-id, so
    Confluence assigns one on save; that added id is not a content change.

    But a managed round-trip that reuses an existing macro *does* carry that
    macro's id (opaque-macro instance reuse preserves the macro's comments,
    permissions, and attachments). Such an id is remote-backed identity, so the
    tolerance is one-directional: a macro-id the candidate already carries must
    survive the round-trip unchanged. A server that **changes or removes** an
    existing id breaks equality, as does every other diff -- macro name,
    parameters, plain-text body, ``ac:schema-version``, list ``start``, opaque
    payload, and every other attribute.

    W2 broadens the tolerance to also accept a server-added
    ``ac:schema-version="1"`` (code / jira macros carry no schema on a fresh
    Markdown-first candidate; the server stamps ``"1"`` on save). A
    ``schema-version`` value other than ``"1"``, and a candidate-carried
    id/schema that is changed or removed, still fail — the post-PUT asymmetric
    meaning is unchanged.

    Use this ONLY for the post-write read-back check. The ownership proof and
    source map keep the strict ``storage_semantically_equal`` /
    ``extract_storage_source`` contract, where ``ac:macro-id`` is always a
    remote-backed identity. Compat boolean over
    :func:`storage_readback_comparison`.
    """

    return storage_readback_comparison(expected, observed).equivalent


@dataclass(frozen=True)
class StorageLeafDiff:
    """Value-free identity diff between two storage documents.

    Reports only leaf *identities* — never fingerprints or leaf values — in
    three disjoint groups: present only on the left, present only on the right,
    or present on both with a differing value. Internal to cfxmark; the public
    comparators surface their own value-free ``public_summary`` instead of this.
    """

    only_left: tuple[LeafIdentity, ...]
    only_right: tuple[LeafIdentity, ...]
    value_differs: tuple[LeafIdentity, ...]


def storage_leaf_diff(left: str, right: str) -> StorageLeafDiff:
    """Diff two storage documents by leaf identity, dropping every leaf value.

    Replaces ad-hoc ``_fingerprints`` set inspection: a caller gets the three
    identity groups without ever handling a fingerprint itself. Internal only.
    """

    left_leaves = _fingerprints(left)
    right_leaves = _fingerprints(right)
    shared = left_leaves.keys() & right_leaves.keys()
    return StorageLeafDiff(
        only_left=tuple(sorted(left_leaves.keys() - right_leaves.keys())),
        only_right=tuple(sorted(right_leaves.keys() - left_leaves.keys())),
        value_differs=tuple(sorted(identity for identity in shared if left_leaves[identity] != right_leaves[identity])),
    )


def _witness_leaf_document(leaf: StorageLeafRef) -> list[object]:
    return [
        list(leaf.semantic_path),
        leaf.field,
        leaf.attribute_name,
        leaf.occurrence_ordinal,
        leaf.value_fingerprint,
    ]


def attribution_witness_sha256(
    *,
    base_source_map_entries: Sequence[SourceMapEntry],
    base_semantic_nodes: Sequence[SemanticNode],
    baseline_source_map_entries: Sequence[SourceMapEntry],
    baseline_semantic_nodes: Sequence[SemanticNode],
    baseline_matches_source_storage: bool,
    attachment_filename_map: Sequence[tuple[str, str]],
    remote_subtrees: Sequence[RemoteSubtreeArtifact],
    presentation_authority: PresentationArtifact,
    options_sha256: str,
) -> str:
    """Fingerprint the independent source-map witness used to verify user owners."""

    def json_default(value: object) -> object:
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"unsupported attribution witness value: {type(value).__name__}")

    def entry_document(entry: SourceMapEntry) -> list[object]:
        return [
            entry.operation_id,
            entry.source_kind,
            entry.node_or_occurrence_id,
            [_witness_leaf_document(leaf) for leaf in sorted(entry.leaves, key=_identity)],
        ]

    def node_document(node: SemanticNode) -> list[object]:
        return [
            node.node_id,
            node.node_type,
            node.parent_id,
            node.ordinal,
            node.subtree_fingerprint,
            node.local_fingerprint,
            node.topology_fingerprint,
            node.explicit_anchor,
            [_witness_leaf_document(leaf) for leaf in sorted(node.leaves, key=_identity)],
        ]

    def subtree_document(subtree: RemoteSubtreeArtifact) -> list[object]:
        return [
            subtree.node_id,
            list(subtree.remote_node_path),
            subtree.base_markdown_range.start,
            subtree.base_markdown_range.end,
            subtree.projection_fingerprint,
            subtree.remote_subtree_fingerprint,
            subtree.requires_fresh_source,
        ]

    payload = json.dumps(
        [
            "cfxmark-attribution-witness-v2",
            baseline_matches_source_storage,
            list(attachment_filename_map),
            asdict(presentation_authority),
            options_sha256,
            [entry_document(entry) for entry in base_source_map_entries],
            [node_document(node) for node in base_semantic_nodes],
            [entry_document(entry) for entry in baseline_source_map_entries],
            [node_document(node) for node in baseline_semantic_nodes],
            [subtree_document(subtree) for subtree in remote_subtrees],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        default=json_default,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class _AttributionCandidate(Protocol):
    @property
    def semantic_nodes(self) -> Sequence[SemanticNode]: ...

    @property
    def source_map_entries(self) -> Sequence[SourceMapEntry]: ...


def _canonical_move_owner_ids(baseline: _AttributionCandidate, candidate: CfxArtifact) -> set[str]:
    """Choose a deterministic minimal subset of real move operations as storage owners."""
    return canonical_move_operation_ids(
        baseline.semantic_nodes,
        candidate.semantic_nodes,
        candidate.edit_operations,
    )


def _changes(before: dict[LeafIdentity, str], after: dict[LeafIdentity, str]) -> dict[LeafIdentity, LeafChange]:
    return {
        identity: LeafChange(identity, before.get(identity), after.get(identity))
        for identity in before.keys() | after.keys()
        if before.get(identity) != after.get(identity)
    }


def _entries_by_identity(entries: Iterable[SourceMapEntry]) -> dict[LeafIdentity, tuple[SourceMapEntry, ...]]:
    grouped: dict[LeafIdentity, list[SourceMapEntry]] = defaultdict(list)
    for entry in entries:
        for leaf in entry.leaves:
            grouped[_identity(leaf)].append(entry)
    return {identity: tuple(items) for identity, items in grouped.items()}


def _owner_ref(owners: Iterable[str]) -> OwnerRef | None:
    unique = tuple(sorted(set(owners)))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return unique


def _deduplicate_changes(changes: Iterable[LeafChange]) -> tuple[LeafChange, ...]:
    by_identity: dict[LeafIdentity, LeafChange] = {}
    for change in changes:
        by_identity[change.identity] = change
    return tuple(by_identity[identity] for identity in sorted(by_identity))


def _descendant_ids(nodes: Sequence[SemanticNode], roots: Iterable[str]) -> frozenset[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node.parent_id is not None:
            children[node.parent_id].append(node.node_id)
    pending = list(roots)
    found: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in found:
            continue
        found.add(node_id)
        pending.extend(children.get(node_id, ()))
    return frozenset(found)


def _maps_cover_storage(storage: str, entries: Sequence[SourceMapEntry]) -> bool:
    actual = Counter(extract_storage_source(storage).leaves)
    reported = Counter(leaf for entry in entries for leaf in entry.leaves)
    return bool(entries) and actual == reported


def _base_inverse_is_complete(base: MarkdownArtifact, source_storage: str) -> bool:
    if not base.base_storage_to_node or not base.base_node_to_storage or not base.source_map_entries:
        return False
    if not _maps_cover_storage(source_storage, base.source_map_entries):
        return False
    forward = {(_identity(leaf), owner) for leaf, owner in base.base_storage_to_node.items()}
    inverse = {(_identity(leaf), owner) for owner, leaves in base.base_node_to_storage.items() for leaf in leaves}
    return forward == inverse


def _candidate_map_is_complete(candidate: CfxArtifact) -> bool:
    if candidate.unmapped_storage_leaves or not _maps_cover_storage(candidate.xhtml, candidate.source_map_entries):
        return False
    forward = {(_identity(leaf), owner) for leaf, owner in candidate.candidate_storage_to_operation.items()}
    inverse = {
        (_identity(leaf), owner) for owner, leaves in candidate.edited_operation_to_storage.items() for leaf in leaves
    }
    return forward == inverse


def _table_presentation_owner(base: MarkdownArtifact, diagnostic: Any) -> str | None:
    path = diagnostic.location.semantic_path
    if len(path) != 1 or not path[0].startswith("table:"):
        return None
    table_id = path[0].removeprefix("table:")
    row = diagnostic.location.table_row
    column = diagnostic.location.table_column
    if row is None or column is None:
        return None
    matches = [
        cell
        for cell in base.presentation.cells
        if cell.table_id == table_id and cell.anchor_row_index == row and cell.anchor_column_index == column
    ]
    if len(matches) != 1:
        return None
    cell = matches[0]
    return presentation_cell_occurrence_id(
        cell.table_id,
        cell.topology_hash,
        cell.anchor_row_index,
        cell.anchor_column_index,
    )


def _migration_owners(
    *,
    base: MarkdownArtifact,
    source_storage: str,
    baseline: CfxArtifact,
    migration_changes: dict[LeafIdentity, LeafChange],
    atomic_migration_storage: Mapping[str, str],
    registry: Any | None,
    composed_migration_storage: tuple[str, tuple[str, ...]] | None = None,
) -> tuple[dict[LeafIdentity, OwnerRef], bool, bool]:
    source_occurrences = occurrences_from_diagnostics(
        base.diagnostics,
        source_storage_sha256=base.source_storage_sha256,
    )
    occurrence_by_diagnostic = {
        diagnostic.occurrence_id: occurrence
        for diagnostic, occurrence in zip(base.diagnostics, source_occurrences, strict=True)
    }
    if len(occurrence_by_diagnostic) != len(base.diagnostics):
        return {}, False, True
    _nodes, _storage_to_node, _node_to_storage, raw_entries, _unmapped = base_maps(
        base.document,
        source_storage,
        registry=registry,
    )
    baseline_entries = tuple(baseline.source_map_entries)
    claims: dict[LeafIdentity, list[str]] = defaultdict(list)
    relevant_atomic_occurrences: set[str] = set()

    for diagnostic in base.diagnostics:
        occurrence = occurrence_by_diagnostic[diagnostic.occurrence_id]
        if occurrence.effect not in _MIGRATION_EFFECTS:
            continue
        resolved_storage = atomic_migration_storage.get(diagnostic.occurrence_id)
        atomic_changes = (
            _changes(_fingerprints(source_storage), _fingerprints(resolved_storage))
            if resolved_storage is not None
            else {}
        )
        if atomic_changes.keys() & migration_changes.keys():
            relevant_atomic_occurrences.add(occurrence.occurrence_id)

        source_scope: set[LeafIdentity] = set()
        candidate_scope: set[LeafIdentity] = set()
        for entry in base.source_map_entries:
            if entry.source_kind == "migration" and entry.node_or_occurrence_id == diagnostic.occurrence_id:
                source_scope.update(_identity(leaf) for leaf in entry.leaves)

        presentation_owner = _table_presentation_owner(base, diagnostic)
        if presentation_owner is not None:
            for entry in raw_entries:
                if entry.source_kind == "presentation" and entry.node_or_occurrence_id == presentation_owner:
                    source_scope.update(_identity(leaf) for leaf in entry.leaves)
            for entry in baseline_entries:
                if entry.source_kind == "presentation" and entry.node_or_occurrence_id == presentation_owner:
                    candidate_scope.update(_identity(leaf) for leaf in entry.leaves)

        path = diagnostic.location.semantic_path
        if path and not path[0].startswith("source[") and presentation_owner is None:
            if path[-1:] in {("#text[0]",), ("#tail[0]",)}:
                projection_path = path[:-1]
                projection_field = "text" if path[-1] == "#text[0]" else "tail"
                source_scope.update(
                    identity
                    for identity in migration_changes
                    if identity[0] == projection_path and identity[1] == projection_field
                )
            elif path[-1].startswith("@") and path[-1].endswith("[0]"):
                # Attribute-scoped occurrence (element-attribute-omitted): the
                # location names the exact attribute, so scope is that single
                # attribute identity; the atomic witness still has to match.
                projection_path = path[:-1]
                attribute_name = path[-1][1:-3]
                source_scope.update(
                    identity
                    for identity in migration_changes
                    if identity[0] == projection_path and identity[1] == "attribute" and identity[2] == attribute_name
                )
            elif diagnostic.code in {"empty-paragraph-dropped", "body-only-table-header-synthesized"}:
                projection_path = path
            else:
                projection_path = path[:-1]
            if diagnostic.code == "layout-flattened":
                projection_path = ()
            for entry in raw_entries:
                source_scope.update(
                    _identity(leaf)
                    for leaf in entry.leaves
                    if leaf.semantic_path[: len(projection_path)] == projection_path
                )
            for entry in baseline_entries:
                candidate_scope.update(
                    _identity(leaf)
                    for leaf in entry.leaves
                    if leaf.semantic_path[: len(projection_path)] == projection_path
                )

        for identity, migration_change in migration_changes.items():
            atomic_change = atomic_changes.get(identity)
            value_matches_atomic_occurrence = (
                atomic_change is not None
                and atomic_change.before_fingerprint == migration_change.before_fingerprint
                and atomic_change.after_fingerprint == migration_change.after_fingerprint
            )
            # Presentation scope narrows where an occurrence may apply, but
            # scope membership never supplies ownership.  A migration owner
            # requires the same independently resolved before/after value as
            # every other atomic occurrence.  Preserved table presentation has
            # no migration diff; a changed value therefore remains unowned.
            if value_matches_atomic_occurrence and (identity in source_scope or identity in candidate_scope):
                claims[identity].append(occurrence.occurrence_id)

    owners = {identity: owner for identity, owner_ids in claims.items() if (owner := _owner_ref(owner_ids)) is not None}
    claimed_occurrences = {owner_id for owner_ids in claims.values() for owner_id in owner_ids}

    # Composite effects: several independently witnessed migrations can meet at
    # one leaf (a colgroup drop and a thead synthesis both shift the table's
    # child list; a header-row move relocates a cell whose paragraph wrapper a
    # second migration unwraps). No single atomic witness value-matches such a
    # leaf. When the ordered composition of every witnessed migration reproduces
    # the leaf's exact after-value, the leaf is attributed to the location-nearest
    # contributing occurrence (a real, report-projected occurrence id, so the
    # consent surface and every downstream identity check stay intact). The
    # attribution is withdrawn wholesale — leaves stay unclassified and fail
    # closed — if any explained leaf finds no contributing anchor or any
    # otherwise-unsurfaced contributor would end up outside the consent surface.
    if composed_migration_storage is not None:
        composed_storage, applied_ids = composed_migration_storage
        diagnostic_by_id = {diagnostic.occurrence_id: diagnostic for diagnostic in base.diagnostics}
        applied_occurrence_ids = {
            diagnostic_id: occurrence_by_diagnostic[diagnostic_id].occurrence_id
            for diagnostic_id in applied_ids
            if diagnostic_id in occurrence_by_diagnostic
        }
        if applied_ids and len(applied_occurrence_ids) == len(set(applied_ids)):
            composed = _fingerprints(composed_storage)
            explained = {
                identity
                for identity, change in migration_changes.items()
                if identity not in owners and composed.get(identity) == change.after_fingerprint
            }
            if explained:
                anchors: list[tuple[str, tuple[str, ...]]] = []
                for diagnostic_id in applied_ids:
                    diagnostic = diagnostic_by_id[diagnostic_id]
                    projected_id = applied_occurrence_ids[diagnostic_id]
                    path = diagnostic.location.semantic_path
                    if path and (path[-1].startswith("@") or path[-1] in ("#text[0]", "#tail[0]")):
                        path = path[:-1]
                    anchors.append((projected_id, path))
                    if path:
                        anchors.append((projected_id, path[:-1]))
                assignment: dict[LeafIdentity, str] = {}
                surfaced = set(claimed_occurrences)
                for identity in sorted(explained):
                    leaf_path = identity[0]
                    best: tuple[int, int, str] | None = None
                    for projected_id, anchor_path in anchors:
                        if leaf_path[: len(anchor_path)] != anchor_path:
                            continue
                        # Prefer a contributor that has not surfaced on the consent
                        # surface yet, then the deepest anchor, then the smallest id
                        # — deterministic, and it spreads the composite leaves so
                        # every applied occurrence ends up consentable.
                        key = (
                            0 if projected_id in surfaced else 1,
                            len(anchor_path),
                            projected_id,
                        )
                        if (
                            best is None
                            or key[0] > best[0]
                            or (key[0] == best[0] and key[1] > best[1])
                            or (key[0] == best[0] and key[1] == best[1] and key[2] < best[2])
                        ):
                            best = key
                    if best is None:
                        assignment = {}
                        break
                    assignment[identity] = best[2]
                    surfaced.add(best[2])
                if assignment:
                    # Every applied contributor must end up on the consent surface
                    # (proof.migration owners feed the accepted-occurrence set).
                    # Composition mixes effects, so per-leaf causality is coarse
                    # here by construction; re-assign one composite leaf to each
                    # contributor the depth pass left unsurfaced. Value safety is
                    # already established by the whole-composition fingerprint
                    # match, and a shortage of leaves withdraws the attribution.
                    unsurfaced = sorted(
                        set(applied_occurrence_ids.values()) - claimed_occurrences - set(assignment.values())
                    )
                    reassignable = [
                        identity
                        for identity, owner_id in sorted(assignment.items())
                        if sum(1 for other in assignment.values() if other == owner_id) > 1
                    ]
                    if len(unsurfaced) <= len(reassignable):
                        for contributor, identity in zip(unsurfaced, reassignable, strict=False):
                            assignment[identity] = contributor
                        owners.update(assignment)
                        claimed_occurrences.update(applied_occurrence_ids.values())

    return owners, not relevant_atomic_occurrences <= claimed_occurrences, False


def _user_owners(
    *,
    baseline: _AttributionCandidate,
    candidate: CfxArtifact,
    user_changes: dict[LeafIdentity, LeafChange],
) -> dict[LeafIdentity, OwnerRef]:
    entries = _entries_by_identity(candidate.source_map_entries)
    move_owner_ids = _canonical_move_owner_ids(baseline, candidate)
    operations = {operation_id(item): item for item in candidate.edit_operations}
    relocated_scope = {
        owner: frozenset(_identity(tombstone) for tombstone in operation.relocated_leaf_tombstones)
        for owner, operation in operations.items()
    }
    delete_owners: dict[LeafIdentity, list[str]] = defaultdict(list)
    relocation_owners: dict[LeafIdentity, list[str]] = defaultdict(list)
    for owner, operation in operations.items():
        if operation.kind == "move" and owner not in move_owner_ids:
            continue
        for tombstone in operation.deleted_leaf_tombstones:
            delete_owners[_identity(tombstone)].append(owner)
        for tombstone in operation.relocated_leaf_tombstones:
            relocation_owners[_identity(tombstone)].append(owner)

    result: dict[LeafIdentity, OwnerRef] = {}
    for identity, change in user_changes.items():
        direct_owners: set[str] = set()
        if change.after_fingerprint is not None:
            for entry in entries.get(identity, ()):
                direct_operation = operations.get(entry.operation_id)
                if direct_operation is not None and (
                    direct_operation.kind != "move" or entry.operation_id in move_owner_ids
                ):
                    direct_owners.add(entry.operation_id)
        # A deletion owns the value it removes even when a following sibling
        # is serialized into the same positional leaf.  Otherwise the emitted
        # after-leaf is the strongest witness; relocation tombstones are used
        # only for old identities that no longer exist.
        relocation = set(relocation_owners.get(identity, ()))
        if len(relocation) > 1:
            relocation = _most_specific_relocation_owners(relocation, relocated_scope)
        owners = set(delete_owners.get(identity, ())) or direct_owners or relocation
        owner_ref = _owner_ref(owners)
        if owner_ref is not None:
            result[identity] = owner_ref
    return result


def _most_specific_relocation_owners(
    candidates: set[str],
    relocated_scope: dict[str, frozenset[LeafIdentity]],
) -> set[str]:
    """Keep only the tightest relocations that claim a shared leaf.

    A topology-only relocation is fallback evidence: a large edit (for example
    replacing one paragraph with several blocks) shifts every following sibling,
    and each such shift is recorded as an ancestor topology update whose
    relocated tombstones expand to cover a broad downstream region. When several
    of these regions overlap on the same leaf, the broad ancestor must not be a
    co-owner of a leaf that a strictly narrower relocation already scopes.

    Drop any candidate whose relocated set is a strict superset of another
    candidate's, so the most specific relocation wins. Genuinely incomparable
    relocations all survive and remain multiply owned, so a real overlapping
    move still fails closed rather than being silently attributed.
    """

    return {
        owner
        for owner in candidates
        if not any(
            other != owner and relocated_scope.get(other, frozenset()) < relocated_scope.get(owner, frozenset())
            for other in candidates
        )
    }


def _move_edit_is_ambiguous(candidate: CfxArtifact) -> bool:
    nodes = {node.node_id: node for node in candidate.semantic_nodes}
    moves = [item for item in candidate.edit_operations if item.kind == "move" and item.edited_node_id is not None]
    non_moves = [item for item in candidate.edit_operations if item.kind != "move" and not item.topology_only]
    for move in moves:
        assert move.edited_node_id is not None
        descendants = _descendant_ids(candidate.semantic_nodes, (move.edited_node_id,))
        for operation in non_moves:
            node_id = operation.edited_node_id or operation.base_node_id
            if node_id in descendants and node_id != move.edited_node_id:
                return True
            if node_id is not None and move.edited_node_id in _descendant_ids(candidate.semantic_nodes, (node_id,)):
                return True
    return any(move.edited_node_id not in nodes for move in moves)


def _verify_no_hidden_migration_owners(
    *,
    artifact: CfxArtifact,
    fresh_candidate: CfxArtifact,
) -> None:
    """Reject user attribution for a leaf fresh storage proves migration-owned.

    The former exception existed only for ``empty-paragraph-dropped``. The blank
    paragraph marker removes that migration itself, so no production migration
    may dissolve a migration/user overlap. This independent check keeps future
    structural migrations fail-closed unless their complete ownership contract
    is deliberately redesigned.
    """

    ownership = artifact.ownership_proof
    assert ownership is not None
    fresh_proof = fresh_candidate.ownership_proof
    if fresh_proof is None:
        # This check is the independent half of the source-bound proof. Without
        # a fresh proof there is no authority for saying that a leaf belongs to
        # the author rather than a migration, so accepting the candidate would
        # turn a missing verifier result into permission to publish.
        raise ConversionError("publish preflight cannot verify migration ownership without an independent proof")
    migration_leaves = {change.identity for change in fresh_proof.migration}
    for change in ownership.intended:
        if change.identity in migration_leaves:
            raise ConversionError("candidate hides a migration owner behind user attribution")


def prove_candidate_ownership(
    *,
    source_storage: str,
    base_artifact: MarkdownArtifact,
    baseline_candidate: CfxArtifact,
    edited_markdown: str,
    final_candidate: CfxArtifact,
    converter: str,
    profile: str,
    atomic_migration_storage: Mapping[str, str] | None = None,
    composed_migration_storage: tuple[str, tuple[str, ...]] | None = None,
    attachment_filename_map: Sequence[tuple[str, str]] = (),
    match_base_semantic_nodes: Sequence[SemanticNode] | None = None,
    match_base_source_map_entries: Sequence[SourceMapEntry] | None = None,
    presentation_authority: PresentationArtifact,
    options_sha256: str,
    registry: Any | None = None,
) -> OwnershipResult:
    """Build the immutable S0/M0/C0/M1/C1 proof consumed by publish preflight."""

    source = _fingerprints(source_storage)
    baseline = _fingerprints(baseline_candidate.xhtml)
    final = _fingerprints(final_candidate.xhtml)
    migration_changes = _changes(source, baseline)
    user_changes = _changes(baseline, final)
    final_changes = _changes(source, final)

    fatal_codes: list[str] = []
    if base_artifact.source_storage_sha256 != hashlib.sha256(source_storage.encode()).hexdigest():
        fatal_codes.append("semantic-source-map-incomplete")
    if not _base_inverse_is_complete(base_artifact, source_storage):
        fatal_codes.append("semantic-source-map-incomplete")
    if not _candidate_map_is_complete(baseline_candidate) or not _candidate_map_is_complete(final_candidate):
        fatal_codes.append("semantic-source-map-incomplete")
    for diagnostic in (*baseline_candidate.diagnostics, *final_candidate.diagnostics):
        if diagnostic.code in _SOURCE_MAP_FATAL_CODES or effect_for_code(diagnostic.code) == "fatal":
            fatal_codes.append(diagnostic.code)
    if _move_edit_is_ambiguous(final_candidate):
        fatal_codes.append("semantic-mapping-ambiguous")

    migration_owners, has_unowned_atomic_occurrence, has_ambiguous_occurrence_mapping = _migration_owners(
        base=base_artifact,
        source_storage=source_storage,
        baseline=baseline_candidate,
        migration_changes=migration_changes,
        atomic_migration_storage=atomic_migration_storage or {},
        registry=registry,
        composed_migration_storage=composed_migration_storage,
    )
    if has_unowned_atomic_occurrence:
        fatal_codes.append("unclassified-storage-change")
    if has_ambiguous_occurrence_mapping:
        fatal_codes.append("semantic-mapping-ambiguous")
    user_owners = _user_owners(
        baseline=baseline_candidate,
        candidate=final_candidate,
        user_changes=user_changes,
    )
    preserved_count = sum(1 for identity, value in source.items() if final.get(identity) == value)
    result = classify_changes(
        artifacts=ChangeArtifacts(
            source_storage_sha256=_sha256(source_storage),
            base_markdown_sha256=_sha256(base_artifact.markdown),
            baseline_candidate_sha256=_sha256(baseline_candidate.xhtml),
            edited_markdown_sha256=_sha256(edited_markdown),
            final_candidate_sha256=_sha256(final_candidate.xhtml),
            converter=converter,
            profile=profile,
            attribution_witness_sha256=attribution_witness_sha256(
                base_source_map_entries=(
                    match_base_source_map_entries
                    if match_base_source_map_entries is not None
                    else base_artifact.source_map_entries
                ),
                base_semantic_nodes=(
                    match_base_semantic_nodes if match_base_semantic_nodes is not None else base_artifact.semantic_nodes
                ),
                baseline_source_map_entries=baseline_candidate.source_map_entries,
                baseline_semantic_nodes=baseline_candidate.semantic_nodes,
                baseline_matches_source_storage=baseline_candidate.xhtml == source_storage,
                attachment_filename_map=attachment_filename_map,
                remote_subtrees=base_artifact.remote_subtrees,
                presentation_authority=presentation_authority,
                options_sha256=options_sha256,
            ),
        ),
        final_changes=final_changes.values(),
        migration_owners=migration_owners,
        user_owners=user_owners,
        preserved_count=preserved_count,
        fatal_diagnostic_codes=fatal_codes,
    )

    migration_unclassified = (
        change for identity, change in migration_changes.items() if identity not in migration_owners
    )
    user_unclassified = (change for identity, change in user_changes.items() if identity not in user_owners)
    migration_multiple = (
        change for identity, change in migration_changes.items() if isinstance(migration_owners.get(identity), tuple)
    )
    user_multiple = (
        change for identity, change in user_changes.items() if isinstance(user_owners.get(identity), tuple)
    )

    return replace(
        result,
        source_leaves=tuple(LeafSnapshot(identity, fingerprint) for identity, fingerprint in sorted(source.items())),
        unclassified=_deduplicate_changes((*result.unclassified, *migration_unclassified, *user_unclassified)),
        multiple_owners=_deduplicate_changes((*result.multiple_owners, *migration_multiple, *user_multiple)),
        fatal_diagnostic_codes=tuple(dict.fromkeys(fatal_codes)),
    )


__all__ = [
    "StorageComparison",
    "attribution_witness_sha256",
    "prove_candidate_ownership",
    "storage_noop_comparison",
    "storage_readback_comparison",
    "storage_readback_equivalent",
    "storage_semantically_equal",
]
