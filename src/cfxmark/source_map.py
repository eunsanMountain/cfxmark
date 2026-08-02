"""Deterministic storage-leaf provenance and semantic edit matching.

The types in this module deliberately do not depend on the Markdown AST or on
an XML implementation.  Parsers and renderers provide canonical node snapshots
and semantic storage leaves; this module proves their correspondence without
falling back to byte offsets, raw ordinals, or first-match heuristics.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Literal, cast

StorageLeafField = Literal["text", "tail", "attribute", "element", "topology"]
SourceKind = Literal["ast-node", "migration", "presentation", "protected-region"]
EditKind = Literal["insert", "delete", "update", "move"]
_MAX_ALIGNMENT_CELLS = 1_000_000
#: Recovering the pairs a tie did not touch is worth two more walks of the
#: matrix on the sibling runs real documents have, and is not worth it on the
#: pathological ones the alignment budget above merely tolerates. Past this,
#: no pair is claimed -- the same answer as before the recovery existed.
_MAX_FORCED_PAIR_CELLS = 40_000


class SourceMapError(ValueError):
    """A source map cannot prove a unique bidirectional correspondence."""

    code = "source-map-invalid"


class SemanticMappingAmbiguous(SourceMapError):
    """More than one minimum-cost semantic edit mapping exists.

    Still raised, so every caller fails closed exactly as before. What is new is
    that the exception carries the mapping that *was* proven.

    A tie is local: it is the alignment of one parent's residual children that
    has two readings, not the document. Discarding the whole walk over it threw
    away every correspondence established elsewhere, and measured across the
    Story corpus that is where the large unclassified counts come from -- 2,298
    leaves on one page, 863 on another, from a tie inside a single table.

    `partial` holds the walk's result with the tied segments confined: no
    correspondence is claimed inside them and every node in them is a delete or
    an insert, which is what an undecided alignment honestly means. A caller that
    can use less than a whole-document proof can take it; a caller that cannot
    keeps failing closed by ignoring it.

    Confinement is per pair, not per segment. A segment is only the run of
    siblings between two exact anchors, so a tie between two identical
    paragraphs used to take down every other sibling sharing that run -- a code
    macro edited in the same run lost its `macro-id` even though it pairs the
    same way in *both* readings. `forced_pairs` carries the pairs that are
    identical across every minimum-cost alignment, which are proven on exactly
    the criterion the unique case uses; the pairs that actually differ between
    readings are the ambiguity and stay unclaimed.
    """

    code = "semantic-mapping-ambiguous"

    def __init__(
        self,
        message: str,
        *,
        partial: EditMatchResult | None = None,
        forced_pairs: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.partial = partial
        self.forced_pairs = forced_pairs


@dataclass(frozen=True)
class StorageLeafRef:
    """Canonical identity for a storage value preserved by conversion."""

    semantic_path: tuple[str, ...]
    field: StorageLeafField
    attribute_name: str | None
    occurrence_ordinal: int
    value_fingerprint: str

    def __post_init__(self) -> None:
        if not self.semantic_path:
            raise SourceMapError("a storage leaf requires a semantic path")
        if self.occurrence_ordinal < 0:
            raise SourceMapError("a storage leaf occurrence ordinal cannot be negative")
        if self.field == "attribute" and not self.attribute_name:
            raise SourceMapError("an attribute leaf requires attribute_name")
        if self.field != "attribute" and self.attribute_name is not None:
            raise SourceMapError("attribute_name is only valid for an attribute leaf")
        if not self.value_fingerprint:
            raise SourceMapError("a storage leaf requires a value fingerprint")


@dataclass(frozen=True)
class SourceMapEntry:
    """Storage leaves emitted for one AST, migration, or presentation operation."""

    operation_id: str
    source_kind: SourceKind
    node_or_occurrence_id: str
    leaves: tuple[StorageLeafRef, ...]


@dataclass(frozen=True)
class EditOperation:
    """A uniquely proven base-to-edited AST operation."""

    kind: EditKind
    base_node_id: str | None
    edited_node_id: str | None
    old_parent_id: str | None
    new_parent_id: str | None
    old_ordinal: int | None
    new_ordinal: int | None
    deleted_leaf_tombstones: tuple[StorageLeafRef, ...] = ()
    relocated_leaf_tombstones: tuple[StorageLeafRef, ...] = ()
    topology_only: bool = False

    def __post_init__(self) -> None:
        if self.kind == "insert" and (self.base_node_id is not None or self.edited_node_id is None):
            raise SourceMapError("insert requires only an edited node ID")
        if self.kind == "delete" and (self.base_node_id is None or self.edited_node_id is not None):
            raise SourceMapError("delete requires only a base node ID")
        if self.kind in {"update", "move"} and (self.base_node_id is None or self.edited_node_id != self.base_node_id):
            raise SourceMapError(f"{self.kind} must retain the base stable node ID")
        if self.kind != "delete" and self.deleted_leaf_tombstones:
            raise SourceMapError("only delete operations may contain leaf tombstones")
        if self.relocated_leaf_tombstones and self.kind not in {"update", "move"}:
            raise SourceMapError("only update or move operations may contain relocation tombstones")
        if self.topology_only and self.kind != "update":
            raise SourceMapError("only update operations may be topology-only")


@dataclass(frozen=True)
class SemanticNode:
    """Canonical AST node snapshot consumed by the deterministic matcher.

    ``node_id`` is a stable ID for base nodes and a conversion-local ID for
    edited nodes.  ``subtree_fingerprint`` covers the full semantic subtree,
    including whitespace, code bytes, and formatting boundaries.
    """

    node_id: str
    node_type: str
    parent_id: str | None
    ordinal: int
    subtree_fingerprint: str
    local_fingerprint: str = ""
    topology_fingerprint: str = ""
    explicit_anchor: str | None = None
    leaves: tuple[StorageLeafRef, ...] = ()
    # True only when this node and its whole subtree carry no remote-backed
    # identity (macro-id, attachment, opaque, presentation, link target). The
    # snapshot layer computes it from an allowlist; every other producer keeps
    # the fail-closed default, so an unclassified node keeps a tied alignment
    # fatal. Not part of the ownership witness (see ownership_proof.node_document).
    payload_free: bool = False

    def __post_init__(self) -> None:
        if not self.node_id or not self.node_type or not self.subtree_fingerprint:
            raise SourceMapError("semantic nodes require ID, type, and full-subtree fingerprint")
        if self.ordinal < 0:
            raise SourceMapError("a semantic node ordinal cannot be negative")


@dataclass(frozen=True)
class EditMatchResult:
    """Unique edit proof plus local-to-stable node correspondence."""

    operations: tuple[EditOperation, ...]
    base_to_edited_source: Mapping[str, str]
    edited_source_to_stable: Mapping[str, str]
    #: Parent pairs whose residual children had more than one minimum alignment.
    #: Empty on every complete proof; non-empty only on the partial result that
    #: rides out on `SemanticMappingAmbiguous`.
    ambiguous_scopes: tuple[tuple[str | None, str | None], ...] = ()


@dataclass(frozen=True)
class _Tree:
    nodes: Mapping[str, SemanticNode]
    children: Mapping[str | None, tuple[str, ...]]
    positions: Mapping[str, tuple[int, ...]]


def fingerprint_semantic_value(value: str | bytes) -> str:
    """Return the domain-neutral SHA-256 fingerprint used by leaf identities."""

    payload = value.encode("utf-8") if isinstance(value, str) else value
    return f"sha256:{sha256(payload).hexdigest()}"


def stable_node_id(parent_stable_id: str | None, node_type: str, ordinal: int, fingerprint: str) -> str:
    """Create a deterministic local stable ID for a base or inserted node."""

    if ordinal < 0:
        raise SourceMapError("a stable node ordinal cannot be negative")
    payload = json.dumps(
        ["cfxmark-stable-node-v1", parent_stable_id, node_type, ordinal, fingerprint],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"node-{sha256(payload).hexdigest()}"


def operation_id(operation: EditOperation) -> str:
    """The deterministic ID for this operation, computed at most once for it.

    Computing it means sorting an operation's tombstone arrays and serialising
    them to JSON to hash. On a page of tables an operation can carry thousands
    of tombstones, and the same operation is asked for its ID many times over --
    measured on a live page, 85,070 calls across 53,629 operations, and that
    recomputation was the single largest cost in a 356-second preflight.

    Cached on the operation itself rather than in a module-level map keyed by
    `id()`: an object's identity is reused after it is collected, so such a map
    can hand one operation's ID to a different operation that happens to land
    at the same address. Here the cache lives and dies with the object it
    describes.

    Writing to `__dict__` directly is what makes this work on a frozen
    dataclass, and it is invisible to equality and hashing, which are generated
    from the declared fields.
    """

    cached: str | None = operation.__dict__.get("_operation_id")
    if cached is not None:
        return cached
    value = _compute_operation_id(operation)
    operation.__dict__["_operation_id"] = value
    return value


def _compute_operation_id(operation: EditOperation) -> str:
    """Create a deterministic ID for an edit operation and its tombstones."""

    tombstones = [
        [
            list(leaf.semantic_path),
            leaf.field,
            leaf.attribute_name,
            leaf.occurrence_ordinal,
            leaf.value_fingerprint,
        ]
        for leaf in sorted(operation.deleted_leaf_tombstones, key=_leaf_key)
    ]
    relocation_tombstones = [
        [
            list(leaf.semantic_path),
            leaf.field,
            leaf.attribute_name,
            leaf.occurrence_ordinal,
            leaf.value_fingerprint,
        ]
        for leaf in sorted(operation.relocated_leaf_tombstones, key=_leaf_key)
    ]
    payload = json.dumps(
        [
            "cfxmark-edit-operation-v2",
            operation.kind,
            operation.base_node_id,
            operation.edited_node_id,
            operation.old_parent_id,
            operation.new_parent_id,
            operation.old_ordinal,
            operation.new_ordinal,
            tombstones,
            relocation_tombstones,
            operation.topology_only,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"op-{sha256(payload).hexdigest()}"


def canonical_move_operation_ids(
    base_nodes: Sequence[SemanticNode],
    edited_nodes: Sequence[SemanticNode],
    operations: Sequence[EditOperation],
) -> set[str]:
    """Return the deterministic minimal basis of real move operations."""

    def longest_increasing_ids(node_ids: Sequence[str], ranks: dict[str, int]) -> set[str]:
        if not node_ids:
            return set()
        tails: list[int] = []
        tail_indices: list[int] = []
        predecessors = [-1] * len(node_ids)
        for index, node_id in enumerate(node_ids):
            rank = ranks[node_id]
            position = bisect_left(tails, rank)
            if position == len(tails):
                tails.append(rank)
                tail_indices.append(index)
            else:
                tails[position] = rank
                tail_indices[position] = index
            if position:
                predecessors[index] = tail_indices[position - 1]
        result: set[str] = set()
        cursor = tail_indices[-1]
        while cursor >= 0:
            result.add(node_ids[cursor])
            cursor = predecessors[cursor]
        return result

    before = {node.node_id: node for node in base_nodes}
    after = {node.node_id: node for node in edited_nodes}
    move_operations = {operation_id(item): item for item in operations if item.kind == "move"}
    selected = {
        owner for owner, operation in move_operations.items() if operation.old_parent_id != operation.new_parent_id
    }
    grouped: defaultdict[str | None, list[tuple[str, EditOperation]]] = defaultdict(list)
    for owner, operation in move_operations.items():
        if operation.old_parent_id == operation.new_parent_id:
            grouped[operation.old_parent_id].append((owner, operation))

    for parent_id, owner_operations in grouped.items():
        shared_children = {
            node_id
            for node_id, before_node in before.items()
            if node_id in after and before_node.parent_id == parent_id and after[node_id].parent_id == parent_id
        }
        base_order = sorted(shared_children, key=lambda node_id: before[node_id].ordinal)
        edited_order = sorted(shared_children, key=lambda node_id: after[node_id].ordinal)
        if base_order == edited_order:
            continue
        base_rank = {node_id: rank for rank, node_id in enumerate(base_order)}
        edited_position = {node_id: position for position, node_id in enumerate(edited_order)}
        movable_ids = {
            operation.base_node_id for _owner, operation in owner_operations if operation.base_node_id is not None
        }
        mandatory = [node_id for node_id in edited_order if node_id not in movable_ids]
        mandatory_points = [
            (-1, -1, ""),
            *((edited_position[node_id], base_rank[node_id], node_id) for node_id in mandatory),
            (len(edited_order), len(base_order), ""),
        ]
        if any(
            left_rank >= right_rank
            for (_left_position, left_rank, _left_id), (_right_position, right_rank, _right_id) in zip(
                mandatory_points, mandatory_points[1:]
            )
        ):
            continue
        stationary = set(mandatory)
        for (left_position, left_rank, _left_id), (right_position, right_rank, _right_id) in zip(
            mandatory_points, mandatory_points[1:]
        ):
            candidates = [
                node_id
                for node_id in edited_order[left_position + 1 : right_position]
                if left_rank < base_rank[node_id] < right_rank
            ]
            stationary.update(longest_increasing_ids(candidates, base_rank))
        canonical_moved = set(base_order) - stationary
        if not canonical_moved <= movable_ids:
            continue
        selected.update(owner for owner, operation in owner_operations if operation.base_node_id in canonical_moved)
    return selected


def build_base_source_maps(
    entries: Iterable[SourceMapEntry],
) -> tuple[dict[StorageLeafRef, str], dict[str, tuple[StorageLeafRef, ...]]]:
    """Build ``base_storage_to_node`` and its exact inverse."""

    return _build_leaf_maps(entries, identity="node")


def build_candidate_source_maps(
    entries: Iterable[SourceMapEntry],
) -> tuple[dict[str, tuple[StorageLeafRef, ...]], dict[StorageLeafRef, str]]:
    """Build ``edited_operation_to_storage`` and its exact inverse."""

    storage_to_operation, operation_to_storage = _build_leaf_maps(entries, identity="operation")
    return operation_to_storage, storage_to_operation


def match_edit_operations(
    base_nodes: Sequence[SemanticNode],
    edited_nodes: Sequence[SemanticNode],
    *,
    markdown_authoritative_tables: bool = False,
) -> EditMatchResult:
    """Prove a unique base-to-edited mapping using the normative precedence.

    Exact parent-scoped identity is resolved first, followed by unique
    order-preserving minimum edit mappings.  Remaining globally unique full
    subtrees may move.  A tied minimum, duplicate delete/insert identity, or a
    combined move-and-update is rejected instead of guessed.
    """

    base = _make_tree(base_nodes, side="base")
    edited = _make_tree(edited_nodes, side="edited")

    base_to_edited: dict[str, str] = {}
    edited_to_base: dict[str, str] = {}
    unmatched_base_roots: set[str] = set()
    unmatched_edited_roots: set[str] = set()
    parent_queue: deque[tuple[str | None, str | None]] = deque([(None, None)])
    processed_parents: set[tuple[str | None, str | None]] = set()
    ambiguous_scopes: list[tuple[tuple[str | None, str | None], str]] = []

    def match_pair(base_id: str, edited_id: str) -> None:
        previous_edited = base_to_edited.get(base_id)
        previous_base = edited_to_base.get(edited_id)
        if (previous_edited is not None and previous_edited != edited_id) or (
            previous_base is not None and previous_base != base_id
        ):
            raise SemanticMappingAmbiguous("a semantic node has more than one correspondence")
        if previous_edited is None:
            base_to_edited[base_id] = edited_id
            edited_to_base[edited_id] = base_id
            parent_queue.append((base_id, edited_id))

    def drain_parent_queue() -> None:
        while parent_queue:
            base_parent, edited_parent = parent_queue.popleft()
            pair = (base_parent, edited_parent)
            if pair in processed_parents:
                continue
            processed_parents.add(pair)
            base_children = [
                child_id for child_id in base.children.get(base_parent, ()) if child_id not in base_to_edited
            ]
            edited_children = [
                child_id for child_id in edited.children.get(edited_parent, ()) if child_id not in edited_to_base
            ]

            exact_pairs = _unique_exact_pairs(base, edited, base_children, edited_children)
            for base_id, edited_id in exact_pairs:
                match_pair(base_id, edited_id)

            exact_base = {base_id for base_id, _ in exact_pairs}
            exact_edited = {edited_id for _, edited_id in exact_pairs}
            for residual_base, residual_edited in _residual_alignment_segments(
                base_children,
                edited_children,
                exact_pairs,
                exact_base,
                exact_edited,
            ):
                try:
                    pairs, deleted, inserted = _unique_minimum_alignment(
                        base,
                        edited,
                        residual_base,
                        residual_edited,
                        pair,
                        markdown_authoritative_tables=markdown_authoritative_tables,
                    )
                except SemanticMappingAmbiguous as tie:
                    # Confined, not resolved. The walk carries on so the rest of
                    # the document keeps its proven correspondence, and the raise
                    # still happens -- once, at the end, with this scope named.
                    #
                    # Confined to the pairs that actually differ between the tied
                    # readings, not to the whole run they sit in. A segment is
                    # just the siblings between two exact anchors, so discarding
                    # all of it also discarded pairs every optimal alignment
                    # agreed on -- measurably, a macro edited beside two
                    # identical paragraphs lost the identity it still owned.
                    ambiguous_scopes.append((pair, str(tie)))
                    pairs = tie.forced_pairs
                    forced_base = {base_id for base_id, _ in pairs}
                    forced_edited = {edited_id for _, edited_id in pairs}
                    deleted = tuple(base_id for base_id in residual_base if base_id not in forced_base)
                    inserted = tuple(edited_id for edited_id in residual_edited if edited_id not in forced_edited)
                for base_id, edited_id in pairs:
                    match_pair(base_id, edited_id)
                unmatched_base_roots.update(deleted)
                unmatched_edited_roots.update(inserted)

    drain_parent_queue()

    while True:
        move_pairs = _globally_unique_move_pairs(
            base,
            edited,
            unmatched_base_roots,
            unmatched_edited_roots,
        )
        if not move_pairs:
            break
        for base_id, edited_id in move_pairs:
            unmatched_base_roots.remove(base_id)
            unmatched_edited_roots.remove(edited_id)
            match_pair(base_id, edited_id)
        drain_parent_queue()

    edited_to_stable = dict(edited_to_base)
    for root_id in sorted(unmatched_edited_roots, key=edited.positions.__getitem__):
        _assign_inserted_stable_ids(edited, root_id, edited_to_stable)

    operations = _build_operations(
        base,
        edited,
        base_to_edited,
        edited_to_stable,
        unmatched_base_roots,
        unmatched_edited_roots,
    )
    result = EditMatchResult(
        operations=operations,
        base_to_edited_source=MappingProxyType(dict(sorted(base_to_edited.items()))),
        edited_source_to_stable=MappingProxyType(dict(sorted(edited_to_stable.items()))),
        ambiguous_scopes=tuple(scope for scope, _ in dict.fromkeys(ambiguous_scopes)),
    )
    if ambiguous_scopes:
        # Raised, as it always was. The difference is that the proof it could not
        # complete travels with it instead of being thrown away.
        raise SemanticMappingAmbiguous(ambiguous_scopes[0][1], partial=result)
    return result


def _build_leaf_maps(
    entries: Iterable[SourceMapEntry], *, identity: Literal["node", "operation"]
) -> tuple[dict[StorageLeafRef, str], dict[str, tuple[StorageLeafRef, ...]]]:
    forward: dict[StorageLeafRef, str] = {}
    reverse_sets: defaultdict[str, set[StorageLeafRef]] = defaultdict(set)
    ordered_entries = sorted(
        entries,
        key=lambda entry: (entry.operation_id, entry.source_kind, entry.node_or_occurrence_id),
    )
    for entry in ordered_entries:
        identity_value = entry.node_or_occurrence_id if identity == "node" else entry.operation_id
        if not identity_value:
            raise SourceMapError(f"a source-map {identity} identity cannot be empty")
        reverse_sets[identity_value]
        if len(set(entry.leaves)) != len(entry.leaves):
            raise SourceMapError(f"source-map {identity_value!r} contains a duplicate leaf")
        for leaf in entry.leaves:
            owner = forward.get(leaf)
            if owner is not None and owner != identity_value:
                raise SourceMapError(f"storage leaf is owned by both {owner!r} and {identity_value!r}")
            forward[leaf] = identity_value
            reverse_sets[identity_value].add(leaf)

    sorted_forward = dict(sorted(forward.items(), key=lambda item: _leaf_key(item[0])))
    reverse = {
        identity_value: tuple(sorted(leaves, key=_leaf_key)) for identity_value, leaves in sorted(reverse_sets.items())
    }
    for leaf, identity_value in sorted_forward.items():
        # Against the SET, not the sorted tuple beside it. `in` on a tuple is a
        # linear scan, so this loop was quadratic in the leaves an identity owns,
        # and every comparison it made was a generated dataclass `__eq__`.
        #
        # Measured on the 79-table document under cProfile: 670,429,362 calls to
        # that `__eq__`, 45.2 s of the profile. `reverse_sets` already holds exactly
        # the same leaves -- `reverse` is built from it, one sort later -- so this is
        # the same assertion asked of the same data in constant time.
        #
        # The tuples stay: they are the public shape, ordered by `_leaf_key`, and
        # callers index into them.
        if leaf not in reverse_sets[identity_value]:
            raise AssertionError("internal source-map inverse is incomplete")
    return sorted_forward, reverse


def _make_tree(nodes: Sequence[SemanticNode], *, side: str) -> _Tree:
    node_by_id: dict[str, SemanticNode] = {}
    children: defaultdict[str | None, list[str]] = defaultdict(list)
    ordinals: set[tuple[str | None, int]] = set()
    for node in nodes:
        if node.node_id in node_by_id:
            raise SourceMapError(f"duplicate {side} semantic node ID {node.node_id!r}")
        ordinal_key = (node.parent_id, node.ordinal)
        if ordinal_key in ordinals:
            raise SourceMapError(f"duplicate {side} sibling ordinal {node.ordinal}")
        ordinals.add(ordinal_key)
        node_by_id[node.node_id] = node
        children[node.parent_id].append(node.node_id)
    for node in nodes:
        if node.parent_id is not None and node.parent_id not in node_by_id:
            raise SourceMapError(f"{side} node {node.node_id!r} has an unknown parent")
    sorted_children = {
        parent_id: tuple(sorted(child_ids, key=lambda child_id: node_by_id[child_id].ordinal))
        for parent_id, child_ids in children.items()
    }
    positions: dict[str, tuple[int, ...]] = {}

    def visit(node_id: str, prefix: tuple[int, ...], active: set[str]) -> None:
        if node_id in active:
            raise SourceMapError(f"{side} semantic node graph contains a cycle")
        active.add(node_id)
        node = node_by_id[node_id]
        position = (*prefix, node.ordinal)
        positions[node_id] = position
        for child_id in sorted_children.get(node_id, ()):
            visit(child_id, position, active)
        active.remove(node_id)

    for root_id in sorted_children.get(None, ()):
        visit(root_id, (), set())
    if len(positions) != len(node_by_id):
        raise SourceMapError(f"{side} semantic node graph is disconnected")
    return _Tree(
        nodes=MappingProxyType(node_by_id),
        children=MappingProxyType(sorted_children),
        positions=MappingProxyType(positions),
    )


def _unique_exact_pairs(
    base: _Tree,
    edited: _Tree,
    base_ids: Sequence[str],
    edited_ids: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    remaining_base = set(base_ids)
    remaining_edited = set(edited_ids)

    for key_kind in ("anchor", "subtree"):
        base_keys = {
            node_id: _exact_key(base.nodes[node_id], key_kind)
            for node_id in base_ids
            if node_id in remaining_base and _exact_key(base.nodes[node_id], key_kind) is not None
        }
        edited_keys = {
            node_id: _exact_key(edited.nodes[node_id], key_kind)
            for node_id in edited_ids
            if node_id in remaining_edited and _exact_key(edited.nodes[node_id], key_kind) is not None
        }
        base_counts = Counter(base_keys.values())
        edited_counts = Counter(edited_keys.values())
        edited_by_key = {key: node_id for node_id, key in edited_keys.items() if edited_counts[key] == 1}
        for base_id in base_ids:
            key = base_keys.get(base_id)
            if key is None or base_counts[key] != 1 or edited_counts[key] != 1:
                continue
            edited_id = edited_by_key[key]
            pairs.append((base_id, edited_id))
            remaining_base.remove(base_id)
            remaining_edited.remove(edited_id)
    return tuple(pairs)


def _exact_key(node: SemanticNode, kind: str) -> tuple[str, ...] | None:
    if kind == "anchor":
        if node.explicit_anchor is None:
            return None
        return (node.node_type, node.topology_fingerprint, node.explicit_anchor)
    return (node.node_type, node.topology_fingerprint, node.subtree_fingerprint)


def _residual_alignment_segments(
    base_ids: Sequence[str],
    edited_ids: Sequence[str],
    exact_pairs: Sequence[tuple[str, str]],
    exact_base: set[str],
    exact_edited: set[str],
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Partition residual alignment by monotonic exact sibling anchors.

    Removing exact matches and aligning every remaining sibling at once loses
    their positional evidence.  A changed block before an unchanged anchor and
    an insertion after the last anchor can then form an artificial minimum-cost
    tie.  Monotonic exact matches are immutable interval boundaries.  Crossing
    matches are deliberately not used as boundaries because they may represent
    moves; those retain the conservative whole-residual alignment.
    """

    if not exact_pairs:
        return ((tuple(base_ids), tuple(edited_ids)),)
    base_positions = {node_id: index for index, node_id in enumerate(base_ids)}
    edited_positions = {node_id: index for index, node_id in enumerate(edited_ids)}
    anchors = sorted(exact_pairs, key=lambda pair: base_positions[pair[0]])
    edited_anchor_positions = [edited_positions[edited_id] for _, edited_id in anchors]
    if edited_anchor_positions != sorted(edited_anchor_positions):
        return (
            (
                tuple(node_id for node_id in base_ids if node_id not in exact_base),
                tuple(node_id for node_id in edited_ids if node_id not in exact_edited),
            ),
        )

    segments: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    previous_base = -1
    previous_edited = -1
    for base_id, edited_id in anchors:
        base_position = base_positions[base_id]
        edited_position = edited_positions[edited_id]
        segments.append(
            (
                tuple(base_ids[previous_base + 1 : base_position]),
                tuple(edited_ids[previous_edited + 1 : edited_position]),
            )
        )
        previous_base = base_position
        previous_edited = edited_position
    segments.append((tuple(base_ids[previous_base + 1 :]), tuple(edited_ids[previous_edited + 1 :])))
    base_only_segments = {index for index, (left, right) in enumerate(segments) if left and not right}
    edited_only_segments = {index for index, (left, right) in enumerate(segments) if right and not left}
    if any(left_index != right_index for left_index in base_only_segments for right_index in edited_only_segments):
        return (
            (
                tuple(node_id for node_id in base_ids if node_id not in exact_base),
                tuple(node_id for node_id in edited_ids if node_id not in exact_edited),
            ),
        )
    return tuple(segments)


