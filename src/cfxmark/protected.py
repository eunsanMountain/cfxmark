"""Remote-backed projections used by managed Markdown editing.

The public Markdown stays readable and marker-free.  During a managed push we
temporarily rehydrate unchanged projections into the legacy opaque transport so
the existing parser/renderer and source-map proof can preserve the exact remote
subtree.  The transport never appears in the user's file.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, fields
from functools import cache
from typing import Literal

import lxml.etree as ET

from cfxmark.artifacts import (
    MarkdownRange,
    ProtectedRegionArtifact,
    ProtectedRegionChange,
    ProtectedRegionProof,
    RemoteSubtreeArtifact,
    TaskStatusChange,
)
from cfxmark.ast import (
    BlockNode,
    DirectiveMacro,
    Document,
    HardBreak,
    Image,
    InlineOpaque,
    List,
    Node,
    OpaqueBlock,
    Paragraph,
    Table,
)
from cfxmark.diagnostics import ConversionDiagnostic
from cfxmark.opaque import opaque_id_for, serialize_inline_opaque, serialize_opaque, serialize_payloads
from cfxmark.parsers.cfx import _parse_fragment_to_element, _serialize_element
from cfxmark.parsers.md import _has_unescaped_pipe, _is_gfm_table_delimiter, _lines_outside_fences
from cfxmark.source_map import EditOperation, SemanticMappingAmbiguous, SemanticNode, match_edit_operations
from cfxmark.xml_ns import storage_node_name, storage_semantic_path


def protected_projection(node: InlineOpaque | OpaqueBlock) -> str:
    """Return the stable, human-readable projection for an opaque node."""

    task_projection = _task_projection(node.raw_xml)
    if task_projection is not None:
        return task_projection
    common_projection = _common_projection(node.raw_xml)
    if common_projection is not None:
        return common_projection
    if isinstance(node, InlineOpaque):
        return node.label or "Unsupported Confluence content"
    return "[Unsupported Confluence content]"


def _opaque_nodes(document: Document) -> tuple[InlineOpaque | OpaqueBlock, ...]:
    found: list[InlineOpaque | OpaqueBlock] = []

    def visit(value: object) -> None:
        if isinstance(value, (InlineOpaque, OpaqueBlock)):
            found.append(value)
            return
        if not isinstance(value, Node):
            return
        for item in fields(value):
            member = getattr(value, item.name)
            if isinstance(member, Node):
                visit(member)
            elif isinstance(member, tuple):
                for child in member:
                    visit(child)

    visit(document)
    return tuple(found)


def build_protected_regions(
    document: Document,
    markdown: str,
    diagnostics: tuple[ConversionDiagnostic, ...] | list[ConversionDiagnostic],
    *,
    excluded_node_paths: tuple[tuple[str, ...], ...] = (),
    excluded_markdown_ranges: tuple[MarkdownRange, ...] = (),
) -> tuple[ProtectedRegionArtifact, ...]:
    """Bind opaque AST occurrences to exact ranges in rendered Markdown."""

    def excluded(path: tuple[str, ...]) -> bool:
        return any(path[: len(owner)] == owner for owner in excluded_node_paths)

    nodes = tuple(node for node in _opaque_nodes(document) if not excluded(node.source_path))
    occurrences_by_path: dict[tuple[str, ...], list[ConversionDiagnostic]] = {}
    for item in diagnostics:
        if item.code != "unsupported-confluence-content" or excluded(item.location.semantic_path):
            continue
        occurrences_by_path.setdefault(item.location.semantic_path, []).append(item)
    node_occurrences: list[tuple[InlineOpaque | OpaqueBlock, ConversionDiagnostic]] = []
    for node in nodes:
        matches = occurrences_by_path.get(node.source_path, ())
        if len(matches) == 1:
            node_occurrences.append((node, matches[0]))
    if not node_occurrences:
        return ()

    projections = tuple(protected_projection(node) for node, _diagnostic in node_occurrences)
    ranges = _unique_ordered_ranges(markdown, projections, excluded_ranges=excluded_markdown_ranges)
    if ranges is None:
        return ()
    regions: list[ProtectedRegionArtifact] = []
    for (node, diagnostic), projection, markdown_range in zip(node_occurrences, projections, ranges, strict=True):
        raw_xml = node.raw_xml
        task_projection = _task_projection(raw_xml)
        is_task_list = task_projection is not None
        regions.append(
            ProtectedRegionArtifact(
                occurrence_id=diagnostic.occurrence_id,
                kind="task-list" if is_task_list else ("inline" if isinstance(node, InlineOpaque) else "block"),
                remote_node_path=diagnostic.location.semantic_path,
                base_markdown_range=markdown_range,
                display_label=projection,
                projection_fingerprint=_sha256(projection),
                remote_subtree_fingerprint=_sha256(raw_xml),
                edit_policy="task_status_only" if is_task_list else "preserve_only",
                next_action=(
                    "change checkbox status only; task text and order are protected"
                    if is_task_list
                    else "leave this projection unchanged or use storage-aware patching"
                ),
            )
        )
    return tuple(regions)


def _has_ragged_rows(table: Table) -> bool:
    """Return whether GFM rectangularisation would change row topology."""

    rows = (*((table.header,) if table.header is not None else ()), *table.body)
    return len({len(row.cells) for row in rows}) > 1


def build_table_island_regions(
    document: Document,
    markdown: str,
    remote_subtrees: tuple[RemoteSubtreeArtifact, ...],
) -> tuple[ProtectedRegionArtifact, ...]:
    """Protect readable top-level tables whose exact storage is not fully editable.

    The artifact stores only a concise label and fingerprints.  The full table
    projection remains the managed Markdown itself, while authenticated remote
    storage is recovered during push.
    """

    subtrees_by_path = {subtree.remote_node_path: subtree for subtree in remote_subtrees}
    regions: list[ProtectedRegionArtifact] = []
    for block in document.children:
        if not isinstance(block, Table) or not block.source_path:
            continue
        subtree = subtrees_by_path.get(block.source_path)
        if subtree is None:
            continue
        if not subtree.requires_fresh_source and not _has_ragged_rows(block):
            continue
        markdown_range = subtree.base_markdown_range
        projection = markdown[markdown_range.start : markdown_range.end]
        projection_fingerprint = f"sha256:{subtree.projection_fingerprint}"
        remote_subtree_fingerprint = f"sha256:{subtree.remote_subtree_fingerprint}"
        if _sha256(projection) != projection_fingerprint:
            continue
        location = "/".join(block.source_path)
        identity = hashlib.sha256(
            f"cfxmark-table-island-v1\0{location}\0{remote_subtree_fingerprint}".encode()
        ).hexdigest()[:20]
        regions.append(
            ProtectedRegionArtifact(
                occurrence_id=f"occ-{identity}",
                kind="table-island",
                remote_node_path=block.source_path,
                base_markdown_range=markdown_range,
                display_label=f"Protected table at {location}",
                projection_fingerprint=projection_fingerprint,
                remote_subtree_fingerprint=remote_subtree_fingerprint,
                edit_policy="preserve_only",
                next_action="leave this table unchanged or use storage-aware patching",
            )
        )
    return tuple(regions)


def build_remote_subtree_regions(
    document: Document,
    markdown: str,
    remote_subtrees: tuple[RemoteSubtreeArtifact, ...],
    *,
    excluded_node_paths: tuple[tuple[str, ...], ...] = (),
) -> tuple[ProtectedRegionArtifact, ...]:
    """Protect readable non-table blocks whose exact storage needs remote reuse.

    Lists with Confluence-only marker styles, empty paragraphs, and other
    non-canonical blocks remain ordinary readable Markdown.  Their unchanged
    projection is nevertheless treated as one atomic island so the push path
    can rehydrate the authenticated remote subtree instead of regenerating it.
    """

    subtrees_by_path = {subtree.remote_node_path: subtree for subtree in remote_subtrees}
    regions: list[ProtectedRegionArtifact] = []
    for block in document.children:
        if isinstance(block, Table) or not block.source_path:
            continue
        is_atomic_projection_paragraph = (
            isinstance(block, Paragraph)
            and bool(block.children)
            and all(isinstance(child, (HardBreak, Image)) for child in block.children)
        )
        if not isinstance(block, (List, DirectiveMacro)) and not is_atomic_projection_paragraph:
            continue
        if any(block.source_path[: len(path)] == path for path in excluded_node_paths):
            continue
        subtree = subtrees_by_path.get(block.source_path)
        if subtree is None or not subtree.requires_fresh_source:
            continue
        markdown_range = subtree.base_markdown_range
        projection = markdown[markdown_range.start : markdown_range.end]
        projection_fingerprint = f"sha256:{subtree.projection_fingerprint}"
        remote_subtree_fingerprint = f"sha256:{subtree.remote_subtree_fingerprint}"
        if not projection or _sha256(projection) != projection_fingerprint:
            continue
        location = "/".join(block.source_path)
        identity = hashlib.sha256(
            f"cfxmark-remote-subtree-region-v1\0{location}\0{remote_subtree_fingerprint}".encode()
        ).hexdigest()[:20]
        regions.append(
            ProtectedRegionArtifact(
                occurrence_id=f"occ-{identity}",
                kind="remote-subtree",
                remote_node_path=block.source_path,
                base_markdown_range=markdown_range,
                display_label=projection,
                projection_fingerprint=projection_fingerprint,
                remote_subtree_fingerprint=remote_subtree_fingerprint,
                edit_policy="preserve_only",
                next_action="leave this block unchanged or edit a different Markdown block",
            )
        )
    return tuple(regions)


@dataclass(frozen=True)
class _ResolvedProtectedRegion:
    node: Node
    raw_xml: str


def _fresh_nodes_for_regions(
    fresh_document: Document,
    fresh_source: str,
    base_markdown: str,
    regions: tuple[ProtectedRegionArtifact, ...],
) -> tuple[_ResolvedProtectedRegion, ...] | None:
    """Rebuild protected authority exclusively from the authenticated remote source."""

    fresh_opaque_nodes = list(_opaque_nodes(fresh_document))
    fresh_block_by_path = {node.source_path: node for node in fresh_document.children if node.source_path}
    if len({region.occurrence_id for region in regions}) != len(regions):
        return None
    if len({region.remote_node_path for region in regions}) != len(regions):
        return None

    source_root = _parse_fragment_to_element(fresh_source)
    protected_descendant_raw: set[str] = set()
    for region in regions:
        if region.kind not in {"table-island", "remote-subtree"}:
            continue
        protected_element = _element_at_path(source_root, region.remote_node_path)
        if protected_element is None:
            return None
        protected_descendant_raw.update(
            _serialize_element(element) for element in protected_element.iter() if isinstance(element.tag, str)
        )
    resolved: list[_ResolvedProtectedRegion] = []
    for region in regions:
        source_element = _element_at_path(source_root, region.remote_node_path)
        if source_element is None:
            return None
        source_raw = _serialize_element(source_element)
        node: Node | None
        if region.kind in {"table-island", "remote-subtree"}:
            node = fresh_block_by_path.get(region.remote_node_path)
        else:
            exact_path = [
                (index, candidate)
                for index, candidate in enumerate(fresh_opaque_nodes)
                if candidate.source_path == region.remote_node_path
            ]
            candidates = exact_path or [
                (index, candidate)
                for index, candidate in enumerate(fresh_opaque_nodes)
                if candidate.raw_xml == source_raw
            ]
            if len(candidates) != 1:
                return None
            index, node = candidates[0]
            fresh_opaque_nodes.pop(index)
        if node is None:
            return None
        markdown_range = region.base_markdown_range
        projection = base_markdown[markdown_range.start : markdown_range.end]
        if (
            _sha256(source_raw) != region.remote_subtree_fingerprint
            or _sha256(projection) != region.projection_fingerprint
            or markdown_range.start < 0
            or markdown_range.end < markdown_range.start
        ):
            return None
        if region.kind == "table-island":
            if not isinstance(node, Table) or region.edit_policy != "preserve_only":
                return None
        elif region.kind == "remote-subtree":
            if (
                not isinstance(node, BlockNode)
                or node.source_path != region.remote_node_path
                or region.edit_policy != "preserve_only"
                or projection != region.display_label
            ):
                return None
        else:
            if not isinstance(node, (InlineOpaque, OpaqueBlock)) or node.raw_xml != source_raw:
                return None
            expected_projection = protected_projection(node)
            task_projection = _task_projection(node.raw_xml)
            expected_kind = (
                "task-list"
                if task_projection is not None
                else ("inline" if isinstance(node, InlineOpaque) else "block")
            )
            expected_policy = "task_status_only" if task_projection is not None else "preserve_only"
            if (
                projection != expected_projection
                or region.display_label != expected_projection
                or region.kind != expected_kind
                or region.edit_policy != expected_policy
            ):
                return None
        resolved.append(_ResolvedProtectedRegion(node=node, raw_xml=source_raw))
    if any(node.raw_xml not in protected_descendant_raw for node in fresh_opaque_nodes):
        return None
    return tuple(resolved)


def _element_at_path(root: ET._Element, path: tuple[str, ...]) -> ET._Element | None:
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
        if ordinal < 0 or ordinal >= len(matches):
            return None
        current = matches[ordinal]
    return current


def prepare_splice_markdown(
    edited_markdown: str,
    *,
    base_markdown: str,
    fresh_document: Document,
    fresh_source: str,
    regions: tuple[ProtectedRegionArtifact, ...],
) -> tuple[
    str,
    tuple[str, ...],
    tuple[ProtectedRegionChange, ...],
    tuple[TaskStatusChange, ...],
    tuple[tuple[str, str, tuple[str, ...]], ...],
]:
    """Rehydrate unchanged protected projections into a transient transport.

    The normative semantic matcher proves correspondence over a flat token
    stream of ordinary Markdown and protected projections.  Duplicate, copied,
    moved, deleted, or changed projections are blocked instead of guessed.
    """

    if not regions:
        return edited_markdown, (), (), (), ()
    nodes = _fresh_nodes_for_regions(fresh_document, fresh_source, base_markdown, regions)
    if nodes is None:
        return (
            edited_markdown,
            (),
            tuple(_blocked_change(region, kind="ambiguous") for region in regions),
            (),
            (),
        )
    try:
        anchored_ranges = _byte_anchored_projection_ranges(
            edited_markdown,
            base_markdown=base_markdown,
            regions=regions,
        )
        mismatched_occurrences: set[str] = set()
        operations_by_occurrence: dict[str, EditOperation] = {}
        current_ranges: dict[str, MarkdownRange] = {}
        if anchored_ranges is not None:
            current_ranges = {
                region.occurrence_id: current_range
                for region, current_range in zip(regions, anchored_ranges, strict=True)
            }
        else:
            base_tokens = _projection_tokens(base_markdown, regions, base_markdown=base_markdown, base=True)
            edited_tokens = _projection_tokens(edited_markdown, regions, base_markdown=base_markdown, base=False)
            match = match_edit_operations(base_tokens.nodes, edited_tokens.nodes)
            operations_by_base = {
                operation.base_node_id: operation for operation in match.operations if operation.base_node_id
            }
            mismatched_keys = {
                key
                for key in base_tokens.region_key_by_occurrence.values()
                if base_tokens.key_counts[key] != edited_tokens.key_counts[key]
            }
            mismatched_occurrences = {
                occurrence_id
                for occurrence_id, key in base_tokens.region_key_by_occurrence.items()
                if key in mismatched_keys
            }
            for region in regions:
                base_node_id = base_tokens.region_node_by_occurrence[region.occurrence_id]
                edited_node_id = match.base_to_edited_source.get(base_node_id)
                if edited_node_id is not None:
                    current_range = edited_tokens.range_by_node.get(edited_node_id)
                    if current_range is not None:
                        current_ranges[region.occurrence_id] = current_range
                operation = operations_by_base.get(base_node_id)
                if operation is not None:
                    operations_by_occurrence[region.occurrence_id] = operation
    except SemanticMappingAmbiguous:
        return (
            edited_markdown,
            (),
            tuple(_blocked_change(region, kind="ambiguous") for region in regions),
            (),
            (),
        )

    mapped: list[tuple[int, int, ProtectedRegionArtifact, _ResolvedProtectedRegion]] = []
    blocked: list[ProtectedRegionChange] = []
    task_changes: list[TaskStatusChange] = []
    task_replacements: dict[str, str] = {}
    for region, resolved in zip(regions, nodes, strict=True):
        node = resolved.node
        if region.occurrence_id in mismatched_occurrences:
            blocked.append(_blocked_change(region, kind="ambiguous"))
            continue
        current_range = current_ranges.get(region.occurrence_id)
        operation = operations_by_occurrence.get(region.occurrence_id)
        if current_range is None and operation is None:
            blocked.append(_blocked_change(region, kind="delete"))
            continue
        if current_range is None or operation is not None:
            blocked.append(
                _blocked_change(
                    region,
                    kind=(
                        "move"
                        if operation is not None and operation.kind == "move"
                        else "edit"
                        if operation is not None and operation.kind == "update"
                        else "ambiguous"
                    ),
                    current_range=current_range,
                )
            )
            continue
        current_projection = edited_markdown[current_range.start : current_range.end]
        if region.edit_policy == "task_status_only":
            base_projection = _task_projection(resolved.raw_xml)
            updated_raw = _apply_task_status_projection(resolved.raw_xml, current_projection)
            if base_projection is None or updated_raw is None:
                blocked.append(_blocked_change(region, kind="edit", current_range=current_range))
                continue
            task_changes.extend(
                _task_status_changes(
                    region,
                    base_projection,
                    current_projection,
                    current_range=current_range,
                )
            )
            task_replacements[region.occurrence_id] = updated_raw
        elif _sha256(current_projection) != region.projection_fingerprint:
            blocked.append(_blocked_change(region, kind="edit", current_range=current_range))
            continue
        mapped.append((current_range.start, current_range.end, region, resolved))

    transient = edited_markdown
    inline_payloads: dict[str, str] = {}
    expected_fingerprints: dict[str, str] = {}
    for start, end, region, resolved in reversed(mapped):
        node = resolved.node
        if region.kind in {"table-island", "remote-subtree"}:
            expected_fingerprints[region.occurrence_id] = _sha256(resolved.raw_xml)
            replacement = serialize_opaque(resolved.raw_xml, opaque_id_for(resolved.raw_xml))
            transient = transient[:start] + replacement + transient[end:]
            continue
        if isinstance(node, InlineOpaque):
            replacement = serialize_inline_opaque(node.label, node.opaque_id)
            inline_payloads[node.opaque_id] = resolved.raw_xml
            expected_fingerprints[region.occurrence_id] = _sha256(resolved.raw_xml)
        else:
            assert isinstance(node, OpaqueBlock)
            raw_xml = task_replacements.get(region.occurrence_id, resolved.raw_xml)
            replacement = serialize_opaque(raw_xml, node.opaque_id if raw_xml == resolved.raw_xml else None)
            expected_fingerprints[region.occurrence_id] = _sha256(raw_xml)
        transient = transient[:start] + replacement + transient[end:]
    if inline_payloads:
        transient = transient.rstrip("\n") + "\n\n" + serialize_payloads(inline_payloads) + "\n"
    return (
        transient,
        tuple(region.occurrence_id for *_prefix, region, _node in mapped),
        tuple(blocked),
        tuple(task_changes),
        tuple(
            (region.occurrence_id, expected_fingerprints[region.occurrence_id], region.remote_node_path)
            for region in regions
            if region.occurrence_id in expected_fingerprints
        ),
    )


def build_protected_region_proofs(
    xhtml: str,
    expectations: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> tuple[tuple[ProtectedRegionProof, ...], tuple[str, ...]]:
    """Bind every expected protected value to one exact final candidate path."""

    if not expectations:
        return (), ()
    root = _parse_fragment_to_element(xhtml)
    actual_paths: dict[str, list[tuple[str, ...]]] = {}
    for element in root.iterdescendants():
        if not isinstance(element.tag, str):
            continue
        raw = _serialize_element(element)
        actual_paths.setdefault(_sha256(raw), []).append(storage_semantic_path(root, element))
    unused_paths = {fingerprint: list(paths) for fingerprint, paths in actual_paths.items()}
    proofs: list[ProtectedRegionProof] = []
    missing: list[str] = []
    for occurrence_id, fingerprint, expected_path in expectations:
        candidates = unused_paths.get(fingerprint, [])
        if expected_path in candidates:
            candidate_path = expected_path
        elif len(candidates) == 1:
            candidate_path = candidates[0]
        else:
            missing.append(occurrence_id)
            continue
        candidates.remove(candidate_path)
        proofs.append(
            ProtectedRegionProof(
                occurrence_id=occurrence_id,
                candidate_node_path=candidate_path,
                remote_subtree_fingerprint=fingerprint,
            )
        )
    return tuple(proofs), tuple(missing)


@dataclass(frozen=True)
class _ProjectionSpan:
    start: int
    end: int
    key: str
    node_type: str


@dataclass(frozen=True)
class _ProjectionTokens:
    nodes: tuple[SemanticNode, ...]
    range_by_node: dict[str, MarkdownRange]
    region_node_by_occurrence: dict[str, str]
    region_key_by_occurrence: dict[str, str]
    key_counts: Counter[str]


def _byte_anchored_projection_ranges(
    edited_markdown: str,
    *,
    base_markdown: str,
    regions: tuple[ProtectedRegionArtifact, ...],
) -> tuple[MarkdownRange, ...] | None:
    """Map protected ranges outside one changed byte window without searching labels.

    Exact no-op, prefix, suffix, and EOF-append edits have authoritative byte
    correspondence. A projection copied into the changed window or intersected
    by that window falls back to the semantic matcher so copy/move/edit safety
    remains fail-closed.
    """

    shared_limit = min(len(base_markdown), len(edited_markdown))
    prefix = 0
    while prefix < shared_limit and base_markdown[prefix] == edited_markdown[prefix]:
        prefix += 1

    suffix = 0
    suffix_limit = shared_limit - prefix
    while (
        suffix < suffix_limit
        and base_markdown[len(base_markdown) - suffix - 1] == edited_markdown[len(edited_markdown) - suffix - 1]
    ):
        suffix += 1

    base_suffix_start = len(base_markdown) - suffix
    edited_suffix_start = len(edited_markdown) - suffix
    delta = len(edited_markdown) - len(base_markdown)
    edited_table_ranges = set(_gfm_table_spans(edited_markdown))
    mapped: list[MarkdownRange] = []
    for region in regions:
        start = region.base_markdown_range.start
        end = region.base_markdown_range.end
        if start < 0 or end < start or end > len(base_markdown):
            raise SemanticMappingAmbiguous("a protected base range is outside the managed Markdown")
        base_projection = base_markdown[start:end]
        if _sha256(base_projection) != region.projection_fingerprint:
            raise SemanticMappingAmbiguous("a protected base range no longer matches its projection fingerprint")
        if end <= prefix:
            current = MarkdownRange(start=start, end=end)
        elif start >= base_suffix_start:
            current = MarkdownRange(start=start + delta, end=end + delta)
        else:
            return None
        current_projection = edited_markdown[current.start : current.end]
        if _sha256(current_projection) != region.projection_fingerprint:
            return None
        if region.kind == "table-island" and (current.start, current.end) not in edited_table_ranges:
            return None
        mapped.append(current)

    changed_projection = edited_markdown[prefix:edited_suffix_start]
    for region in regions:
        projection = base_markdown[region.base_markdown_range.start : region.base_markdown_range.end]
        if projection and projection in changed_projection:
            return None
    return tuple(mapped)


def _gfm_table_spans(markdown: str) -> tuple[tuple[int, int], ...]:
    """Return complete pipe-table runs outside fenced code blocks.

    A protected table projection is reusable only when it owns the entire run.
    This makes a row prepended or appended without a blank line part of the
    changed boundary instead of letting the old table match as a substring.
    """

    lines = markdown.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    content_lines = [line.rstrip("\r\n") for line in lines]
    outside_fences = _lines_outside_fences(content_lines)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        content = content_lines[index]
        indentation = len(content) - len(content.lstrip(" "))
        if not outside_fences[index] or content.startswith("\t") or indentation > 3 or not _has_unescaped_pipe(content):
            index += 1
            continue

        run_start = index
        while index < len(lines):
            content = content_lines[index]
            indentation = len(content) - len(content.lstrip(" "))
            if (
                not outside_fences[index]
                or content.startswith("\t")
                or indentation > 3
                or not _has_unescaped_pipe(content)
            ):
                break
            index += 1
        run = tuple(content_lines[run_start:index])
        if len(run) < 2 or not any(_is_gfm_table_delimiter(line) for line in run[1:]):
            continue
        final_line = lines[index - 1].rstrip("\r\n")
        spans.append((offsets[run_start], offsets[index - 1] + len(final_line)))
    return tuple(spans)


def _projection_tokens(
    markdown: str,
    regions: tuple[ProtectedRegionArtifact, ...],
    *,
    base_markdown: str,
    base: bool,
) -> _ProjectionTokens:
    spans_by_range: dict[tuple[int, int], _ProjectionSpan] = {}
    region_by_range = {(region.base_markdown_range.start, region.base_markdown_range.end): region for region in regions}
    if base:
        for region in regions:
            start = region.base_markdown_range.start
            end = region.base_markdown_range.end
            if start < 0 or end < start or end > len(markdown):
                raise SemanticMappingAmbiguous("a protected base range is outside the managed Markdown")
            projection = markdown[start:end]
            if _sha256(projection) != region.projection_fingerprint:
                raise SemanticMappingAmbiguous("a protected base range no longer matches its projection fingerprint")
            if region.edit_policy == "task_status_only":
                task_key = _task_text_key(projection)
                if task_key is None:
                    raise SemanticMappingAmbiguous("a protected task base range is not a task projection")
                key = _sha256("task\0" + "\0".join(task_key))
                node_type = "TaskProjection"
            else:
                key = _sha256("preserve\0" + projection)
                node_type = "ProtectedProjection"
            spans_by_range[(start, end)] = _ProjectionSpan(start, end, key, node_type)
    else:
        table_projections = {
            base_markdown[region.base_markdown_range.start : region.base_markdown_range.end]
            for region in regions
            if region.edit_policy == "preserve_only" and region.kind == "table-island"
        }
        preserve_projections = {
            region.display_label
            for region in regions
            if region.edit_policy == "preserve_only" and region.kind != "table-island"
        }
        table_spans = _gfm_table_spans(markdown)
        for projection in table_projections:
            key = _sha256("preserve\0" + projection)
            for start, end in table_spans:
                if markdown[start:end] != projection:
                    continue
                span = _ProjectionSpan(start, end, key, "ProtectedProjection")
                spans_by_range[(span.start, span.end)] = span
        atomic_table_ranges = tuple(spans_by_range)

        def overlaps_atomic_table(start: int, end: int) -> bool:
            return any(start < table_end and table_start < end for table_start, table_end in atomic_table_ranges)

        for projection in preserve_projections:
            key = _sha256("preserve\0" + projection)
            for occurrence in re.finditer(re.escape(projection), markdown):
                if overlaps_atomic_table(occurrence.start(), occurrence.end()):
                    continue
                span = _ProjectionSpan(occurrence.start(), occurrence.end(), key, "ProtectedProjection")
                spans_by_range[(span.start, span.end)] = span
        for occurrence in re.finditer(r"(?m)(?:^- \[[ xX]\] [^\n]*(?:\n|$))+", markdown):
            end = occurrence.end() - (1 if occurrence.group(0).endswith("\n") else 0)
            if overlaps_atomic_table(occurrence.start(), end):
                continue
            projection = markdown[occurrence.start() : end]
            task_key = _task_text_key(projection)
            if task_key is None:
                continue
            key = _sha256("task\0" + "\0".join(task_key))
            span = _ProjectionSpan(occurrence.start(), end, key, "TaskProjection")
            spans_by_range[(span.start, span.end)] = span

    spans = sorted(spans_by_range.values(), key=lambda item: (item.start, item.end))
    if any(current.start < previous.end for previous, current in zip(spans, spans[1:])):
        raise SemanticMappingAmbiguous("protected projection candidates overlap")

    if base and any(key not in spans_by_range for key in region_by_range):
        raise SemanticMappingAmbiguous("a protected base range is not an exact projection token")

    nodes: list[SemanticNode] = []
    range_by_node: dict[str, MarkdownRange] = {}
    region_node_by_occurrence: dict[str, str] = {}
    region_key_by_occurrence: dict[str, str] = {}
    cursor = 0

    def append_node(node_type: str, fingerprint: str, source_range: MarkdownRange | None) -> str:
        ordinal = len(nodes)
        prefix = "base" if base else "edited"
        node_id = f"{prefix}-projection-{ordinal}-{fingerprint.removeprefix('sha256:')[:12]}"
        nodes.append(
            SemanticNode(
                node_id=node_id,
                node_type=node_type,
                parent_id=None,
                ordinal=ordinal,
                subtree_fingerprint=fingerprint,
                local_fingerprint=fingerprint,
                topology_fingerprint=_sha256(node_type),
            )
        )
        if source_range is not None:
            range_by_node[node_id] = source_range
        return node_id

    def append_markdown_tokens(start: int, end: int) -> None:
        for token in re.finditer(r"\S+", markdown[start:end]):
            value = token.group(0)
            append_node("MarkdownText", _sha256(value), None)

    for span in spans:
        if cursor < span.start:
            append_markdown_tokens(cursor, span.start)
        source_range = MarkdownRange(start=span.start, end=span.end)
        node_id = append_node(span.node_type, span.key, source_range)
        if base:
            matched_region = region_by_range.get((span.start, span.end))
            if matched_region is not None:
                region_node_by_occurrence[matched_region.occurrence_id] = node_id
                region_key_by_occurrence[matched_region.occurrence_id] = span.key
        cursor = span.end
    if cursor < len(markdown):
        append_markdown_tokens(cursor, len(markdown))

    if base and len(region_node_by_occurrence) != len(regions):
        raise SemanticMappingAmbiguous("not every protected region has a unique base token")
    return _ProjectionTokens(
        nodes=tuple(nodes),
        range_by_node=range_by_node,
        region_node_by_occurrence=region_node_by_occurrence,
        region_key_by_occurrence=region_key_by_occurrence,
        key_counts=Counter(span.key for span in spans),
    )


def _unique_ordered_ranges(
    markdown: str,
    projections: tuple[str, ...],
    *,
    excluded_ranges: tuple[MarkdownRange, ...] = (),
) -> tuple[MarkdownRange, ...] | None:
    def available(start: int, end: int) -> bool:
        return not any(start < excluded.end and excluded.start < end for excluded in excluded_ranges)

    candidates = tuple(
        tuple(
            MarkdownRange(match.start(), match.end())
            for match in re.finditer(re.escape(projection), markdown)
            if available(match.start(), match.end())
        )
        for projection in projections
    )
    if any(not items for items in candidates):
        return None

    @cache
    def solve(index: int, minimum_start: int) -> tuple[tuple[MarkdownRange, ...], ...]:
        if index == len(candidates):
            return ((),)
        solutions: list[tuple[MarkdownRange, ...]] = []
        for candidate in candidates[index]:
            if candidate.start < minimum_start:
                continue
            for suffix in solve(index + 1, candidate.end):
                solutions.append((candidate, *suffix))
                if len(solutions) == 2:
                    return tuple(solutions)
        return tuple(solutions)

    solutions = solve(0, 0)
    return solutions[0] if len(solutions) == 1 else None


def _blocked_change(
    region: ProtectedRegionArtifact,
    *,
    kind: Literal["edit", "move", "delete", "ambiguous"],
    current_range: MarkdownRange | None = None,
) -> ProtectedRegionChange:
    action = {"edit": "edited", "move": "moved", "delete": "deleted", "ambiguous": "matched"}[kind]
    return ProtectedRegionChange(
        occurrence_id=region.occurrence_id,
        kind=kind,
        base_markdown_range=region.base_markdown_range,
        current_markdown_range=current_range,
        display_label=region.display_label,
        reason=f"protected {region.kind} projection cannot be safely {action}",
        next_action=region.next_action,
    )


def _task_text_key(projection: str) -> tuple[str, ...] | None:
    lines = projection.splitlines()
    values: list[str] = []
    for line in lines:
        match = re.fullmatch(r"- \[[ xX]\] (.*)", line)
        if match is None:
            return None
        values.append(match.group(1))
    return tuple(values)


def _task_status_changes(
    region: ProtectedRegionArtifact,
    base_projection: str,
    edited_projection: str,
    *,
    current_range: MarkdownRange,
) -> tuple[TaskStatusChange, ...]:
    base_lines = base_projection.splitlines()
    edited_lines = edited_projection.splitlines()
    changes: list[TaskStatusChange] = []
    base_offset = region.base_markdown_range.start
    current_offset = current_range.start
    for index, (base_line, edited_line) in enumerate(zip(base_lines, edited_lines, strict=True)):
        base_status: Literal["incomplete", "complete"] = (
            "complete" if base_line.startswith("- [x]") or base_line.startswith("- [X]") else "incomplete"
        )
        edited_status: Literal["incomplete", "complete"] = (
            "complete" if edited_line.startswith("- [x]") or edited_line.startswith("- [X]") else "incomplete"
        )
        base_end = base_offset + len(base_line)
        current_end = current_offset + len(edited_line)
        if base_status != edited_status:
            changes.append(
                TaskStatusChange(
                    occurrence_id=region.occurrence_id,
                    task_index=index,
                    before=base_status,
                    after=edited_status,
                    base_markdown_range=MarkdownRange(start=base_offset, end=base_end),
                    current_markdown_range=MarkdownRange(start=current_offset, end=current_end),
                    remote_node_path=region.remote_node_path,
                )
            )
        base_offset = base_end + 1
        current_offset = current_end + 1
    return tuple(changes)


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


_TASK_STATUS_RE = re.compile(
    r"(?P<open><ac:task-status(?:\s[^>]*)?>)(?P<status>.*?)(?P<close></ac:task-status>)",
    re.DOTALL,
)


def _task_projection(raw_xml: str) -> str | None:
    if "<ac:task-list" not in raw_xml:
        return None
    try:
        wrapped = '<root xmlns:ac="urn:cfxmark:ac" xmlns:ri="urn:cfxmark:ri">' + raw_xml + "</root>"
        root = ET.fromstring(wrapped.encode("utf-8"))
    except (ET.XMLSyntaxError, UnicodeEncodeError):
        return None
    lines: list[str] = []
    for task in (item for item in root.iter() if isinstance(item.tag, str) and ET.QName(item).localname == "task"):
        status_node = next(
            (item for item in task if isinstance(item.tag, str) and ET.QName(item).localname == "task-status"),
            None,
        )
        body_node = next(
            (item for item in task if isinstance(item.tag, str) and ET.QName(item).localname == "task-body"),
            None,
        )
        if status_node is None or body_node is None:
            return None
        status = _element_text(status_node).strip()
        body = " ".join(_element_text(body_node).split())
        lines.append(f"- [{'x' if status == 'complete' else ' '}] {body}")
    return "\n".join(lines) if lines else None


def _apply_task_status_projection(raw_xml: str, projection: str) -> str | None:
    base = _task_projection(raw_xml)
    if base is None:
        return None
    base_lines = base.splitlines()
    edited_lines = projection.splitlines()
    if len(base_lines) != len(edited_lines):
        return None
    statuses: list[str] = []
    for base_line, edited_line in zip(base_lines, edited_lines, strict=True):
        base_match = re.fullmatch(r"- \[[ xX]\] (.*)", base_line)
        edited_match = re.fullmatch(r"- \[([ xX])\] (.*)", edited_line)
        if base_match is None or edited_match is None or base_match.group(1) != edited_match.group(2):
            return None
        statuses.append("complete" if edited_match.group(1).lower() == "x" else "incomplete")
    status_iter = iter(statuses)
    updated = _TASK_STATUS_RE.sub(
        lambda match: match.group("open") + next(status_iter) + match.group("close"),
        raw_xml,
    )
    try:
        next(status_iter)
    except StopIteration:
        return updated
    return None


def _common_projection(raw_xml: str) -> str | None:
    try:
        wrapped = '<root xmlns:ac="urn:cfxmark:ac" xmlns:ri="urn:cfxmark:ri">' + raw_xml + "</root>"
        root = ET.fromstring(wrapped.encode("utf-8"))
    except (ET.XMLSyntaxError, UnicodeEncodeError):
        return None
    if not len(root) or not isinstance(root[0].tag, str):
        return None
    element = root[0]
    local = ET.QName(element).localname
    if local == "time":
        visible = " ".join(_element_text(element).split())
        datetime = next(
            (str(value) for name, value in element.attrib.items() if ET.QName(name).localname == "datetime"),
            "",
        )
        return visible or datetime or "time"
    if local in {"ul", "ol"}:
        items = [" ".join(_element_text(item).split()) for item in element if ET.QName(item).localname == "li"]
        if not items:
            return None
        prefix = "-" if local == "ul" else "1."
        return "<br>".join(f"{prefix} {item}" for item in items)
    if local == "table":
        cells = [
            " ".join(_element_text(cell).split())
            for cell in element.iter()
            if isinstance(cell.tag, str) and ET.QName(cell).localname in {"th", "td"}
        ]
        visible = " | ".join(item for item in cells if item)
        return f"Nested table: {visible}" if visible else "Nested table"
    return None


def _element_text(element: ET._Element) -> str:
    return "".join(part.decode("utf-8") if isinstance(part, bytes) else str(part) for part in element.itertext())


__all__ = [
    "build_protected_regions",
    "build_remote_subtree_regions",
    "build_table_island_regions",
    "prepare_splice_markdown",
    "protected_projection",
]
