"""Fresh-remote subtree reuse for managed Markdown candidates."""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from dataclasses import replace
from typing import Any

import lxml.etree as ET

from cfxmark.artifacts import MarkdownRange, RemoteSubtreeArtifact, RemoteSubtreeProof
from cfxmark.ast import Document
from cfxmark.normalize import _serialize_children_exact, preservation_signature
from cfxmark.parsers.cfx import _parse_fragment_to_element, _serialize_element
from cfxmark.renderers.cfx import render_cfx
from cfxmark.renderers.md import MarkdownRenderOptions, render_md
from cfxmark.source_map import EditOperation, SemanticNode, SourceKind, SourceMapEntry, StorageLeafRef
from cfxmark.storage_map import extract_storage_source, storage_leaf_identity
from cfxmark.xml_ns import storage_node_name


def build_remote_subtrees(
    document: Document,
    markdown: str,
    semantic_nodes: tuple[SemanticNode, ...],
    source_xhtml: str,
    *,
    md_options: MarkdownRenderOptions,
    registry: Any | None = None,
) -> tuple[RemoteSubtreeArtifact, ...]:
    """Bind every source-backed top-level semantic block to its Markdown range."""

    top_nodes = _top_level_nodes(semantic_nodes)
    if len(top_nodes) != len(document.children):
        return ()
    source_root = _parse_fragment_to_element(source_xhtml)
    cursor = 0
    result: list[RemoteSubtreeArtifact] = []
    for block, semantic_node in zip(document.children, top_nodes, strict=True):
        if not block.source_path:
            continue
        projection = render_md(Document(children=(block,)), options=md_options).rstrip("\n")
        if not projection:
            continue
        start = markdown.find(projection, cursor)
        if start < 0:
            return ()
        end = start + len(projection)
        cursor = end
        element = _element_at_path(source_root, block.source_path)
        if element is None:
            return ()
        raw = _serialize_element(element)
        canonical, _attachments, _warnings = render_cfx(Document(children=(block,)), registry=registry)
        result.append(
            RemoteSubtreeArtifact(
                node_id=semantic_node.node_id,
                remote_node_path=block.source_path,
                base_markdown_range=MarkdownRange(start=start, end=end),
                projection_fingerprint=_sha256(projection),
                remote_subtree_fingerprint=_sha256(raw),
                requires_fresh_source=(
                    preservation_signature(raw) != preservation_signature(canonical)
                    or projection.rstrip(" \t") != projection
                ),
            )
        )
    return tuple(result)


def claim_base_remote_leaves(
    semantic_nodes: tuple[SemanticNode, ...],
    storage_to_node: dict[StorageLeafRef, str],
    node_to_storage: dict[str, tuple[StorageLeafRef, ...]],
    source_map_entries: tuple[SourceMapEntry, ...],
    unmapped: tuple[StorageLeafRef, ...],
    remote_subtrees: tuple[RemoteSubtreeArtifact, ...],
) -> tuple[
    tuple[SemanticNode, ...],
    dict[StorageLeafRef, str],
    dict[str, tuple[StorageLeafRef, ...]],
    tuple[SourceMapEntry, ...],
    tuple[StorageLeafRef, ...],
]:
    """Treat otherwise-unmapped leaves inside a reusable subtree as remote-owned."""

    if not remote_subtrees or not unmapped:
        return semantic_nodes, storage_to_node, node_to_storage, source_map_entries, unmapped
    document_id = next(node.node_id for node in semantic_nodes if node.parent_id is None)
    owner_by_path = tuple((item.remote_node_path, item.node_id) for item in remote_subtrees)
    owner_index, owner_prefix_lengths = _remote_owner_index(owner_by_path)
    claimed: dict[str, list[StorageLeafRef]] = defaultdict(list)
    remaining: list[StorageLeafRef] = []
    for leaf in unmapped:
        owner = _first_remote_owner(leaf, owner_index, owner_prefix_lengths)
        if owner is None and leaf.semantic_path == ("#fragment[0]",) and leaf.field == "topology":
            owner = document_id
        if owner is None:
            remaining.append(leaf)
            continue
        claimed[owner].append(leaf)
        storage_to_node[leaf] = owner
    if not claimed:
        return semantic_nodes, storage_to_node, node_to_storage, source_map_entries, unmapped

    revised_entries = list(source_map_entries)
    for owner, leaves in claimed.items():
        merged = tuple(sorted((*node_to_storage.get(owner, ()), *leaves), key=_leaf_sort_key))
        node_to_storage[owner] = merged
        entry_index = next(
            (
                index
                for index, entry in enumerate(revised_entries)
                if entry.source_kind == "ast-node" and entry.node_or_occurrence_id == owner
            ),
            None,
        )
        current = revised_entries[entry_index] if entry_index is not None else None
        revised = SourceMapEntry(
            operation_id=current.operation_id if current is not None else owner,
            source_kind="ast-node",
            node_or_occurrence_id=owner,
            leaves=merged,
        )
        if entry_index is None:
            revised_entries.append(revised)
        else:
            revised_entries[entry_index] = revised
    revised_nodes = tuple(replace(node, leaves=node_to_storage.get(node.node_id, ())) for node in semantic_nodes)
    return (
        revised_nodes,
        storage_to_node,
        node_to_storage,
        tuple(sorted(revised_entries, key=lambda item: item.operation_id)),
        tuple(remaining),
    )