def _tie_is_ownership_neutral(
    base: _Tree,
    edited: _Tree,
    base_ids: Sequence[str],
    edited_ids: Sequence[str],
    *,
    markdown_authoritative_tables: bool = False,
    parent_pair: tuple[str | None, str | None] = (None, None),
) -> bool:
    """Whether a tied minimum alignment can be resolved without guessing.

    Both conditions must hold:

    * Every node on both sides is payload-free -- carries no remote-backed
      identity (macro-id, attachment, opaque payload, presentation, link
      target). A delete+insert would strand such identity, so it stays fatal.
    * No pair of nodes matches at zero edit cost. A zero-cost survivor means an
      identical value exists on both sides and the tie is about *which*
      duplicate it belongs to -- for example deleting one of two identical
      siblings -- which changes tombstone provenance and must stay fatal. This
      mirrors the DP's ``update_cost == 0`` condition exactly.

    Without an exact survivor and with only payload-free nodes, the ambiguity is
    only the order of unrelated delete/insert operations: the final storage and
    its ownership are the same for every minimum script.

    Both gates are evaluated over only the *correspondence-eligible* nodes -- the
    ones whose ``node_type`` also appears on the opposite side. The alignment DP
    offers a ``pair`` transition only between equal node types (see
    ``_unique_minimum_alignment``), so a node whose type is absent on the
    opposite side can never be a tie participant: it is a forced delete/insert in
    *every* minimum-cost script, identical across all tied scripts, and
    contributes nothing to the ambiguity. Dragging such a forced payload-bearing
    insert/delete into the payload-free gate would wrongly turn a legitimate
    paragraph tie fatal. Restricting the gate to eligible nodes is exactly the
    contrapositive of the necessary correspondence condition, so it exempts only
    provable non-participants and stays fail-closed for everything else (a
    same-type node is kept even when it is not the true correspondent). The
    forced remainder is classified through the normal delete/insert path, so the
    caller still returns the FULL id tuples (nothing is dropped).
    """

    base_types = {base.nodes[base_id].node_type for base_id in base_ids}
    edited_types = {edited.nodes[edited_id].node_type for edited_id in edited_ids}
    tie_base_ids = [base_id for base_id in base_ids if base.nodes[base_id].node_type in edited_types]
    tie_edited_ids = [edited_id for edited_id in edited_ids if edited.nodes[edited_id].node_type in base_types]

    if not all(base.nodes[base_id].payload_free for base_id in tie_base_ids):
        return False
    if not all(edited.nodes[edited_id].payload_free for edited_id in tie_edited_ids):
        return False
    exact_table_scope = markdown_authoritative_tables and _table_tie_scope(base, edited, parent_pair)
    for base_id in tie_base_ids:
        base_node = base.nodes[base_id]
        base_key = _semantic_equality_key(base_node)
        for edited_id in tie_edited_ids:
            edited_node = edited.nodes[edited_id]
            if edited_node.node_type == base_node.node_type and _semantic_equality_key(edited_node) == base_key:
                if not exact_table_scope:
                    return False
    return True


def _table_tie_scope(
    base: _Tree,
    edited: _Tree,
    parent_pair: tuple[str | None, str | None],
) -> bool:
    """Whether a tied sibling run is inside a Markdown-authoritative table."""

    base_parent, edited_parent = parent_pair
    if base_parent is None or edited_parent is None:
        return False
    return (
        base.nodes[base_parent].node_type in {"Table", "TableRow"}
        and edited.nodes[edited_parent].node_type in {"Table", "TableRow"}
        and base.nodes[base_parent].payload_free
        and edited.nodes[edited_parent].payload_free
    )


def _forced_pairs_across_optimal_alignments(
    base: _Tree,
    edited: _Tree,
    base_ids: Sequence[str],
    edited_ids: Sequence[str],
    costs: Sequence[Sequence[int]],
) -> tuple[tuple[str, str], ...]:
    """Pairs that are identical in *every* minimum-cost alignment of this run.

    A tie means the optimal alignments disagree, not that they disagree
    everywhere. Two identical paragraphs may swap between readings while the
    macro beside them pairs the same way in all of them; that macro's
    correspondence is proven on the very criterion the unique case uses, and
    throwing it away is what stripped identities off untouched nodes.

    Every complete alignment path runs from (0,0) to (n,m), so it consumes each
    base row exactly once. Collect the transitions that consume row `i` and lie
    on *some* optimal path -- a transition is on one when the best cost through
    it equals the best cost overall. If all of them are the same pairing, then
    every optimal alignment makes that pairing, and no counting is needed.
    Anything else stays unclaimed, which is what an undecided alignment means.

    Bounded by its own budget, well under the alignment budget that let the run
    get this far. This walks the matrix twice more, and on a page whose tie
    spans hundreds of siblings that is real time spent to recover identities
    for a publish that is going to be refused anyway. Returning nothing is not
    a wrong answer here -- it is precisely the behaviour before this existed, so
    the fallback is conservative rather than degraded.
    """

    rows = len(base_ids) + 1
    columns = len(edited_ids) + 1
    if rows * columns > _MAX_FORCED_PAIR_CELLS:
        return ()

    # Best cost from each cell to the end, the mirror of `costs`.
    suffix = [[0] * columns for _ in range(rows)]
    for base_index in range(rows - 2, -1, -1):
        suffix[base_index][columns - 1] = suffix[base_index + 1][columns - 1] + 1
    for edited_index in range(columns - 2, -1, -1):
        suffix[rows - 1][edited_index] = suffix[rows - 1][edited_index + 1] + 1
    for base_index in range(rows - 2, -1, -1):
        for edited_index in range(columns - 2, -1, -1):
            best = min(suffix[base_index + 1][edited_index] + 1, suffix[base_index][edited_index + 1] + 1)
            base_node = base.nodes[base_ids[base_index]]
            edited_node = edited.nodes[edited_ids[edited_index]]
            if base_node.node_type == edited_node.node_type:
                update_cost = 0 if _semantic_equality_key(base_node) == _semantic_equality_key(edited_node) else 1
                best = min(best, suffix[base_index + 1][edited_index + 1] + update_cost)
            suffix[base_index][edited_index] = best

    total = costs[0][0] + suffix[0][0]
    forced: list[tuple[str, str]] = []
    for base_index in range(1, rows):
        # Every optimal path consumes base row `base_index` exactly once, either
        # by deleting it or by pairing it with one edited column.
        consumers: set[int | None] = set()
        for edited_index in range(columns):
            if costs[base_index - 1][edited_index] + 1 + suffix[base_index][edited_index] == total:
                consumers.add(None)  # deleted on some optimal path
            if edited_index == 0:
                continue
            base_node = base.nodes[base_ids[base_index - 1]]
            edited_node = edited.nodes[edited_ids[edited_index - 1]]
            if base_node.node_type != edited_node.node_type:
                continue
            update_cost = 0 if _semantic_equality_key(base_node) == _semantic_equality_key(edited_node) else 1
            through = costs[base_index - 1][edited_index - 1] + update_cost + suffix[base_index][edited_index]
            if through == total:
                consumers.add(edited_index)
            if len(consumers) > 1:
                break
        if len(consumers) == 1:
            only = next(iter(consumers))
            if only is not None:
                forced.append((base_ids[base_index - 1], edited_ids[only - 1]))
    return tuple(forced)