def apply_remote_subtree_splice(
    candidate_xhtml: str,
    *,
    splice_source: str,
    remote_subtrees: tuple[RemoteSubtreeArtifact, ...],
    base_nodes: tuple[SemanticNode, ...],
    candidate_nodes: tuple[SemanticNode, ...],
    edit_operations: tuple[EditOperation, ...],
    source_map_entries: tuple[SourceMapEntry, ...],
) -> tuple[
    str,
    tuple[SourceMapEntry, ...],
    tuple[StorageLeafRef, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[RemoteSubtreeProof, ...],
]:
    """Replace uniquely matched untouched or moved top-level blocks with fresh remote values."""

    if not remote_subtrees:
        return candidate_xhtml, source_map_entries, (), (), (), (), ()
    reusable = tuple(
        item
        for item in remote_subtrees
        if _subtree_reusable(item.node_id, base_nodes, candidate_nodes, edit_operations)
    )
    if not reusable:
        return candidate_xhtml, source_map_entries, (), (), (), (), ()
    required = tuple(item.node_id for item in reusable)
    if len(set(required)) != len(required):
        return candidate_xhtml, source_map_entries, (), (), required, required, ()

    source_root = _parse_fragment_to_element(splice_source)
    candidate_root = _parse_fragment_to_element(candidate_xhtml)
    original_paths = _semantic_element_paths(candidate_root)
    candidate_paths = _candidate_paths(source_map_entries)
    planned: list[tuple[RemoteSubtreeArtifact, ET._Element, ET._Element, tuple[str, ...]]] = []
    for artifact in reusable:
        source_element = _element_at_path(source_root, artifact.remote_node_path)
        target_path = candidate_paths.get(artifact.node_id)
        target_element = _element_at_path(candidate_root, target_path) if target_path is not None else None
        if source_element is None or target_element is None or target_element.getparent() is None:
            continue
        source_raw = _serialize_element(source_element)
        if _sha256(source_raw) != artifact.remote_subtree_fingerprint:
            continue
        assert target_path is not None
        planned.append((artifact, source_element, target_element, target_path))

    replacements: list[tuple[RemoteSubtreeArtifact, ET._Element]] = []
    for artifact, source_element, target_element, target_path in planned:
        parent = target_element.getparent()
        if parent is None:
            continue
        index = parent.index(target_element)
        replacement = copy.deepcopy(source_element)
        replacement.tail = source_element.tail if target_path == artifact.remote_node_path else target_element.tail
        parent.remove(target_element)
        parent.insert(index, replacement)
        replacements.append((artifact, replacement))
    if not replacements:
        return candidate_xhtml, source_map_entries, (), (), required, required, ()

    spliced = _serialize_children_exact(candidate_root)
    actual = extract_storage_source(spliced)
    current_paths = {element: path for path, element in _semantic_element_paths(candidate_root)}
    preserved_paths = tuple(
        (current_paths[element], artifact.node_id, artifact.remote_subtree_fingerprint)
        for artifact, element in replacements
    )
    preserved_index, preserved_prefix_lengths = _remote_owner_index(
        tuple((path, owner) for path, owner, _fingerprint in preserved_paths)
    )
    existing_by_identity = {storage_leaf_identity(leaf): entry for entry in source_map_entries for leaf in entry.leaves}
    source_entries_by_prefix: defaultdict[tuple[str, ...], list[tuple[StorageLeafRef, SourceMapEntry]]] = defaultdict(
        list
    )
    for entry in source_map_entries:
        for leaf in entry.leaves:
            for length in range(1, len(leaf.semantic_path) + 1):
                source_entries_by_prefix[leaf.semantic_path[:length]].append((leaf, entry))
    rebased_by_identity: dict[tuple[tuple[str, ...], str, str | None, int], list[SourceMapEntry]] = defaultdict(list)
    for old_path, element in original_paths:
        new_path = current_paths.get(element)
        if new_path is None:
            continue
        for leaf, entry in source_entries_by_prefix.get(old_path, ()):
            rebased_identity = (
                new_path + leaf.semantic_path[len(old_path) :],
                leaf.field,
                leaf.attribute_name,
                leaf.occurrence_ordinal,
            )
            rebased_by_identity[rebased_identity].append(entry)
    grouped: dict[tuple[str, SourceKind, str], list[StorageLeafRef]] = defaultdict(list)
    unmapped: list[StorageLeafRef] = []
    fallback_owner = replacements[0][0].node_id
    for leaf in actual.leaves:
        preserved_owner = _first_remote_owner(leaf, preserved_index, preserved_prefix_lengths)
        if preserved_owner is not None:
            grouped[(f"preserve:{preserved_owner}", "protected-region", preserved_owner)].append(leaf)
            continue
        identity = storage_leaf_identity(leaf)
        rebased = {
            (entry.operation_id, entry.source_kind, entry.node_or_occurrence_id): entry
            for entry in rebased_by_identity.get(identity, ())
        }
        current = next(iter(rebased.values())) if len(rebased) == 1 else existing_by_identity.get(identity)
        if current is not None and len(rebased) <= 1:
            grouped[(current.operation_id, current.source_kind, current.node_or_occurrence_id)].append(leaf)
            continue
        if leaf.semantic_path == ("#fragment[0]",) and leaf.field == "topology":
            grouped[(f"preserve-root:{fallback_owner}", "protected-region", fallback_owner)].append(leaf)
            continue
        unmapped.append(leaf)
    entries = tuple(
        SourceMapEntry(operation_id=key[0], source_kind=key[1], node_or_occurrence_id=key[2], leaves=tuple(leaves))
        for key, leaves in sorted(grouped.items())
    )
    preserved = tuple(item.node_id for item, _element in replacements)
    preserved_ids = set(preserved)
    skipped = tuple(node_id for node_id in required if node_id not in preserved_ids)
    proofs = tuple(
        RemoteSubtreeProof(
            node_id=node_id,
            candidate_node_path=path,
            remote_subtree_fingerprint=fingerprint,
        )
        for path, node_id, fingerprint in preserved_paths
    )
    return spliced, entries, tuple(unmapped), preserved, required, skipped, proofs


def _semantic_element_paths(root: ET._Element) -> tuple[tuple[tuple[str, ...], ET._Element], ...]:
    """Index each element by the same sibling-aware path used by storage leaves."""

    result: list[tuple[tuple[str, ...], ET._Element]] = []

    def visit(parent: ET._Element, prefix: tuple[str, ...]) -> None:
        counts: defaultdict[str, int] = defaultdict(int)
        for child in parent:
            name = storage_node_name(child)
            ordinal = counts[name]
            counts[name] += 1
            path = prefix + (f"{name}[{ordinal}]",)
            result.append((path, child))
            visit(child, path)

    visit(root, ())
    return tuple(result)


def _top_level_nodes(nodes: tuple[SemanticNode, ...]) -> tuple[SemanticNode, ...]:
    root = next((node for node in nodes if node.parent_id is None), None)
    if root is None:
        return ()
    return tuple(sorted((node for node in nodes if node.parent_id == root.node_id), key=lambda item: item.ordinal))


def _candidate_paths(entries: tuple[SourceMapEntry, ...]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for entry in entries:
        grouped[entry.node_or_occurrence_id].extend(
            leaf.semantic_path for leaf in entry.leaves if leaf.semantic_path != ("#fragment[0]",)
        )
    return {node_id: _common_prefix(paths) for node_id, paths in grouped.items() if paths}


def _subtree_reusable(
    node_id: str,
    base_nodes: tuple[SemanticNode, ...],
    candidate_nodes: tuple[SemanticNode, ...],
    operations: tuple[EditOperation, ...],
) -> bool:
    base_parents = {node.node_id: node.parent_id for node in base_nodes}
    candidate_parents = {node.node_id: node.parent_id for node in candidate_nodes}

    def inside(candidate: str | None, parents: dict[str, str | None]) -> bool:
        current = candidate
        while current is not None:
            if current == node_id:
                return True
            current = parents.get(current)
        return False

    for operation in operations:
        if operation.kind == "move" and operation.base_node_id == node_id:
            continue
        if inside(operation.base_node_id, base_parents) or inside(operation.edited_node_id, candidate_parents):
            return False
        if operation.kind == "insert" and inside(operation.new_parent_id, candidate_parents):
            return False
        if operation.kind == "delete" and inside(operation.old_parent_id, base_parents):
            return False
        if operation.kind == "move" and (
            inside(operation.old_parent_id, base_parents) or inside(operation.new_parent_id, candidate_parents)
        ):
            return False
    return node_id in candidate_parents


def _element_at_path(root: ET._Element, path: tuple[str, ...] | None) -> ET._Element | None:
    if not path:
        return None
    current = root
    for component in path:
        if component == "#fragment[0]":
            continue
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


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    prefix = list(paths[0])
    for path in paths[1:]:
        limit = min(len(prefix), len(path))
        index = 0
        while index < limit and prefix[index] == path[index]:
            index += 1
        prefix = prefix[:index]
    return tuple(prefix)


def _is_under(path: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return path[: len(prefix)] == prefix


def _remote_contains_leaf(leaf: StorageLeafRef, prefix: tuple[str, ...]) -> bool:
    return _is_under(leaf.semantic_path, prefix) and not (leaf.semantic_path == prefix and leaf.field == "tail")


def _remote_owner_index(
    owner_by_path: tuple[tuple[tuple[str, ...], str], ...],
) -> tuple[dict[tuple[str, ...], tuple[int, str]], tuple[int, ...]]:
    indexed: dict[tuple[str, ...], tuple[int, str]] = {}
    for position, (path, owner) in enumerate(owner_by_path):
        indexed.setdefault(path, (position, owner))
    return indexed, tuple(sorted({len(path) for path in indexed}))


def _first_remote_owner(
    leaf: StorageLeafRef,
    indexed: dict[tuple[str, ...], tuple[int, str]],
    prefix_lengths: tuple[int, ...],
) -> str | None:
    selected: tuple[int, str] | None = None
    for length in prefix_lengths:
        if length > len(leaf.semantic_path):
            break
        match = indexed.get(leaf.semantic_path[:length])
        if match is None or (length == len(leaf.semantic_path) and leaf.field == "tail"):
            continue
        if selected is None or match[0] < selected[0]:
            selected = match
    return selected[1] if selected is not None else None


def _leaf_sort_key(leaf: StorageLeafRef) -> tuple[tuple[str, ...], str, str, int]:
    return leaf.semantic_path, leaf.field, leaf.attribute_name or "", leaf.occurrence_ordinal


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["apply_remote_subtree_splice", "build_remote_subtrees", "claim_base_remote_leaves"]