def _unique_minimum_alignment(
    base: _Tree,
    edited: _Tree,
    base_ids: Sequence[str],
    edited_ids: Sequence[str],
    parent_pair: tuple[str | None, str | None],
    *,
    markdown_authoritative_tables: bool = False,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[str, ...]]:
    if not any(
        base.nodes[base_id].node_type == edited.nodes[edited_id].node_type
        for base_id in base_ids
        for edited_id in edited_ids
    ):
        # Delete/insert ordering is not an ownership ambiguity when no nodes can
        # correspond. Every minimum script proves the same empty matching and
        # the same topology sets, so retain that unique semantic result.
        return (), tuple(base_ids), tuple(edited_ids)
    rows = len(base_ids) + 1
    columns = len(edited_ids) + 1
    cell_count = rows * columns
    if cell_count > _MAX_ALIGNMENT_CELLS:
        raise SemanticMappingAmbiguous(
            f"semantic alignment safety budget exceeded under parent pair {parent_pair!r}: "
            f"{cell_count} cells > {_MAX_ALIGNMENT_CELLS}"
        )
    costs = [[0] * columns for _ in range(rows)]
    counts = [[0] * columns for _ in range(rows)]
    counts[0][0] = 1
    for base_index in range(1, rows):
        costs[base_index][0] = base_index
        counts[base_index][0] = 1
    for edited_index in range(1, columns):
        costs[0][edited_index] = edited_index
        counts[0][edited_index] = 1

    for base_index in range(1, rows):
        for edited_index in range(1, columns):
            choices = [
                (costs[base_index - 1][edited_index] + 1, counts[base_index - 1][edited_index]),
                (costs[base_index][edited_index - 1] + 1, counts[base_index][edited_index - 1]),
            ]
            base_node = base.nodes[base_ids[base_index - 1]]
            edited_node = edited.nodes[edited_ids[edited_index - 1]]
            if base_node.node_type == edited_node.node_type:
                update_cost = 0 if _semantic_equality_key(base_node) == _semantic_equality_key(edited_node) else 1
                choices.append(
                    (
                        costs[base_index - 1][edited_index - 1] + update_cost,
                        counts[base_index - 1][edited_index - 1],
                    )
                )
            minimum = min(cost for cost, _ in choices)
            costs[base_index][edited_index] = minimum
            counts[base_index][edited_index] = min(2, sum(count for cost, count in choices if cost == minimum))

    if counts[-1][-1] != 1:
        if _tie_is_ownership_neutral(
            base,
            edited,
            base_ids,
            edited_ids,
            markdown_authoritative_tables=markdown_authoritative_tables,
            parent_pair=parent_pair,
        ):
            # A payload-free run substitution with no exact survivor: every
            # minimum script yields byte-identical storage and no identity is
            # carried, so the tie is only about the order of unrelated
            # delete/insert operations. Record the unique delete-all/insert-all
            # matching (same form as the no-correspondence early return above)
            # instead of failing closed.
            #
            # Return the FULL base/edited id tuples, never the
            # correspondence-eligible subset that _tie_is_ownership_neutral gated
            # over: a payload-bearing node exempted from the neutrality gate is a
            # forced delete/insert that must still be emitted to _build_operations
            # as a delete/insert. Dropping it here would be silent data loss.
            return (), tuple(base_ids), tuple(edited_ids)
        raise SemanticMappingAmbiguous(
            f"minimum semantic edit mapping is tied under parent pair {parent_pair!r}",
            forced_pairs=_forced_pairs_across_optimal_alignments(base, edited, base_ids, edited_ids, costs),
        )

    pairs: list[tuple[str, str]] = []
    deleted: list[str] = []
    inserted: list[str] = []
    base_index = len(base_ids)
    edited_index = len(edited_ids)
    while base_index or edited_index:
        transitions: list[tuple[str, int, int]] = []
        current_cost = costs[base_index][edited_index]
        if base_index and costs[base_index - 1][edited_index] + 1 == current_cost:
            transitions.extend([("delete", base_index - 1, edited_index)] * counts[base_index - 1][edited_index])
        if edited_index and costs[base_index][edited_index - 1] + 1 == current_cost:
            transitions.extend([("insert", base_index, edited_index - 1)] * counts[base_index][edited_index - 1])
        if base_index and edited_index:
            base_node = base.nodes[base_ids[base_index - 1]]
            edited_node = edited.nodes[edited_ids[edited_index - 1]]
            if base_node.node_type == edited_node.node_type:
                update_cost = 0 if _semantic_equality_key(base_node) == _semantic_equality_key(edited_node) else 1
                if costs[base_index - 1][edited_index - 1] + update_cost == current_cost:
                    transitions.extend(
                        [("pair", base_index - 1, edited_index - 1)] * counts[base_index - 1][edited_index - 1]
                    )
        if len(transitions) != 1:
            raise SemanticMappingAmbiguous(
                f"minimum semantic edit mapping cannot be reconstructed under parent pair {parent_pair!r}"
            )
        action, next_base, next_edited = transitions[0]
        if action == "pair":
            pairs.append((base_ids[base_index - 1], edited_ids[edited_index - 1]))
        elif action == "delete":
            deleted.append(base_ids[base_index - 1])
        else:
            inserted.append(edited_ids[edited_index - 1])
        base_index, edited_index = next_base, next_edited
    pairs.reverse()
    deleted.reverse()
    inserted.reverse()
    return tuple(pairs), tuple(deleted), tuple(inserted)


def _globally_unique_move_pairs(
    base: _Tree,
    edited: _Tree,
    base_ids: set[str],
    edited_ids: set[str],
) -> tuple[tuple[str, str], ...]:
    base_keys = {node_id: _move_key(base.nodes[node_id]) for node_id in base_ids}
    edited_keys = {node_id: _move_key(edited.nodes[node_id]) for node_id in edited_ids}
    base_counts = Counter(base_keys.values())
    edited_counts = Counter(edited_keys.values())
    edited_by_key = {key: node_id for node_id, key in edited_keys.items() if edited_counts[key] == 1}
    pairs = [
        (base_id, edited_by_key[key])
        for base_id, key in base_keys.items()
        if base_counts[key] == 1 and edited_counts[key] == 1
    ]
    return tuple(sorted(pairs, key=lambda pair: base.positions[pair[0]]))


def _move_key(node: SemanticNode) -> tuple[str, str, str]:
    return (node.node_type, node.topology_fingerprint, node.subtree_fingerprint)


def _assign_inserted_stable_ids(tree: _Tree, root_id: str, edited_to_stable: dict[str, str]) -> None:
    node = tree.nodes[root_id]
    parent_stable_id = edited_to_stable.get(node.parent_id) if node.parent_id is not None else None
    if node.parent_id is not None and parent_stable_id is None:
        raise SourceMapError("an inserted node parent has no stable identity")
    edited_to_stable[root_id] = stable_node_id(
        parent_stable_id,
        node.node_type,
        node.ordinal,
        node.subtree_fingerprint,
    )
    for child_id in tree.children.get(root_id, ()):
        _assign_inserted_stable_ids(tree, child_id, edited_to_stable)


def _build_operations(
    base: _Tree,
    edited: _Tree,
    base_to_edited: Mapping[str, str],
    edited_to_stable: Mapping[str, str],
    deleted_roots: set[str],
    inserted_roots: set[str],
) -> tuple[EditOperation, ...]:
    operations: list[EditOperation] = []
    moved_ids = _moved_base_ids(base, edited, base_to_edited)
    for base_id, edited_id in base_to_edited.items():
        base_node = base.nodes[base_id]
        edited_node = edited.nodes[edited_id]
        base_local = base_node.local_fingerprint or base_node.subtree_fingerprint
        edited_local = edited_node.local_fingerprint or edited_node.subtree_fingerprint
        local_updated = base_local != edited_local
        base_children = base.children.get(base_id, ())
        edited_child_sources = edited.children.get(edited_id, ())
        edited_children = tuple(edited_to_stable[child_id] for child_id in edited_child_sources)
        topology_updated = base_children != edited_children and (
            any(child_id in deleted_roots for child_id in base_children)
            or any(child_id in inserted_roots for child_id in edited_child_sources)
        )
        updated = local_updated or topology_updated
        moved = base_id in moved_ids
        if updated and moved:
            raise SemanticMappingAmbiguous(f"node {base_id!r} is both moved and updated")
        if not updated and not moved:
            continue
        old_parent = base_node.parent_id
        new_parent = edited_to_stable.get(edited_node.parent_id) if edited_node.parent_id is not None else None
        relocated_leaves: tuple[StorageLeafRef, ...] = ()
        if moved:
            relocated_leaves = _subtree_leaves(base, base_id)
        elif topology_updated:
            relocated: list[StorageLeafRef] = []
            for child_id in base_children:
                child_edited_id = base_to_edited.get(child_id)
                if child_edited_id is None:
                    continue
                child = base.nodes[child_id]
                edited_child = edited.nodes[child_edited_id]
                edited_parent = (
                    edited_to_stable.get(edited_child.parent_id) if edited_child.parent_id is not None else None
                )
                if child.parent_id != edited_parent or child.ordinal != edited_child.ordinal:
                    relocated.extend(_subtree_leaves(base, child_id))
            relocated_leaves = tuple(sorted(set(relocated), key=_leaf_key))
        operations.append(
            EditOperation(
                "move" if moved else "update",
                base_id,
                base_id,
                old_parent,
                new_parent,
                base_node.ordinal,
                edited_node.ordinal,
                relocated_leaf_tombstones=relocated_leaves,
                topology_only=topology_updated and not local_updated,
            )
        )
    for base_id in deleted_roots:
        node = base.nodes[base_id]
        operations.append(
            EditOperation(
                "delete",
                base_id,
                None,
                node.parent_id,
                None,
                node.ordinal,
                None,
                _subtree_leaves(base, base_id),
            )
        )
    for edited_id in inserted_roots:
        node = edited.nodes[edited_id]
        new_parent = edited_to_stable.get(node.parent_id) if node.parent_id is not None else None
        operations.append(
            EditOperation(
                "insert",
                None,
                edited_to_stable[edited_id],
                None,
                new_parent,
                None,
                node.ordinal,
            )
        )
    stable_edited_nodes = tuple(
        replace(
            node,
            node_id=edited_to_stable[node.node_id],
            parent_id=edited_to_stable[node.parent_id] if node.parent_id is not None else None,
        )
        for node in edited.nodes.values()
    )
    canonical_move_ids = canonical_move_operation_ids(tuple(base.nodes.values()), stable_edited_nodes, operations)
    canonical_move_nodes = {
        operation.base_node_id
        for operation in operations
        if operation_id(operation) in canonical_move_ids and operation.base_node_id is not None
    }
    move_groups: defaultdict[str | None, list[EditOperation]] = defaultdict(list)
    for operation in operations:
        if operation.kind == "move" and operation.old_parent_id == operation.new_parent_id:
            move_groups[operation.old_parent_id].append(operation)
    relocation_basis_by_node: dict[str, tuple[StorageLeafRef, ...]] = {}
    for group in move_groups.values():
        canonical_group = [operation for operation in group if operation.base_node_id in canonical_move_nodes]
        if len(canonical_group) != 1:
            continue
        relocation_basis_by_node[cast(str, canonical_group[0].base_node_id)] = tuple(
            sorted(
                {leaf for operation in group for leaf in operation.relocated_leaf_tombstones},
                key=_leaf_key,
            )
        )
    operations = [
        replace(
            operation,
            relocated_leaf_tombstones=relocation_basis_by_node[operation.base_node_id],
        )
        if operation.base_node_id in relocation_basis_by_node
        else operation
        for operation in operations
    ]
    stable_to_edited_source = {stable_id: source_id for source_id, stable_id in edited_to_stable.items()}
    return tuple(
        sorted(
            operations,
            key=lambda item: _operation_sort_key(item, base, edited, stable_to_edited_source),
        )
    )


def _moved_base_ids(base: _Tree, edited: _Tree, base_to_edited: Mapping[str, str]) -> set[str]:
    moved: set[str] = set()
    for base_id, edited_id in base_to_edited.items():
        base_parent = base.nodes[base_id].parent_id
        edited_parent = edited.nodes[edited_id].parent_id
        expected_edited_parent = base_to_edited.get(base_parent) if base_parent is not None else None
        if edited_parent != expected_edited_parent:
            moved.add(base_id)

    grouped: defaultdict[tuple[str | None, str | None], list[tuple[str, str]]] = defaultdict(list)
    for base_id, edited_id in base_to_edited.items():
        base_parent = base.nodes[base_id].parent_id
        edited_parent = edited.nodes[edited_id].parent_id
        expected_edited_parent = base_to_edited.get(base_parent) if base_parent is not None else None
        if edited_parent == expected_edited_parent:
            grouped[(base_parent, edited_parent)].append((base_id, edited_id))
    for pairs in grouped.values():
        base_order = [base_id for base_id, _ in sorted(pairs, key=lambda pair: base.nodes[pair[0]].ordinal)]
        edited_order = [base_id for base_id, _ in sorted(pairs, key=lambda pair: edited.nodes[pair[1]].ordinal)]
        if base_order != edited_order:
            base_rank = {node_id: rank for rank, node_id in enumerate(base_order)}
            edited_rank = {node_id: rank for rank, node_id in enumerate(edited_order)}
            moved.update(node_id for node_id in base_order if base_rank[node_id] != edited_rank[node_id])
    return moved


def _subtree_leaves(tree: _Tree, root_id: str) -> tuple[StorageLeafRef, ...]:
    leaves: list[StorageLeafRef] = []
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        leaves.extend(tree.nodes[node_id].leaves)
        stack.extend(reversed(tree.children.get(node_id, ())))
    if len(leaves) != len(set(leaves)):
        raise SourceMapError(f"deleted subtree {root_id!r} contains duplicate leaf identities")
    return tuple(sorted(leaves, key=_leaf_key))


def _semantic_equality_key(node: SemanticNode) -> tuple[str, str, str, str | None]:
    return (node.node_type, node.topology_fingerprint, node.subtree_fingerprint, node.explicit_anchor)


def _leaf_key(leaf: StorageLeafRef) -> tuple[tuple[str, ...], str, str, int, str]:
    return (
        leaf.semantic_path,
        leaf.field,
        leaf.attribute_name or "",
        leaf.occurrence_ordinal,
        leaf.value_fingerprint,
    )


def _operation_sort_key(
    operation: EditOperation,
    base: _Tree,
    edited: _Tree,
    stable_to_edited_source: Mapping[str, str],
) -> tuple[int, tuple[int, ...], str]:
    if operation.base_node_id is not None:
        position = base.positions[operation.base_node_id]
        side = 0
    else:
        if operation.edited_node_id is None:
            raise AssertionError("insert operation is missing its edited stable node ID")
        edited_source_id = stable_to_edited_source[operation.edited_node_id]
        position = edited.positions[edited_source_id]
        side = 1
    return (side, position, operation.kind)


__all__ = [
    "EditMatchResult",
    "EditOperation",
    "SemanticMappingAmbiguous",
    "SemanticNode",
    "SourceMapEntry",
    "SourceMapError",
    "StorageLeafRef",
    "build_base_source_maps",
    "build_candidate_source_maps",
    "canonical_move_operation_ids",
    "fingerprint_semantic_value",
    "match_edit_operations",
    "operation_id",
    "stable_node_id",
]
