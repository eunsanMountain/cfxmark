"""Public conversion API.

This module exposes the two public entry points — :func:`to_cfx` and
:func:`to_md` — plus the :class:`ConversionResult` dataclass they
return and the :class:`ConversionOptions` used to tweak their
behaviour.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from typing import Literal
from urllib.parse import unquote, urlsplit

import lxml.etree as ET

from cfxmark._version import __version__
from cfxmark.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    CfxArtifact,
    ManagedCfxArtifact,
    MarkdownArtifact,
    ProtectedRegionChange,
    ProtectedRegionProof,
    RemoteSubtreeProof,
    TaskStatusChange,
    apply_macro_identities,
    base_maps,
    candidate_maps,
    collateral_diagnostics,
    macro_identities,
    unrenderable_background_diagnostics,
)
from cfxmark.ast import Document, Image, InlineOpaque, Node, OpaqueBlock, Table, UnsupportedMarker
from cfxmark.diagnostics import (
    ConversionDiagnostic,
    DiagnosticLocation,
    DiagnosticResolution,
    diagnostics_from_messages,
    make_diagnostic,
)
from cfxmark.exceptions import ConversionError, ParseError
from cfxmark.macros import MacroRegistry, default_registry
from cfxmark.migration_comments import (
    render_migrations_summary,
    render_notice,
    strip_control_comments,
)
from cfxmark.migration_report import MigrationReport, effect_for_code, occurrences_from_diagnostics
from cfxmark.normalize import _serialize_children_exact, preservation_signature
from cfxmark.opaque import (
    is_round_trippable_inline_atomic,
    is_round_trippable_table_wrapper,
    strip_asset_markers,
)
from cfxmark.ownership_proof import prove_candidate_ownership
from cfxmark.parsers.cfx import (
    _EMOTICON_TEXT,
    _enumerate_attachment_names,
    _parse_fragment_to_element,
    parse_cfx_with_diagnostics,
    parse_cfx_with_loss_locations,
)
from cfxmark.parsers.jira_wiki import parse_jira_wiki
from cfxmark.parsers.md import parse_md
from cfxmark.presentation import (
    DEFAULT_COLOR_CAPABILITIES,
    STYLE_EDIT_UNSUPPORTED,
    TOPOLOGY_CHANGED,
    ColorCapabilityMatrix,
    PresentationArtifact,
    TableCellAddress,
    TableCellPresentation,
    TableTopologyAuthority,
    _iter_tables,
    apply_presentation_with_authority,
    extract_presentation,
)
from cfxmark.protected import (
    build_protected_region_proofs,
    build_protected_regions,
    build_remote_subtree_regions,
    build_table_island_regions,
    prepare_splice_markdown,
)
from cfxmark.remote_splice import apply_remote_subtree_splice, build_remote_subtrees, claim_base_remote_leaves
from cfxmark.renderers.cfx import render_cfx
from cfxmark.renderers.md import (
    MarkdownRenderOptions,
    render_md,
)
from cfxmark.source_map import (
    EditOperation,
    SemanticMappingAmbiguous,
    SemanticNode,
    SourceMapEntry,
    StorageLeafRef,
    build_candidate_source_maps,
    operation_id,
)
from cfxmark.xml_ns import AC_URI, ac_attr, ns_of, storage_semantic_path, strip_ns

_TABLE_ISLAND_NONLOCAL_CODES = frozenset({"semantic-mapping-ambiguous", "table-cell-span-overlap"})
_LOCALIZED_REMOTE_PRESERVABLE_CODES = frozenset({"empty-paragraph-dropped", "emoticon-to-unicode", "html-tag-stripped"})

# Track B (W3): a nested ``<ol start="N">`` (N != 1) that follows inline text
# inside a list item cannot be represented in Markdown -- CommonMark forbids an
# ordered list whose first number is not 1 from interrupting a paragraph, so the
# managed body would collapse it into lazy continuation text and lose the ordered
# list. This is an UNREPRESENTABLE shape that fails closed; it is named so an
# operator can fix it, never auto-migrated. The resolution is a manual remote
# edit, NOT a consent toggle: a Markdown push cannot perform it.
_REWRITE_AS_FLAT_LIST = DiagnosticResolution(
    id="rewrite-as-flat-list",
    label="Rewrite the list as a flat structure in Confluence, then re-pull",
    effect=(
        "Not auto-migrated and not resolvable by a Markdown push (--accept-migration cannot apply it): "
        "edit the page in the Confluence web UI, or an explicit storage-format workflow, to flatten the "
        "nested ordered list, then re-pull. Markdown cannot represent the original start number."
    ),
)


def _nested_ordered_list_start_loss_paths(xhtml: str) -> tuple[tuple[str, ...], ...]:
    """Storage paths of nested ``ol[start!=1]`` that collapse in managed Markdown.

    Track B (W3): an ordered list whose first number is not 1 cannot interrupt
    inline list-item text in CommonMark. When an ``<ol start="N">`` (N != 1) is
    the first child of an ``<li>`` that already carries leading inline text, the
    managed round trip cannot keep it -- ``- step\\n  2. b`` reparses as a lazy
    paragraph continuation and the ordered list is lost. This shape is
    unrepresentable and fails closed; reporting it names the loss so an operator
    can rewrite the list flat in the remote. Only this exact shape is reported: a
    first-child-only nested ordered list (no leading text) round-trips and is
    excluded, and a paragraph-wrapped body is a distinct normalization left to its
    own diagnostic.
    """

    root = _parse_fragment_to_element(xhtml)
    paths: list[tuple[str, ...]] = []
    for element in root.iterdescendants():
        if not isinstance(element.tag, str) or strip_ns(element.tag) != "ol":
            continue
        start = element.get("start")
        if start is None or start == "1":
            continue
        parent = element.getparent()
        if parent is None or not isinstance(parent.tag, str) or strip_ns(parent.tag) != "li":
            continue
        # The ol must be the first child of the li (no preceding element sibling
        # that would compose another migration) and the li must carry leading
        # inline text -- exactly the shape the atomic rewrite reproduces.
        if element.getprevious() is not None or not (parent.text or "").strip():
            continue
        paths.append(storage_semantic_path(root, element))
    return tuple(paths)


def _image_attachment_fingerprint(node: SemanticNode) -> str | None:
    if node.node_type != "Image":
        return None
    leaf = next(
        (item for item in node.leaves if item.field == "attribute" and item.attribute_name == "ri:filename"),
        None,
    )
    return leaf.value_fingerprint if leaf is not None else None


def _asset_transport_identity_changed(
    markdown: str,
    *,
    base_artifact: MarkdownArtifact,
    operation_to_storage: Mapping[str, tuple[StorageLeafRef, ...]],
    edit_operations: tuple[EditOperation, ...],
) -> bool:
    """Reject edits that promote converter-owned asset transport into body authority."""

    _base_body, base_bindings = strip_asset_markers(base_artifact.markdown)
    _edited_body, edited_bindings = strip_asset_markers(markdown)
    base_sources = Counter(binding.remote_src for binding in base_bindings.values())
    edited_sources = Counter(binding.remote_src for binding in edited_bindings.values())
    if edited_sources - base_sources:
        return True

    base_nodes = {node.node_id: node for node in base_artifact.semantic_nodes}
    for operation in edit_operations:
        if operation.kind != "update":
            continue
        base_node = base_nodes.get(operation.base_node_id or "")
        if base_node is None:
            continue
        base_src = _image_attachment_fingerprint(base_node)
        if base_src is None:
            continue
        edited_src = next(
            (
                leaf.value_fingerprint
                for leaf in operation_to_storage.get(operation_id(operation), ())
                if leaf.field == "attribute" and leaf.attribute_name == "ri:filename"
            ),
            None,
        )
        if edited_src != base_src:
            return True
    return False


def _ownership_diagnostic(code: str, *, source_fingerprint: str, ordinal: int) -> ConversionDiagnostic:
    if code == "full-replacement-available":
        return make_diagnostic(
            code="full-replacement-available",
            category="informational",
            severity="informational",
            message="the strict ownership proof refused this candidate; an explicitly approved full replacement is available",
            source_fingerprint=source_fingerprint,
            ordinal=ordinal,
        )
    if code == "migration-user-overlap-ambiguous":
        return make_diagnostic(
            code="migration-user-overlap-ambiguous",
            category="structural_loss",
            severity="blocking",
            message="candidate storage changes do not have exactly one source-bound owner",
            source_fingerprint=source_fingerprint,
            ordinal=ordinal,
        )
    if code == "multiple-change-owners":
        return make_diagnostic(
            code="multiple-change-owners",
            category="structural_loss",
            severity="blocking",
            message="candidate storage changes do not have exactly one source-bound owner",
            source_fingerprint=source_fingerprint,
            ordinal=ordinal,
        )
    if code == "semantic-mapping-ambiguous":
        return make_diagnostic(
            code="semantic-mapping-ambiguous",
            category="structural_loss",
            severity="blocking",
            message="candidate storage changes do not have exactly one source-bound owner",
            source_fingerprint=source_fingerprint,
            ordinal=ordinal,
        )
    if code == "semantic-source-map-incomplete":
        return make_diagnostic(
            code="semantic-source-map-incomplete",
            category="structural_loss",
            severity="blocking",
            message="candidate storage changes do not have exactly one source-bound owner",
            source_fingerprint=source_fingerprint,
            ordinal=ordinal,
        )
    return make_diagnostic(
        code="unclassified-storage-change",
        category="structural_loss",
        severity="blocking",
        message="candidate storage changes do not have exactly one source-bound owner",
        source_fingerprint=source_fingerprint,
        ordinal=ordinal,
    )


def _diagnostic_source_key(diagnostic: ConversionDiagnostic) -> tuple[object, ...]:
    """Identify one physical producer occurrence independently of pipeline stage."""

    return (
        diagnostic.code,
        diagnostic.location.semantic_path,
        diagnostic.location.heading_path,
        diagnostic.location.table_row,
        diagnostic.location.table_column,
    )


def _diagnostics_fail_closed_on_identity_collision(
    diagnostics: Sequence[ConversionDiagnostic],
    *,
    source_fingerprint: str,
) -> tuple[ConversionDiagnostic, ...]:
    """Collapse repeated pipeline views, but preserve ambiguous physical identities.

    A diagnostic may be carried through several pipeline stages and revised at
    the same physical source location.  That is one occurrence.  Reusing its
    ID for different locations is instead an ownership ambiguity: retain both
    atoms and add a fatal diagnostic so no consent can cover either silently.
    """

    locations_by_id: dict[str, set[tuple[object, ...]]] = {}
    for diagnostic in diagnostics:
        locations_by_id.setdefault(diagnostic.occurrence_id, set()).add(_diagnostic_source_key(diagnostic))
    collisions = {occurrence_id for occurrence_id, locations in locations_by_id.items() if len(locations) > 1}

    ordered: list[ConversionDiagnostic] = []
    latest_index: dict[str, int] = {}
    for diagnostic in diagnostics:
        if diagnostic.occurrence_id in collisions:
            ordered.append(diagnostic)
            continue
        previous = latest_index.get(diagnostic.occurrence_id)
        if previous is None:
            latest_index[diagnostic.occurrence_id] = len(ordered)
            ordered.append(diagnostic)
        else:
            ordered[previous] = diagnostic
    if collisions:
        ordered.append(
            make_diagnostic(
                code="semantic-mapping-ambiguous",
                category="structural_loss",
                severity="blocking",
                message=(
                    f"{len(collisions)} diagnostic occurrence identity collision(s) map to multiple source locations"
                ),
                source_fingerprint=source_fingerprint,
                location=DiagnosticLocation(("diagnostic-occurrence-collision[0]",)),
            )
        )
    return tuple(ordered)


def _validate_public_text_input(source: str) -> None:
    """Reject Unicode code points that cannot cross XML conversion boundaries."""

    for index, character in enumerate(source):
        codepoint = ord(character)
        if (
            codepoint in {0x09, 0x0A, 0x0D}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            continue
        raise ParseError(f"input contains XML-incompatible Unicode character U+{codepoint:04X} at index {index}")


def _enumerate_attachments(xhtml: str) -> tuple[str, ...]:
    """Return exact live attachment identities in document order."""

    return _enumerate_attachment_names(xhtml)


def _merge_rendered_attachments(
    document: Document,
    xhtml: str,
    native_attachments: Sequence[str],
    attachment_filename_map: Sequence[tuple[str, str]],
) -> tuple[str, ...]:
    """Merge local image transports with live opaque attachment identities."""

    native_paths = set(native_attachments)
    mapped_remote = dict(attachment_filename_map)
    native_remote_names: set[str] = set()
    for node in _iter_nodes(document):
        if not isinstance(node, Image):
            continue
        if node.src.startswith("data:"):
            if node.attachment_filename is not None:
                native_remote_names.add(node.attachment_filename)
            continue
        if node.attachment_filename is not None:
            local_path = node.src
            remote_name = node.attachment_filename
        else:
            local_path = unquote(urlsplit(node.src).path)
            remote_name = mapped_remote.get(local_path) or local_path.rsplit("/", 1)[-1]
        if local_path not in native_paths:
            continue
        native_remote_names.add(remote_name)
    merged = list(native_attachments)
    for name in _enumerate_attachments(xhtml):
        if name in native_remote_names or name in merged:
            continue
        merged.append(name)
    return tuple(merged)


def _table_paths(document: Document) -> tuple[tuple[str, ...], ...]:
    """Return table source paths in presentation traversal order."""

    paths: list[tuple[str, ...]] = []

    def visit(value: object) -> None:
        if isinstance(value, Table):
            paths.append(value.source_path)
            return
        if not isinstance(value, Node):
            return
        for dataclass_field in fields(value):
            child = getattr(value, dataclass_field.name)
            if isinstance(child, Node):
                visit(child)
            elif isinstance(child, tuple):
                for member in child:
                    if isinstance(member, Node):
                        visit(member)

    visit(document)
    return tuple(paths)


def _presentation_without_protected_tables(
    presentation: PresentationArtifact,
    base_artifact: MarkdownArtifact | None,
) -> PresentationArtifact:
    """Exclude table islands while presentation is applied to editable tables."""

    if base_artifact is None:
        return presentation
    protected_paths = {
        region.remote_node_path for region in base_artifact.protected_regions if region.kind == "table-island"
    }
    if not protected_paths:
        return presentation
    removed_table_ids = {
        authority.table_id
        for path, authority in zip(_table_paths(base_artifact.document), presentation.tables, strict=False)
        if any(path[: len(protected)] == protected for protected in protected_paths)
    }
    return PresentationArtifact(
        cells=tuple(item for item in presentation.cells if item.table_id not in removed_table_ids),
        addresses=tuple(item for item in presentation.addresses if item.table_id not in removed_table_ids),
        ambiguous_table_ids=tuple(item for item in presentation.ambiguous_table_ids if item not in removed_table_ids),
        tables=tuple(item for item in presentation.tables if item.table_id not in removed_table_ids),
    )


def _merge_protected_presentation(
    rendered: PresentationArtifact,
    base_artifact: MarkdownArtifact | None,
) -> PresentationArtifact:
    """Merge fresh editable-table state with immutable protected-table state."""

    if base_artifact is None:
        return rendered
    protected_paths = {
        region.remote_node_path for region in base_artifact.protected_regions if region.kind == "table-island"
    }
    if not protected_paths:
        return rendered
    base_paths = _table_paths(base_artifact.document)
    baseline = base_artifact.presentation
    if len(base_paths) != len(baseline.tables):
        return baseline
    protected_indices = {index for index, path in enumerate(base_paths) if path in protected_paths}
    if len(rendered.tables) != len(base_paths) - len(protected_indices):
        return baseline

    tables: list[TableTopologyAuthority] = []
    cells: list[TableCellPresentation] = []
    addresses: list[TableCellAddress] = []
    ambiguous_table_ids: list[str] = []
    rendered_index = 0
    for index in range(len(base_paths)):
        if index in protected_indices:
            source = baseline
            authority = baseline.tables[index]
        else:
            source = rendered
            authority = rendered.tables[rendered_index]
            rendered_index += 1
        tables.append(authority)
        cells.extend(item for item in source.cells if item.table_id == authority.table_id)
        addresses.extend(item for item in source.addresses if item.table_id == authority.table_id)
        if authority.table_id in source.ambiguous_table_ids:
            ambiguous_table_ids.append(authority.table_id)
    return PresentationArtifact(
        cells=tuple(cells),
        addresses=tuple(
            sorted(addresses, key=lambda item: (item.table_id, item.logical_row_index, item.logical_column_index))
        ),
        ambiguous_table_ids=tuple(ambiguous_table_ids),
        tables=tuple(tables),
    )


def _presentation_changed(
    presentation: PresentationArtifact,
    base_artifact: MarkdownArtifact | None,
) -> bool:
    """Return whether a caller requested a deferred 0.4 table-style edit."""

    if base_artifact is None:
        return False
    return presentation != base_artifact.presentation


_ALIGNMENT_STYLE_RE = re.compile(r"\s*text-align\s*:\s*(left|center|right)\s*;?\s*\Z", re.IGNORECASE)


def _markdown_authoritative_table_presentation(
    base_artifact: MarkdownArtifact,
    source_storage: str,
) -> bool:
    """Whether every remote table contains only Markdown-regenerable presentation.

    This is deliberately a type check, not a value comparison with M1.  Once a
    source table has no remote-only presentation, the Markdown author owns its
    dimensions and alignment values; no cell-to-cell attribution is needed.
    """

    tables = _iter_tables(base_artifact.document)
    if not tables or not any(
        cell.source_style is not None
        for table in tables
        for row in (table.header, *table.body)
        if row is not None
        for cell in row.cells
    ):
        return False
    source_root = _parse_fragment_to_element(source_storage)
    source_tables = [
        element for element in source_root.iter() if isinstance(element.tag, str) and strip_ns(element.tag) == "table"
    ]
    if len(source_tables) != len(tables):
        return False
    return all(
        _table_presentation_is_markdown_regenerable(table, source_table)
        for table, source_table in zip(tables, source_tables, strict=True)
    )


def _without_markdown_authoritative_table_protection(base_artifact: MarkdownArtifact) -> MarkdownArtifact:
    """Drop only table-local recovery state once Markdown owns every table."""

    table_paths = {
        tuple(table.source_path) for table in _iter_tables(base_artifact.document) if table.source_path is not None
    }
    if not table_paths:
        return base_artifact

    def is_table_local(path: tuple[str, ...]) -> bool:
        return any(path[: len(table_path)] == table_path for table_path in table_paths)

    return replace(
        base_artifact,
        protected_regions=tuple(
            region for region in base_artifact.protected_regions if not is_table_local(tuple(region.remote_node_path))
        ),
        remote_subtrees=tuple(
            subtree for subtree in base_artifact.remote_subtrees if not is_table_local(tuple(subtree.remote_node_path))
        ),
    )


def _table_presentation_is_markdown_regenerable(table: Table, source_table: ET._Element) -> bool:
    if table.header is None or any(
        descendant is not source_table and strip_ns(descendant.tag) == "table"
        for descendant in source_table.iter()
        if isinstance(descendant.tag, str)
    ):
        return False
    if any(ns_of(descendant.tag) == AC_URI for descendant in source_table.iter() if isinstance(descendant.tag, str)):
        return False
    if any(
        strip_ns(descendant.tag) in {"blockquote", "div", "hr", "li", "ol", "pre", "ul"}
        for descendant in source_table.iter()
        if isinstance(descendant.tag, str)
    ):
        return False
    if set(source_table.attrib) - {"class"}:
        return False
    if source_table.get("class") not in {None, "confluenceTable"}:
        return False

    source_sections = [child for child in source_table if isinstance(child.tag, str)]
    if [strip_ns(section.tag) for section in source_sections] != ["thead", "tbody"]:
        return False
    header_section, body_section = source_sections
    source_rows = [("header", row) for row in header_section if isinstance(row.tag, str)] + [
        ("body", row) for row in body_section if isinstance(row.tag, str)
    ]
    rows = (table.header,) + table.body
    if len(source_rows) != len(rows) or any(
        kind != ("header" if index == 0 else "body") for index, (kind, _row) in enumerate(source_rows)
    ):
        return False

    for row_index, ((kind, source_row), row) in enumerate(zip(source_rows, rows, strict=True)):
        if strip_ns(source_row.tag) != "tr" or source_row.attrib:
            return False
        source_cells = [cell for cell in source_row if isinstance(cell.tag, str)]
        expected_tag = "th" if kind == "header" else "td"
        if len(source_cells) != len(row.cells) or len(row.cells) != len(table.alignments):
            return False
        for column_index, (source_cell, cell) in enumerate(zip(source_cells, row.cells, strict=True)):
            if (
                strip_ns(source_cell.tag) != expected_tag
                or cell.colspan != 1
                or cell.rowspan != 1
                or cell.background is not None
            ):
                return False
            allowed_attributes = {"class", "style", "align"}
            if set(source_cell.attrib) - allowed_attributes:
                return False
            expected_class = "confluenceTh" if row_index == 0 else "confluenceTd"
            if source_cell.get("class") not in {None, expected_class}:
                return False
            if (align := source_cell.get("align")) is not None and align.strip().lower() not in {
                "left",
                "center",
                "right",
            }:
                return False
            if cell.align != table.alignments[column_index]:
                return False
            if cell.source_style is not None:
                match = _ALIGNMENT_STYLE_RE.fullmatch(cell.source_style)
                if match is None or match.group(1).lower() != cell.align.value:
                    return False
    return True


def _rebase_transient_semantic_nodes(
    original: tuple[SemanticNode, ...],
    transient: tuple[SemanticNode, ...],
    protected_node_ids: frozenset[str],
    *,
    rewrite_all: bool = False,
) -> tuple[SemanticNode, ...] | None:
    """Give a base transient tree the original stable IDs by exact tree path."""

    def by_tree_path(nodes: tuple[SemanticNode, ...]) -> dict[tuple[int, ...], SemanticNode] | None:
        children: dict[str | None, list[SemanticNode]] = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node)
        roots = children.get(None, [])
        if len(roots) != 1:
            return None
        result: dict[tuple[int, ...], SemanticNode] = {}

        def visit(node: SemanticNode, path: tuple[int, ...]) -> bool:
            if path in result:
                return False
            result[path] = node
            descendants = sorted(children.get(node.node_id, []), key=lambda item: item.ordinal)
            if len({item.ordinal for item in descendants}) != len(descendants):
                return False
            return all(visit(child, (*path, child.ordinal)) for child in descendants)

        return result if visit(roots[0], ()) else None

    original_by_path = by_tree_path(original)
    transient_by_path = by_tree_path(transient)
    if original_by_path is None or transient_by_path is None:
        return None
    protected_paths = {path for path, node in original_by_path.items() if node.node_id in protected_node_ids}
    if len(protected_paths) != len(protected_node_ids):
        return None
    stable_by_transient: dict[str, str] = {}
    original_node_by_transient: dict[str, SemanticNode] = {}
    for path, transient_node in transient_by_path.items():
        original_node = original_by_path.get(path)
        if original_node is None:
            return None
        inside_protected = next(
            (protected for protected in protected_paths if path[: len(protected)] == protected), None
        )
        if inside_protected is not None and path != inside_protected:
            return None
        if inside_protected is None and transient_node.node_type != original_node.node_type:
            return None
        stable_by_transient[transient_node.node_id] = original_node.node_id
        original_node_by_transient[transient_node.node_id] = original_node
    if any(
        path not in transient_by_path
        and not any(path[: len(protected)] == protected and path != protected for protected in protected_paths)
        for path in original_by_path
    ):
        return None
    rewritten_paths = {
        prefix
        for protected in protected_paths
        for length in range(len(protected) + 1)
        for prefix in (protected[:length],)
    }
    path_by_transient_id = {node.node_id: path for path, node in transient_by_path.items()}
    rebased: list[SemanticNode] = []
    for node in transient:
        path = path_by_transient_id[node.node_id]
        original_node = original_node_by_transient[node.node_id]
        if path not in rewritten_paths and not rewrite_all:
            rebased.append(original_node)
            continue
        rebased.append(
            replace(
                node,
                node_id=stable_by_transient[node.node_id],
                parent_id=stable_by_transient[node.parent_id] if node.parent_id is not None else None,
                leaves=original_node.leaves,
            )
        )
    return tuple(rebased)


def _first_changed_storage_path(
    before: Mapping[StorageLeafRef, str],
    after: Mapping[StorageLeafRef, str],
) -> tuple[str, ...] | None:
    def identity(leaf: StorageLeafRef) -> tuple[tuple[str, ...], str, str | None, int]:
        return leaf.semantic_path, leaf.field, leaf.attribute_name, leaf.occurrence_ordinal

    before_by_identity = {identity(leaf): leaf for leaf in before}
    after_by_identity = {identity(leaf): leaf for leaf in after}
    for key in sorted(set(before_by_identity) | set(after_by_identity)):
        before_leaf = before_by_identity.get(key)
        after_leaf = after_by_identity.get(key)
        if before_leaf is None or after_leaf is None or before_leaf.value_fingerprint != after_leaf.value_fingerprint:
            return key[0]
    return None


def _same_semantic_shape(left: tuple[SemanticNode, ...], right: tuple[SemanticNode, ...]) -> bool:
    def shape(node: SemanticNode) -> tuple[object, ...]:
        return (
            node.node_id,
            node.node_type,
            node.parent_id,
            node.ordinal,
            node.subtree_fingerprint,
            node.local_fingerprint,
            node.topology_fingerprint,
        )

    return tuple(map(shape, left)) == tuple(map(shape, right))


def _iter_nodes(value: object) -> tuple[Node, ...]:
    if not isinstance(value, Node):
        return ()
    found: list[Node] = [value]
    if not is_dataclass(value):
        return tuple(found)
    for item in fields(value):
        child = getattr(value, item.name)
        if isinstance(child, Node):
            found.extend(_iter_nodes(child))
        elif isinstance(child, tuple):
            for member in child:
                found.extend(_iter_nodes(member))
    return tuple(found)


def _lossless_opaque_paths(document: Document) -> frozenset[tuple[str, ...]]:
    """Return the deliberately narrow opaque nodes carried by managed Markdown.

    These nodes are represented by authenticated opaque controls rather than a
    lossy readable projection, so their parser diagnostic is not a migration
    loss.  All other opaque content remains protected/fail-closed.
    """

    return frozenset(
        node.source_path
        for node in _iter_nodes(document)
        if (
            isinstance(node, InlineOpaque)
            and (
                is_round_trippable_inline_atomic(node.raw_xml)
                or is_round_trippable_table_wrapper(node.raw_xml, node.source_path)
            )
            or isinstance(node, OpaqueBlock)
            and is_round_trippable_table_wrapper(node.raw_xml, node.source_path)
        )
    )


def _attachment_url_diagnostics(document: Document, *, source_fingerprint: str) -> tuple[ConversionDiagnostic, ...]:
    diagnostics: list[ConversionDiagnostic] = []
    for ordinal, node in enumerate(item for item in _iter_nodes(document) if isinstance(item, Image)):
        if node.attachment_filename is not None:
            continue
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", node.src):
            continue
        url = urlsplit(node.src)
        if not url.query and not url.fragment:
            continue
        diagnostics.append(
            make_diagnostic(
                code="attachment-url-components-unrenderable",
                category="content_loss",
                severity="blocking",
                message="local attachment query or fragment cannot be represented in Confluence storage",
                source_fingerprint=source_fingerprint,
                ordinal=ordinal,
            )
        )
    return tuple(diagnostics)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversionOptions:
    """Options shared by :func:`to_cfx` and :func:`to_md`.

    * ``bullet_marker`` — the Markdown bullet character to use (``-`` by
      default). Only ``-``, ``*``, and ``+`` are meaningful.
    * ``code_fence`` — the code fence characters used in Markdown
      output. Defaults to ``"```"``.
    * ``passthrough_html_comment_prefixes`` — tuple of leading-word
      prefixes (``"workflow:"``, ``"mytool:"``, …) that identify HTML
      comment blocks caller-owned on the Markdown side. Matching
      comments are preserved across ``parse_md`` / ``render_md`` as
      :class:`cfxmark.ast.PassthroughComment` nodes and silently
      dropped by :func:`to_cfx` and :func:`to_jira_wiki` (they are
      local metadata, not document content). ``cfxmark:`` prefixes
      are never eligible — cfxmark's own sentinel comments have
      dedicated handling.
    """

    bullet_marker: str = "-"
    code_fence: str = "```"
    passthrough_html_comment_prefixes: tuple[str, ...] = ()
    profile: Literal["legacy", "editable", "readable"] = "legacy"
    color_capabilities: ColorCapabilityMatrix = DEFAULT_COLOR_CAPABILITIES
    attachment_filename_map: tuple[tuple[str, str], ...] = ()

    def md_options(self) -> MarkdownRenderOptions:
        return MarkdownRenderOptions(
            bullet_marker=self.bullet_marker,
            code_fence=self.code_fence,
            profile=self.profile,
        )


DEFAULT_OPTIONS = ConversionOptions()


def _conversion_options_sha256(options: ConversionOptions) -> str:
    payload = json.dumps(asdict(options), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def resolve_storage_migrations(
    xhtml: str,
    resolutions: Mapping[str, str],
    *,
    options: ConversionOptions = DEFAULT_OPTIONS,
) -> str:
    """Apply only explicitly approved, occurrence-bound storage migrations."""

    _validate_public_text_input(xhtml)
    if not resolutions:
        return xhtml
    artifact = to_md_artifact(xhtml, options=options)
    return _resolve_storage_migrations_from_diagnostics(xhtml, artifact.diagnostics, resolutions)


def _resolve_storage_migrations_from_diagnostics(
    xhtml: str,
    diagnostics: tuple[ConversionDiagnostic, ...] | list[ConversionDiagnostic],
    resolutions: Mapping[str, str],
) -> str:
    diagnostics_by_occurrence = {item.occurrence_id: item for item in diagnostics}
    root = _parse_fragment_to_element(xhtml)
    elements_by_path = {
        storage_semantic_path(root, element): element
        for element in root.iterdescendants()
        if isinstance(element.tag, str)
    }
    replacements: list[tuple[ET._Element, str]] = []
    for occurrence_id, resolution in sorted(resolutions.items()):
        diagnostic = diagnostics_by_occurrence.get(occurrence_id)
        if diagnostic is None:
            raise ConversionError(f"migration occurrence is not present in source storage: {occurrence_id}")
        if diagnostic.code != "emoticon-to-unicode" or resolution != "unicode":
            raise ConversionError(
                f"migration resolution is not supported by exact storage resolver: {diagnostic.code}/{resolution}"
            )
        element = elements_by_path.get(diagnostic.location.semantic_path)
        if element is None or ns_of(element.tag) != AC_URI or strip_ns(element.tag) != "emoticon":
            raise ConversionError(f"migration occurrence does not identify one emoticon element: {occurrence_id}")
        name = (element.get(ac_attr("name")) or "emoticon").strip().lower()
        replacements.append((element, _EMOTICON_TEXT.get(name, f":{name}:")))

    for element, replacement in replacements:
        parent = element.getparent()
        if parent is None:
            raise ConversionError("migration occurrence has no storage parent")
        previous = element.getprevious()
        replacement_with_tail = replacement + (element.tail or "")
        if previous is None:
            parent.text = (parent.text or "") + replacement_with_tail
        else:
            previous.tail = (previous.tail or "") + replacement_with_tail
        parent.remove(element)
    return (root.text or "") + _serialize_children_exact(root)


def _storage_node_at_path(root: ET._Element, path: tuple[str, ...]) -> ET._Element | None:
    matches = [element for element in root.iterdescendants() if storage_semantic_path(root, element) == path]
    return matches[0] if len(matches) == 1 else None


def _append_at_node_position(node: ET._Element, value: str) -> None:
    if not value:
        return
    previous = node.getprevious()
    parent = node.getparent()
    if parent is None:
        raise ConversionError("migration occurrence has no storage parent")
    if previous is None:
        parent.text = (parent.text or "") + value
    else:
        previous.tail = (previous.tail or "") + value


def _drop_storage_node(node: ET._Element, *, replacement: str = "") -> None:
    parent = node.getparent()
    if parent is None:
        raise ConversionError("migration occurrence has no storage parent")
    _append_at_node_position(node, replacement + (node.tail or ""))
    node.tail = None
    parent.remove(node)


def _unwrap_storage_node(node: ET._Element) -> None:
    parent = node.getparent()
    if parent is None:
        raise ConversionError("migration occurrence has no storage parent")
    insert_at = parent.index(node)
    _append_at_node_position(node, node.text or "")
    moved = list(node)
    for child in moved:
        node.remove(child)
        parent.insert(insert_at, child)
        insert_at += 1
    tail = node.tail or ""
    node.tail = None
    parent.remove(node)
    if not tail:
        return
    if moved:
        moved[-1].tail = (moved[-1].tail or "") + tail
    elif insert_at == 0:
        parent.text = (parent.text or "") + tail
    else:
        previous = parent[insert_at - 1]
        previous.tail = (previous.tail or "") + tail


def _resolve_atomic_storage_migration(
    source_storage: str,
    diagnostics: tuple[ConversionDiagnostic, ...],
    diagnostic: ConversionDiagnostic,
) -> str | None:
    if diagnostic.code == "emoticon-to-unicode":
        return _resolve_storage_migrations_from_diagnostics(
            source_storage,
            diagnostics,
            {diagnostic.occurrence_id: "unicode"},
        )

    path = diagnostic.location.semantic_path
    leaf_field: Literal["text", "tail"] | None = None
    attribute_name: str | None = None
    if path[-1:] == ("#text[0]",):
        path = path[:-1]
        leaf_field = "text"
    elif path[-1:] == ("#tail[0]",):
        path = path[:-1]
        leaf_field = "tail"
    elif path and path[-1].startswith("@") and path[-1].endswith("[0]"):
        attribute_name = path[-1][1:-3]
        path = path[:-1]
    if not path or path[0].startswith("source["):
        return None

    root = _parse_fragment_to_element(source_storage)
    node = _storage_node_at_path(root, path)
    if node is None:
        return None

    if diagnostic.code == "xml-comment-dropped":
        if isinstance(node.tag, str):
            return None
        parent = node.getparent()
        if parent is None:
            return None
        inline_hosts = {"p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
        if isinstance(parent.tag, str) and strip_ns(parent.tag) in inline_hosts:
            _drop_storage_node(node)
        else:
            tail = node.tail or ""
            insert_at = parent.index(node)
            node.tail = None
            parent.remove(node)
            if tail.strip():
                paragraph = ET.Element("p")
                paragraph.text = tail
                parent.insert(insert_at, paragraph)
    elif diagnostic.code == "html-tag-stripped":
        if not isinstance(node.tag, str) or strip_ns(node.tag) != "span":
            return None
        _unwrap_storage_node(node)
    elif diagnostic.code == "layout-flattened":
        if not isinstance(node.tag, str) or ns_of(node.tag) != AC_URI or strip_ns(node.tag) != "layout":
            return None
        nested_layouts = [
            child
            for child in node.iterdescendants()
            if isinstance(child.tag, str)
            and ns_of(child.tag) == AC_URI
            and strip_ns(child.tag) in {"layout", "layout-section", "layout-cell"}
        ]
        for child in reversed(nested_layouts):
            _unwrap_storage_node(child)
        _unwrap_storage_node(node)
    elif diagnostic.code == "empty-paragraph-dropped":
        if (
            not isinstance(node.tag, str)
            or strip_ns(node.tag) != "p"
            or len(node)
            or "".join(map(str, node.itertext()))
        ):
            return None
        node.append(ET.Element("br"))
    elif diagnostic.code == "list-item-paragraph-unwrapped":
        if not isinstance(node.tag, str) or strip_ns(node.tag) != "p":
            return None
        parent = node.getparent()
        if parent is None or strip_ns(parent.tag) != "li" or len(parent) != 1:
            return None
        _unwrap_storage_node(node)
    elif diagnostic.code == "trailing-space-trimmed":
        if leaf_field is None:
            return None
        value = getattr(node, leaf_field)
        if value is None or not value.endswith((" ", "\t")):
            return None
        setattr(node, leaf_field, value.rstrip(" \t"))
    elif diagnostic.code == "element-attribute-omitted":
        if attribute_name is None or node.get(attribute_name) is None:
            return None
        del node.attrib[attribute_name]
    elif diagnostic.code == "table-colgroup-omitted":
        if not isinstance(node.tag, str) or strip_ns(node.tag) != "colgroup":
            return None
        _drop_storage_node(node)
    elif diagnostic.code == "table-cell-paragraph-unwrapped":
        if not isinstance(node.tag, str) or strip_ns(node.tag) != "p":
            return None
        parent = node.getparent()
        if parent is None or strip_ns(parent.tag) not in {"td", "th"} or len(parent) != 1:
            return None
        _unwrap_storage_node(node)
    elif diagnostic.code == "element-attribute-normalized":
        if attribute_name is None:
            return None
        value = node.get(attribute_name)
        if value is None:
            return None
        from cfxmark.artifacts import normalized_attribute_value

        normalized = normalized_attribute_value(attribute_name, value)
        if normalized is None or normalized == value:
            return None
        node.set(attribute_name, normalized)
    elif diagnostic.code == "table-cell-paragraphs-collapsed":
        if not isinstance(node.tag, str) or strip_ns(node.tag) not in {"td", "th"}:
            return None
        paragraphs = list(node)
        if len(paragraphs) < 2 or not all(
            isinstance(child.tag, str) and strip_ns(child.tag) == "p" for child in paragraphs
        ):
            return None
        for paragraph in paragraphs[:-1]:
            paragraph.addnext(ET.Element("br"))
        for paragraph in paragraphs:
            _unwrap_storage_node(paragraph)
    elif diagnostic.code == "body-only-table-header-synthesized":
        if not isinstance(node.tag, str) or strip_ns(node.tag) != "table":
            return None
        children = [child for child in node if isinstance(child.tag, str)]
        if any(strip_ns(child.tag) == "thead" for child in children):
            return None
        tbody = next((child for child in children if strip_ns(child.tag) == "tbody"), None)
        if tbody is None or len(tbody) == 0:
            return None
        first_row = tbody[0]
        if not isinstance(first_row.tag, str) or strip_ns(first_row.tag) != "tr":
            return None
        cells = [cell for cell in first_row if isinstance(cell.tag, str)]
        if not cells or any(strip_ns(cell.tag) != "th" for cell in cells):
            return None
        thead = ET.Element("thead")
        insert_at = node.index(tbody)
        tbody.remove(first_row)
        first_row.tail = None
        thead.append(first_row)
        node.insert(insert_at, thead)
    else:
        return None
    return (root.text or "") + _serialize_children_exact(root)


# Composition order for the composite-effect witness: migrations that keep
# every semantic path stable run first, node unwraps next, node drops after
# them, and node moves/restructures (whose application would shift sibling
# paths used by the other resolvers) run last.
_ATOMIC_COMPOSE_ORDER: dict[str, int] = {
    "element-attribute-omitted": 0,
    "element-attribute-normalized": 0,
    "table-cell-paragraphs-collapsed": 1,
    "trailing-space-trimmed": 0,
    "emoticon-to-unicode": 1,
    "list-item-paragraph-unwrapped": 1,
    "table-cell-paragraph-unwrapped": 1,
    "html-tag-stripped": 2,
    "xml-comment-dropped": 2,
    "empty-paragraph-dropped": 2,
    "table-colgroup-omitted": 2,
    "layout-flattened": 3,
    "body-only-table-header-synthesized": 3,
}


def _compose_atomic_migrations(
    source_storage: str,
    diagnostics: tuple[ConversionDiagnostic, ...],
) -> tuple[str, tuple[str, ...]]:
    """Apply every witnessable migration cumulatively, in path-stability order.

    A resolver that no longer matches (its path was consumed by an earlier
    migration) is skipped: the leaves it would have explained stay unowned and
    the proof still fails closed on them.
    """

    storage = source_storage
    applied: list[str] = []
    ordered = sorted(
        (diagnostic for diagnostic in diagnostics if diagnostic.code in _ATOMIC_COMPOSE_ORDER),
        key=lambda diagnostic: (_ATOMIC_COMPOSE_ORDER[diagnostic.code], diagnostic.location.semantic_path),
    )
    for diagnostic in ordered:
        try:
            resolved = _resolve_atomic_storage_migration(storage, diagnostics, diagnostic)
        except ConversionError:
            # An earlier migration consumed this resolver's path (e.g. a second
            # emoticon in the same paragraph after the first was resolved).
            # Skipping keeps the affected leaves unowned and fail-closed.
            resolved = None
        if resolved is None:
            continue
        storage = resolved
        applied.append(diagnostic.occurrence_id)
    return storage, tuple(applied)


def _bind_exact_migration_occurrences(
    diagnostics: list[ConversionDiagnostic],
    storage_to_node: dict[StorageLeafRef, str],
    node_to_storage: dict[str, tuple[StorageLeafRef, ...]],
    source_map_entries: tuple[SourceMapEntry, ...],
    unmapped_storage: tuple[StorageLeafRef, ...],
) -> tuple[
    list[ConversionDiagnostic],
    dict[StorageLeafRef, str],
    dict[str, tuple[StorageLeafRef, ...]],
    tuple[SourceMapEntry, ...],
    tuple[StorageLeafRef, ...],
]:
    """Bind occurrence diagnostics only when construct ownership is unique.

    Every emitted source-map entry contains only leaves at or below the exact
    construct path recorded on the corresponding diagnostic.
    """

    remaining = list(unmapped_storage)
    migration_entries: list[SourceMapEntry] = []
    existing_occurrence_ids = {
        entry.node_or_occurrence_id for entry in source_map_entries if entry.source_kind == "migration"
    }
    existing_migration_leaves = {
        leaf for entry in source_map_entries if entry.source_kind == "migration" for leaf in entry.leaves
    }
    claimed: dict[StorageLeafRef, str] = {}
    path_counts: dict[tuple[str, ...], int] = {}
    candidates = [
        diagnostic
        for diagnostic in diagnostics
        if effect_for_code(diagnostic.code) in {"converted", "normalized", "removed", "unsupported"}
        and any(resolution.id in {"unicode", "manual-replacement"} for resolution in diagnostic.resolutions)
    ]
    for diagnostic in candidates:
        path = diagnostic.location.semantic_path
        path_counts[path] = path_counts.get(path, 0) + 1
    all_leaves = tuple(dict.fromkeys((*storage_to_node, *remaining)))
    for diagnostic in candidates:
        if diagnostic.occurrence_id in existing_occurrence_ids:
            continue
        path = diagnostic.location.semantic_path
        if path_counts[path] != 1 or (path and path[0].startswith("source[")):
            continue
        leaf_path = path[:-1] if path[-1:] in {("#text[0]",), ("#tail[0]",)} else path
        leaf_field = "text" if path[-1:] == ("#text[0]",) else "tail" if path[-1:] == ("#tail[0]",) else None
        attribute_name: str | None = None
        if path and path[-1].startswith("@") and path[-1].endswith("[0]"):
            # Attribute-scoped occurrence: claim exactly that one attribute leaf.
            leaf_path = path[:-1]
            attribute_name = path[-1][1:-3]
        leaves = tuple(
            leaf
            for leaf in all_leaves
            if (
                (
                    attribute_name is not None
                    and leaf.semantic_path == leaf_path
                    and leaf.field == "attribute"
                    and leaf.attribute_name == attribute_name
                )
                or (
                    attribute_name is None
                    and leaf.semantic_path[: len(leaf_path)] == leaf_path
                    and (leaf_field is None or (leaf.semantic_path == leaf_path and leaf.field == leaf_field))
                )
            )
            and leaf not in existing_migration_leaves
            and leaf not in claimed
        )
        if not leaves:
            continue
        migration_entries.append(
            SourceMapEntry(
                diagnostic.occurrence_id,
                "migration",
                diagnostic.occurrence_id,
                leaves,
            )
        )
        for leaf in leaves:
            claimed[leaf] = diagnostic.occurrence_id
            storage_to_node[leaf] = diagnostic.occurrence_id
    claimed_leaves = set(claimed)
    remaining = [leaf for leaf in remaining if leaf not in claimed_leaves]
    emoticons_by_parent: dict[tuple[str, ...], list[str]] = {}
    for diagnostic in diagnostics:
        if diagnostic.code == "emoticon-to-unicode" and diagnostic.location.semantic_path:
            emoticons_by_parent.setdefault(diagnostic.location.semantic_path[:-1], []).append(diagnostic.occurrence_id)
    for leaf in tuple(remaining):
        occurrence_ids = emoticons_by_parent.get(leaf.semantic_path)
        if leaf.field != "text" or not occurrence_ids:
            continue
        payload = "\0".join(("cfxmark-emoticon-text-owner-v1", *sorted(occurrence_ids)))
        owner = "migration-coalesced-" + hashlib.sha256(payload.encode()).hexdigest()
        migration_entries.append(SourceMapEntry(owner, "migration", owner, (leaf,)))
        storage_to_node[leaf] = owner
        remaining.remove(leaf)
    retained_entries = tuple(
        replace(entry, leaves=tuple(leaf for leaf in entry.leaves if leaf not in claimed_leaves))
        for entry in source_map_entries
    )
    rebuilt: dict[str, list[StorageLeafRef]] = {}
    for leaf, owner in storage_to_node.items():
        rebuilt.setdefault(owner, []).append(leaf)
    node_to_storage = {
        owner: tuple(
            sorted(
                leaves,
                key=lambda leaf: (leaf.semantic_path, leaf.field, leaf.attribute_name or "", leaf.occurrence_ordinal),
            )
        )
        for owner, leaves in rebuilt.items()
    }
    return (
        diagnostics,
        storage_to_node,
        node_to_storage,
        (*retained_entries, *migration_entries),
        tuple(remaining),
    )


_UNSUPPORTED_MARKER_RE = re.compile(r"<!--\s*cfxmark:unsupported\b(?P<attrs>.*?)-->", re.IGNORECASE | re.DOTALL)


def _bind_placeholder_occurrences(markdown: str, diagnostics: list[ConversionDiagnostic]) -> str:
    occurrences = iter(
        diagnostic.occurrence_id for diagnostic in diagnostics if diagnostic.code == "unsupported-confluence-content"
    )

    def annotate(match: re.Match[str]) -> str:
        occurrence_id = next(occurrences, None)
        if occurrence_id is None or re.search(r"\boccurrence=", match.group("attrs")):
            return match.group(0)
        return match.group(0).replace(" push-safe=false", f' occurrence="{occurrence_id}" push-safe=false')

    return _UNSUPPORTED_MARKER_RE.sub(annotate, markdown)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of a single conversion call.

    * ``xhtml`` — present on ``to_cfx`` results. ``None`` otherwise.
    * ``markdown`` — present on ``to_md`` results. ``None`` otherwise.
    * ``attachments`` — list of local file references emitted during
      Markdown → Confluence conversion. The caller is responsible for
      uploading these via the Confluence REST API before pushing the
      page body. ``None`` for ``to_md``.
    * ``warnings`` — human-readable messages describing anything the
      converter could not represent exactly (dropped HTML comments,
      escalated inline unknowns, …).
    * ``document`` — the intermediate cfxmark AST.
    """

    xhtml: str | None = None
    markdown: str | None = None
    attachments: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    document: Document | None = None
    jira_wiki: str | None = None
    push_safe: bool = True
    losses: tuple[str, ...] = field(default_factory=tuple)
    diagnostics: tuple[ConversionDiagnostic, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Markdown → Confluence
# ---------------------------------------------------------------------------


def to_cfx(
    markdown: str,
    *,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Markdown to Confluence Storage Format XHTML.

    :param markdown: Markdown source text.
    :param options: Conversion options. Defaults to
        :data:`DEFAULT_OPTIONS`. Only
        :attr:`ConversionOptions.passthrough_html_comment_prefixes`
        affects the ``markdown → cfx`` direction — bullet marker and
        code fence settings are no-ops here because Confluence storage
        XHTML has no Markdown style choices to make.
    :param macros: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``xhtml``,
        ``attachments``, ``warnings`` and ``document`` populated.
    """

    _validate_public_text_input(markdown)
    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    document, parse_warnings = parse_md(
        markdown,
        registry=registry,
        passthrough_html_comment_prefixes=opts.passthrough_html_comment_prefixes,
    )
    xhtml, native_attachments, render_warnings = render_cfx(
        document,
        registry=registry,
        attachment_filename_map=opts.attachment_filename_map,
    )
    warnings = tuple(parse_warnings) + tuple(render_warnings)
    diagnostics = diagnostics_from_messages(warnings=warnings, losses=(), source=markdown)
    blocking = tuple(item for item in diagnostics if item.blocking)
    # Native images preserve their *original* local path (which may
    # contain directory prefixes like ``assets/img.png``) so the caller
    # knows where to read bytes from. Opaque blocks contain byte-
    # preserved XML that only exposes the Confluence basename; we
    # merge those in without duplicating natives already listed.
    return ConversionResult(
        xhtml=xhtml,
        markdown=None,
        attachments=_merge_rendered_attachments(
            document,
            xhtml,
            native_attachments,
            opts.attachment_filename_map,
        ),
        warnings=tuple(parse_warnings) + tuple(render_warnings),
        document=document,
        push_safe=not blocking,
        losses=tuple(item.message for item in blocking),
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Confluence → Markdown
# ---------------------------------------------------------------------------


def to_md(
    xhtml: str,
    *,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Confluence Storage Format XHTML to Markdown.

    :param xhtml: Confluence storage XHTML fragment (the body of a
        page, without the XML declaration or surrounding document
        structure).
    :param options: Conversion options. Defaults to :data:`DEFAULT_OPTIONS`.
    :param macros: Macro registry to use. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``markdown``,
        ``warnings`` and ``document`` populated.
    """

    _validate_public_text_input(xhtml)
    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    document, parse_warnings, located_losses = parse_cfx_with_loss_locations(
        xhtml, registry=registry, color_capabilities=opts.color_capabilities
    )
    lossless_opaque_paths = _lossless_opaque_paths(document)
    parse_losses = tuple(loss.message for loss in located_losses if loss.semantic_path not in lossless_opaque_paths)
    markdown = render_md(document, options=opts.md_options())
    has_unsupported = _contains_opaque(document, allowed_paths=lossless_opaque_paths)
    losses: tuple[str, ...]
    if opts.profile == "readable":
        losses = ("readable Markdown is not intended for round-trip publishing",) + tuple(parse_losses)
    elif has_unsupported:
        losses = ("unsupported Confluence content omitted from editable Markdown",) + tuple(parse_losses)
    elif parse_losses:
        losses = tuple(parse_losses)
    else:
        losses = ()
    if opts.profile == "editable" and any(cell.background is not None for cell in extract_presentation(document).cells):
        losses += ("table cell background is not represented by the string-only editable API",)
    diagnostics = diagnostics_from_messages(
        warnings=tuple(parse_warnings),
        losses=losses,
        source=xhtml,
    )
    push_safe = (
        opts.profile != "readable"
        and not (opts.profile == "editable" and has_unsupported)
        and not any(item.blocking for item in diagnostics)
    )
    return ConversionResult(
        xhtml=None,
        markdown=markdown,
        attachments=_enumerate_attachments(xhtml),
        warnings=tuple(parse_warnings),
        document=document,
        push_safe=push_safe,
        losses=losses,
        diagnostics=diagnostics,
    )


def to_md_artifact(
    xhtml: str,
    *,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> MarkdownArtifact:
    """Convert storage to managed Markdown plus typed external artifacts."""

    _validate_public_text_input(xhtml)
    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    document, parse_warnings, located_losses = parse_cfx_with_loss_locations(
        xhtml, registry=registry, color_capabilities=opts.color_capabilities
    )
    parse_losses = [loss.message for loss in located_losses]
    markdown = render_md(document, options=replace(opts.md_options(), protected_projections=True))
    presentation = extract_presentation(document)
    semantic_nodes, storage_to_node, node_to_storage, source_map_entries, unmapped_storage = base_maps(
        document,
        xhtml,
        registry=registry,
    )
    parse_diagnostics = list(
        diagnostics_from_messages(
            warnings=tuple(parse_warnings),
            losses=tuple(parse_losses),
            source=xhtml,
            loss_locations=tuple(DiagnosticLocation(loss.semantic_path) for loss in located_losses),
        )
    )
    lossless_opaque_paths = _lossless_opaque_paths(document)
    if lossless_opaque_paths:
        parse_diagnostics = [
            item
            for item in parse_diagnostics
            if not (
                item.code == "unsupported-confluence-content" and item.location.semantic_path in lossless_opaque_paths
            )
        ]
    # Track B (W3): name each nested ordered list whose start number cannot be
    # represented, before the body is published as ordinary Markdown, so the
    # unrepresentable shape fails closed with an actionable diagnostic instead of a
    # bare unclassified loss. This does NOT make the shape consentable or
    # migration-owned -- it remains fatal and requires a manual remote rewrite.
    for ordinal, loss_path in enumerate(_nested_ordered_list_start_loss_paths(xhtml)):
        parse_diagnostics.append(
            make_diagnostic(
                code="nested-ordered-list-start-unrepresentable",
                category="structural_loss",
                severity="blocking",
                message="a nested ordered list whose first number is not 1 cannot be represented in Markdown",
                source_fingerprint=preservation_signature(xhtml),
                ordinal=ordinal,
                location=DiagnosticLocation(loss_path),
                resolutions=(_REWRITE_AS_FLAT_LIST,),
            )
        )
    # One atom per styled cell. A single diagnostic carrying a count would ask
    # the user to approve N distinct losses with one decision, and leave the
    # report unable to say which cells they were.
    styled_cells = [cell for cell in presentation.cells if cell.background is not None]
    for ordinal, cell in enumerate(styled_cells):
        parse_diagnostics.append(
            make_diagnostic(
                code="table-cell-background-omitted",
                category="informational",
                severity="informational",
                # The managed Markdown does not carry the colour, so describing
                # it as "preserved" invites the reader to assume publishing is
                # harmless. It survives only because the remote page still holds
                # it; a full migration publish drops it.
                message=(
                    "Markdown cannot express this table-cell background; a source-bound candidate "
                    "reattaches the exact remote presentation value"
                ),
                source_fingerprint=preservation_signature(xhtml),
                ordinal=ordinal,
                # Scoped by the table's content-derived identity rather than its
                # position, so reordering tables does not silently re-point an
                # occurrence at a different table. Tables whose identity collides
                # are reported through ambiguous_table_ids, which is fatal.
                location=DiagnosticLocation(
                    semantic_path=(f"table:{cell.table_id}",),
                    table_row=cell.anchor_row_index,
                    table_column=cell.anchor_column_index,
                ),
            )
        )
    parse_diagnostics = list(
        _diagnostics_fail_closed_on_identity_collision(
            parse_diagnostics,
            source_fingerprint=preservation_signature(xhtml),
        )
    )
    (
        parse_diagnostics,
        storage_to_node,
        node_to_storage,
        source_map_entries,
        unmapped_storage,
    ) = _bind_exact_migration_occurrences(
        parse_diagnostics,
        storage_to_node,
        node_to_storage,
        source_map_entries,
        unmapped_storage,
    )
    semantic_nodes = tuple(replace(node, leaves=node_to_storage.get(node.node_id, ())) for node in semantic_nodes)
    markdown = _bind_placeholder_occurrences(markdown, parse_diagnostics)
    remote_subtrees = build_remote_subtrees(
        document,
        markdown,
        semantic_nodes,
        xhtml,
        md_options=replace(opts.md_options(), protected_projections=True),
        registry=registry,
    )
    table_regions = build_table_island_regions(document, markdown, remote_subtrees)
    table_paths = tuple(region.remote_node_path for region in table_regions)
    table_ranges = tuple(region.base_markdown_range for region in table_regions)
    remote_regions = build_remote_subtree_regions(
        document,
        markdown,
        remote_subtrees,
        excluded_node_paths=table_paths,
    )
    remote_region_paths = tuple(region.remote_node_path for region in remote_regions)
    remote_region_ranges = tuple(region.base_markdown_range for region in remote_regions)
    protected_block_paths = (*table_paths, *remote_region_paths)
    protected_block_ranges = (*table_ranges, *remote_region_ranges)
    opaque_regions = build_protected_regions(
        document,
        markdown,
        parse_diagnostics,
        excluded_node_paths=protected_block_paths,
        excluded_markdown_ranges=protected_block_ranges,
    )
    protected_regions = tuple(
        sorted(
            (
                *(
                    region
                    for region in opaque_regions
                    if not any(region.remote_node_path[: len(path)] == path for path in protected_block_paths)
                ),
                *table_regions,
                *remote_regions,
            ),
            key=lambda region: (region.base_markdown_range.start, region.base_markdown_range.end),
        )
    )
    if protected_block_paths:
        parse_diagnostics = [
            item
            for item in parse_diagnostics
            if not (
                item.blocking
                and item.code not in _TABLE_ISLAND_NONLOCAL_CODES
                and item.location.semantic_path != ("source[0]",)
                and any(item.location.semantic_path[: len(path)] == path for path in protected_block_paths)
            )
        ]
    (
        semantic_nodes,
        storage_to_node,
        node_to_storage,
        source_map_entries,
        unmapped_storage,
    ) = claim_base_remote_leaves(
        semantic_nodes,
        storage_to_node,
        node_to_storage,
        source_map_entries,
        unmapped_storage,
        remote_subtrees,
    )
    base_macro_identities = macro_identities(document)
    protected_occurrences = {region.occurrence_id for region in protected_regions}
    if protected_regions:
        parse_diagnostics = [
            item
            for item in parse_diagnostics
            if not (item.code == "unsupported-confluence-content" and item.occurrence_id in protected_occurrences)
        ]
    if any(not table.header_present for table in presentation.tables):
        parse_diagnostics = [item for item in parse_diagnostics if item.code != "body-only-table-header-synthesized"]
    if presentation.tables:
        parse_diagnostics = [item for item in parse_diagnostics if item.code != "table-column-alignment-mixed"]
    provisional = MarkdownArtifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        status="needs_migration",
        markdown=markdown,
        presentation=presentation,
        diagnostics=tuple(parse_diagnostics),
        document=document,
        push_safe=False,
        base_storage_to_node=storage_to_node,
        base_node_to_storage=node_to_storage,
        semantic_nodes=semantic_nodes,
        source_map_entries=source_map_entries,
        unmapped_storage_leaves=unmapped_storage,
        source_storage_sha256=hashlib.sha256(xhtml.encode()).hexdigest(),
        attachments=_enumerate_attachments(xhtml),
        protected_regions=protected_regions,
        remote_subtrees=remote_subtrees,
        macro_identities=base_macro_identities,
    )
    candidate = to_cfx_artifact(
        markdown,
        presentation=presentation,
        base_artifact=provisional,
        splice_source=xhtml,
        options=opts,
        macros=registry,
        _skip_markdown_authoritative_tables=True,
    )
    preserved_subtree_ids = set(candidate.preserved_subtrees)
    preserved_subtree_paths = tuple(
        subtree.remote_node_path for subtree in remote_subtrees if subtree.node_id in preserved_subtree_ids
    )
    parse_diagnostics = [
        replace(
            item,
            severity="informational",
            message=f"{item.message}; exact remote subtree preserved while unchanged",
        )
        if item.code in _LOCALIZED_REMOTE_PRESERVABLE_CODES
        and item.location.semantic_path != ("source[0]",)
        and any(item.location.semantic_path[: len(path)] == path for path in preserved_subtree_paths)
        else item
        for item in parse_diagnostics
    ]
    automatic_resolutions = {
        item.occurrence_id: "unicode"
        for item in parse_diagnostics
        if item.code == "emoticon-to-unicode" and item.blocking
    }
    expected_storage = _resolve_storage_migrations_from_diagnostics(
        xhtml,
        parse_diagnostics,
        automatic_resolutions,
    )
    diagnostics = parse_diagnostics
    managed_roundtrip_changed = False
    if candidate is not None:
        diagnostics.extend(collateral_diagnostics(expected_storage, candidate.xhtml))
        managed_roundtrip_changed = bool(candidate.edit_operations)
        managed_roundtrip_changed = managed_roundtrip_changed or preservation_signature(
            expected_storage
        ) != preservation_signature(candidate.xhtml)
    parse_has_blocker = any(item.blocking for item in diagnostics)
    diagnostics.extend(
        item for item in candidate.diagnostics if not (parse_has_blocker and item.code == "semantic-mapping-ambiguous")
    )
    if unmapped_storage:
        diagnostics.append(
            make_diagnostic(
                code="semantic-source-map-incomplete",
                category="structural_loss",
                severity="blocking",
                message="one or more storage leaves have no unique semantic owner",
                source_fingerprint=preservation_signature(xhtml),
            )
        )
    ownership_codes = {
        "unclassified-storage-change",
        "multiple-change-owners",
        "migration-user-overlap-ambiguous",
    }
    unexplained_blocker = any(
        item.blocking and item.occurrence_id not in automatic_resolutions and item.code not in ownership_codes
        for item in diagnostics
    )
    if managed_roundtrip_changed and not unexplained_blocker:
        _expected_nodes, expected_storage_to_node, _expected_node_to_storage, _expected_entries, _expected_unmapped = (
            base_maps(
                document,
                expected_storage,
                registry=registry,
            )
        )
        changed_path = _first_changed_storage_path(
            expected_storage_to_node,
            candidate.candidate_storage_to_operation,
        )
        diagnostics.append(
            make_diagnostic(
                code="managed-base-roundtrip-changed",
                category="structural_loss",
                severity="blocking",
                message="generated managed Markdown changes storage without a user edit",
                source_fingerprint=preservation_signature(xhtml),
                location=DiagnosticLocation(changed_path or ("source[0]",)),
            )
        )
    ordered = tuple(
        sorted(
            _diagnostics_fail_closed_on_identity_collision(
                diagnostics,
                source_fingerprint=preservation_signature(xhtml),
            ),
            key=lambda item: item.occurrence_id,
        )
    )
    push_safe = opts.profile != "readable" and not any(item.blocking for item in ordered)
    report = MigrationReport.build(
        occurrences=occurrences_from_diagnostics(
            ordered,
            source_storage_sha256=hashlib.sha256(xhtml.encode()).hexdigest(),
        )
    )
    return MarkdownArtifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        status="ready_to_edit" if push_safe else "needs_migration",
        markdown=markdown,
        disclosure=_disclose(report),
        presentation=presentation,
        diagnostics=ordered,
        document=document,
        push_safe=push_safe,
        base_storage_to_node=storage_to_node,
        base_node_to_storage=node_to_storage,
        semantic_nodes=semantic_nodes,
        source_map_entries=source_map_entries,
        unmapped_storage_leaves=unmapped_storage,
        source_storage_sha256=hashlib.sha256(xhtml.encode()).hexdigest(),
        attachments=_enumerate_attachments(xhtml),
        protected_regions=protected_regions,
        remote_subtrees=remote_subtrees,
        macro_identities=base_macro_identities,
    )


def _disclose(report: MigrationReport) -> str:
    """Render the control-comment header describing what a push would do.

    Returned separately from the body rather than glued onto it. Every offset
    the artifact carries -- protected region ranges, source map entries, remote
    subtree ranges -- indexes `markdown`, so prefixing that field would leave
    each of those ranges pointing at the wrong bytes while still looking
    correct. The body stays the thing offsets mean; `managed_markdown` composes
    the two for the file a user actually edits.

    The notice is unconditional. A summary that appeared only on lossy pages
    would make its absence ambiguous: unmanaged document, or managed one with
    nothing to report? The summary and markers are conditional precisely because
    the notice is not.
    """
    lines = [render_notice()]
    if report.occurrences:
        lines.append(render_migrations_summary(report))
    return "\n".join(lines)


def _serializer_ancestor_node_ids(
    edit_operations: tuple[EditOperation, ...],
    source_map_entries: tuple[SourceMapEntry, ...],
) -> tuple[str, ...]:
    """Ancestor node ids a serializer must retain to place structural edits.

    Shared by the core render and the ownership-base re-derivation so both derive
    this witness identically from the same operation set.
    """

    serializer_ancestors = {
        parent_id
        for operation in edit_operations
        if operation.kind in {"insert", "delete", "move"}
        for parent_id in (operation.old_parent_id, operation.new_parent_id)
        if parent_id is not None
    }
    if any(
        operation.kind in {"insert", "delete", "move"}
        and (operation.old_parent_id is None or operation.new_parent_id is None)
        for operation in edit_operations
    ):
        serializer_ancestors.update(
            entry.node_or_occurrence_id
            for entry in source_map_entries
            if any(leaf.semantic_path == ("#fragment[0]",) for leaf in entry.leaves)
        )
    return tuple(sorted(serializer_ancestors))


def _with_operation_layer(target: CfxArtifact, source: CfxArtifact, *, c0_bound: bool) -> CfxArtifact:
    """Copy the operation layer from ``source`` onto ``target``.

    Always moves the operation-derived witnesses (edit operations, serializer
    ancestors, matched base nodes). On the C0 path it additionally moves the
    stable-id-dependent semantic nodes, source map, and unmapped leaves so the
    whole operation layer stays internally consistent -- swapping a subset would
    desync ``_user_owners``' ``entry.operation_id`` lookups. On the S0 path
    (remote splice / protected regions) only the operation-derived fields move,
    exactly as before, so the S0-node-keyed splice and storage map stay
    ``target``'s.
    """

    updated = replace(
        target,
        edit_operations=source.edit_operations,
        serializer_ancestor_node_ids=source.serializer_ancestor_node_ids,
        semantic_match_base_nodes=source.semantic_match_base_nodes,
    )
    if not c0_bound:
        return updated
    return replace(
        updated,
        semantic_nodes=source.semantic_nodes,
        source_map_entries=source.source_map_entries,
        edited_operation_to_storage=source.edited_operation_to_storage,
        candidate_storage_to_operation=source.candidate_storage_to_operation,
        unmapped_storage_leaves=source.unmapped_storage_leaves,
    )


def _to_cfx_artifact_core(
    markdown: str,
    *,
    presentation: PresentationArtifact | None = None,
    base_artifact: MarkdownArtifact | None = None,
    splice_source: str | None = None,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
    operation_base: tuple[tuple[SemanticNode, ...], tuple[SourceMapEntry, ...]] | None = None,
    markdown_authoritative_tables: bool = False,
) -> CfxArtifact:
    """Build one candidate without recursively invoking the ownership proof.

    ``operation_base`` overrides the base fed to the semantic-diff matcher only
    (nodes + source-map). When set, edit operations and their tombstones derive
    against that base -- the C0 baseline -- so user ownership is proven against
    the round-tripped state the user actually edited, while the document, rendered
    storage, macro identities, and remote-subtree splice stay bound to the S0
    ``base_artifact``. ``None`` keeps the S0-bound behaviour.
    """
    # Disclosure is cfxmark's annotation, not the user's content. Stripping it
    # first keeps every downstream offset indexed to the body, and keeps the
    # header out of what gets published. A reader who deletes the comments and
    # one who leaves them alone must produce the same candidate.
    # Asset markers are converter transport metadata consumed by ``parse_md``.
    # Keep them until that parser has recovered the original remote filename;
    # stripping them here also changes protected table byte ranges and can make
    # an unchanged table look ambiguous. Other managed control comments remain
    # outside the candidate body and are removed at this boundary.
    markdown = strip_control_comments(markdown, preserve_asset_markers=True)

    _validate_public_text_input(markdown)
    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    diagnostics: list[ConversionDiagnostic] = []
    source_fingerprint = hashlib.sha256(markdown.encode()).hexdigest()
    transient_markdown = markdown
    preserved_regions: tuple[str, ...] = ()
    required_regions: tuple[str, ...] = ()
    unspliced_regions: tuple[str, ...] = ()
    protected_region_proofs: tuple[ProtectedRegionProof, ...] = ()
    protected_region_expectations: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    protected_region_changes: tuple[ProtectedRegionChange, ...] = ()
    task_status_changes: tuple[TaskStatusChange, ...] = ()
    preserved_subtrees: tuple[str, ...] = ()
    required_subtrees: tuple[str, ...] = ()
    unspliced_subtrees: tuple[str, ...] = ()
    preserved_subtree_proofs: tuple[RemoteSubtreeProof, ...] = ()
    splice_source_matches = True
    semantic_base_nodes = base_artifact.semantic_nodes if base_artifact is not None else None
    remote_backed = bool(
        base_artifact is not None
        and (
            base_artifact.protected_regions
            or any(subtree.requires_fresh_source for subtree in base_artifact.remote_subtrees)
        )
    )
    if remote_backed:
        assert base_artifact is not None
        splice_source_matches = (
            splice_source is not None
            and hashlib.sha256(splice_source.encode()).hexdigest() == base_artifact.source_storage_sha256
        )
        if splice_source_matches and base_artifact.protected_regions:
            assert splice_source is not None
            required_regions = tuple(region.occurrence_id for region in base_artifact.protected_regions)
            fresh_document, _fresh_warnings, _fresh_losses = parse_cfx_with_loss_locations(
                splice_source,
                registry=registry,
                color_capabilities=opts.color_capabilities,
            )
            (
                transient_markdown,
                preserved_regions,
                protected_region_changes,
                task_status_changes,
                protected_region_expectations,
            ) = prepare_splice_markdown(
                markdown,
                base_markdown=base_artifact.markdown,
                fresh_document=fresh_document,
                fresh_source=splice_source,
                regions=base_artifact.protected_regions,
            )
        elif not splice_source_matches:
            diagnostics.append(
                make_diagnostic(
                    code="splice-source-mismatch",
                    category="structural_loss",
                    severity="blocking",
                    message="remote splice source does not match the managed Markdown baseline",
                    source_fingerprint=source_fingerprint,
                )
            )
        if splice_source_matches and base_artifact.remote_subtrees and not protected_region_changes:
            base_transient_markdown = base_artifact.markdown
            base_region_changes: tuple[ProtectedRegionChange, ...] = ()
            if base_artifact.protected_regions:
                assert splice_source is not None
                (
                    base_transient_markdown,
                    _base_preserved_regions,
                    base_region_changes,
                    _base_task_changes,
                    _base_expectations,
                ) = prepare_splice_markdown(
                    base_artifact.markdown,
                    base_markdown=base_artifact.markdown,
                    fresh_document=fresh_document,
                    fresh_source=splice_source,
                    regions=base_artifact.protected_regions,
                )
            if not base_region_changes:
                base_transient_document, _base_parse_warnings = parse_md(
                    base_transient_markdown,
                    registry=registry,
                    passthrough_html_comment_prefixes=opts.passthrough_html_comment_prefixes,
                )
                base_render_presentation = (
                    PresentationArtifact()
                    if markdown_authoritative_tables
                    else _presentation_without_protected_tables(presentation or PresentationArtifact(), base_artifact)
                )
                (
                    base_transient_document,
                    _base_presentation_messages,
                    base_presentation_occurrences,
                ) = apply_presentation_with_authority(base_transient_document, base_render_presentation)
                transient_base_nodes = candidate_maps(
                    base_transient_document,
                    registry=registry,
                    attachment_filename_map=opts.attachment_filename_map,
                    presentation_occurrences=base_presentation_occurrences,
                    markdown_authoritative_tables=markdown_authoritative_tables,
                )[0]
                if transient_base_nodes:
                    protected_block_region_paths = {
                        region.remote_node_path
                        for region in base_artifact.protected_regions
                        if region.kind in {"table-island", "remote-subtree"}
                    }
                    protected_node_ids = frozenset(
                        subtree.node_id
                        for subtree in base_artifact.remote_subtrees
                        if subtree.remote_node_path in protected_block_region_paths
                    )
                    assert splice_source is not None
                    fresh_storage_nodes = base_maps(
                        base_artifact.document,
                        splice_source,
                        registry=registry,
                    )[0]
                    rebased_nodes = _rebase_transient_semantic_nodes(
                        base_artifact.semantic_nodes,
                        transient_base_nodes,
                        protected_node_ids,
                        rewrite_all=_same_semantic_shape(base_artifact.semantic_nodes, fresh_storage_nodes),
                    )
                    if rebased_nodes is not None:
                        semantic_base_nodes = rebased_nodes
    for ordinal, change in enumerate(protected_region_changes):
        assert base_artifact is not None
        region = next(item for item in base_artifact.protected_regions if item.occurrence_id == change.occurrence_id)
        diagnostics.append(
            make_diagnostic(
                code="protected-region-edited",
                category="structural_loss",
                severity="blocking",
                message=(
                    f"protected {region.kind} projection was {change.kind}; "
                    f"base_range={change.base_markdown_range.start}:{change.base_markdown_range.end}; "
                    f"current_range="
                    f"{change.current_markdown_range.start if change.current_markdown_range else change.base_markdown_range.start}:"
                    f"{change.current_markdown_range.end if change.current_markdown_range else change.base_markdown_range.end}; "
                    f"next_action={change.next_action}"
                ),
                source_fingerprint=source_fingerprint,
                ordinal=ordinal,
                location=DiagnosticLocation(region.remote_node_path),
            )
        )
    document, parse_warnings = parse_md(
        transient_markdown,
        registry=registry,
        passthrough_html_comment_prefixes=opts.passthrough_html_comment_prefixes,
    )
    presentation_artifact = (
        presentation
        if presentation is not None
        else base_artifact.presentation
        if base_artifact is not None
        else PresentationArtifact()
    )
    presentation_changed = _presentation_changed(presentation_artifact, base_artifact)
    if presentation_changed:
        diagnostics.append(
            make_diagnostic(
                code="table-style-edit-not-supported",
                category="presentation_loss",
                severity="blocking",
                message="table presentation edits are deferred to cfxmark 0.6 / atls 0.4",
                source_fingerprint=source_fingerprint,
            )
        )
    effective_presentation = presentation_artifact
    render_presentation = (
        PresentationArtifact()
        if markdown_authoritative_tables
        else _presentation_without_protected_tables(effective_presentation, base_artifact)
    )
    document, presentation_messages, presentation_occurrences = apply_presentation_with_authority(
        document,
        render_presentation,
    )
    diagnostics.extend(_attachment_url_diagnostics(document, source_fingerprint=source_fingerprint))
    for ordinal, presentation_item in enumerate(presentation_messages):
        if presentation_item.message == STYLE_EDIT_UNSUPPORTED:
            code = "table-style-edit-not-supported"
        elif presentation_item.message == TOPOLOGY_CHANGED:
            code = "table-topology-changed"
        else:
            code = "table-presentation-ambiguous"
        diagnostics.append(
            make_diagnostic(
                code=code,
                category="presentation_loss",
                severity="blocking",
                message=presentation_item.message,
                source_fingerprint=source_fingerprint,
                ordinal=ordinal,
                location=DiagnosticLocation(
                    semantic_path=(f"source[{ordinal}]",),
                    table_index=presentation_item.table_ordinal,
                ),
            )
        )
    if base_artifact is None or splice_source is None:
        diagnostics.extend(
            unrenderable_background_diagnostics(
                document,
                source_fingerprint=source_fingerprint,
                string_only=False,
            )
        )
    # The operation base feeds only the semantic-diff matcher; it defaults to the
    # (possibly rebased) S0 base. A caller may substitute the C0 baseline so
    # tombstones cover leaves the round-trip introduced. Everything else stays S0.
    # Computed here, after any transient rebasing of semantic_base_nodes above.
    op_base_nodes = operation_base[0] if operation_base is not None else semantic_base_nodes
    op_base_entries = (
        operation_base[1]
        if operation_base is not None
        else (base_artifact.source_map_entries if base_artifact is not None else None)
    )
    identity_base_nodes = semantic_base_nodes or (base_artifact.semantic_nodes if base_artifact is not None else ())
    identities_applied = base_artifact is None
    try:
        if base_artifact is not None:
            document = apply_macro_identities(
                document,
                identities=base_artifact.macro_identities,
                base_nodes=identity_base_nodes,
                markdown_authoritative_tables=markdown_authoritative_tables,
            )
            identities_applied = True
        (
            semantic_nodes,
            operation_to_storage,
            storage_to_operation,
            edit_operations,
            xhtml,
            native_attachments,
            render_warnings,
            source_map_entries,
            unmapped_storage,
        ) = candidate_maps(
            document,
            base_nodes=op_base_nodes,
            base_source_map_entries=op_base_entries,
            registry=registry,
            attachment_filename_map=opts.attachment_filename_map,
            presentation_occurrences=presentation_occurrences,
            markdown_authoritative_tables=markdown_authoritative_tables,
        )
    except SemanticMappingAmbiguous as error:
        diagnostics.append(
            make_diagnostic(
                code=error.code,
                category="structural_loss",
                severity="blocking",
                message="semantic mapping is ambiguous",
                source_fingerprint=source_fingerprint,
            )
        )
        # Retried WITH the base, taking the confined proof the tie left behind.
        # Dropping the base here was the defect: a tie inside one table discarded
        # the correspondence for the whole document, and the unattributed leaves
        # that produced were counted in the thousands across the Story corpus.
        #
        # Identity carry is retried on the same terms, and only here -- reaching
        # a partial correspondence is gated on the blocking diagnostic above
        # already being recorded, so no caller can be handed identities drawn
        # from an undecided alignment without also being told the walk tied.
        # Left fail-closed, a duplicate paragraph pair anywhere stripped the
        # `macro-id` off every macro on the page, including macros the walk had
        # proven and the tie never touched.
        #
        # Only the scope-confined tie carries a `partial`; the other ambiguity
        # raises do not, and this call aligns against `identity_base_nodes`
        # while `candidate_maps` uses `op_base_nodes` -- which diverge when a C0
        # baseline is supplied. So identity carry can fail where candidate
        # generation would have succeeded, and letting that escape would turn an
        # inspectable candidate back into no candidate at all, which is the
        # regression this whole branch exists to undo. Carrying nothing is the
        # behaviour before any of this, and the blocking diagnostic is recorded
        # either way.
        if not identities_applied and base_artifact is not None:
            with suppress(SemanticMappingAmbiguous):
                document = apply_macro_identities(
                    document,
                    identities=base_artifact.macro_identities,
                    base_nodes=identity_base_nodes,
                    accept_partial_correspondence=True,
                    markdown_authoritative_tables=markdown_authoritative_tables,
                )
        (
            semantic_nodes,
            operation_to_storage,
            storage_to_operation,
            edit_operations,
            xhtml,
            native_attachments,
            render_warnings,
            source_map_entries,
            unmapped_storage,
        ) = candidate_maps(
            document,
            base_nodes=op_base_nodes,
            base_source_map_entries=op_base_entries,
            registry=registry,
            attachment_filename_map=opts.attachment_filename_map,
            presentation_occurrences=presentation_occurrences,
            markdown_authoritative_tables=markdown_authoritative_tables,
            accept_partial_correspondence=True,
        )
    if base_artifact is not None and _asset_transport_identity_changed(
        markdown,
        base_artifact=base_artifact,
        operation_to_storage=operation_to_storage,
        edit_operations=edit_operations,
    ):
        diagnostics.append(
            make_diagnostic(
                code="asset-marker-edited",
                category="structural_loss",
                severity="blocking",
                message=(
                    "converter-owned asset identity changed without a separate attachment identity proof; "
                    "keep the marker unchanged and edit the visible image destination only"
                ),
                source_fingerprint=source_fingerprint,
                location=DiagnosticLocation(("source[0]",)),
            )
        )
    if (
        splice_source_matches
        and splice_source is not None
        and base_artifact is not None
        and base_artifact.remote_subtrees
    ):
        (
            spliced_xhtml,
            spliced_entries,
            spliced_unmapped,
            preserved_subtrees,
            required_subtrees,
            unspliced_subtrees,
            preserved_subtree_proofs,
        ) = apply_remote_subtree_splice(
            xhtml,
            splice_source=splice_source,
            remote_subtrees=base_artifact.remote_subtrees,
            base_nodes=base_artifact.semantic_nodes,
            candidate_nodes=semantic_nodes,
            edit_operations=edit_operations,
            source_map_entries=source_map_entries,
        )
        if preserved_subtrees:
            xhtml = spliced_xhtml
            source_map_entries = spliced_entries
            unmapped_storage = spliced_unmapped
            operation_to_storage, storage_to_operation = build_candidate_source_maps(source_map_entries)
        if unspliced_subtrees:
            diagnostics.append(
                make_diagnostic(
                    code="remote-subtree-splice-incomplete",
                    category="structural_loss",
                    severity="blocking",
                    message=(
                        f"{len(unspliced_subtrees)} required remote subtree(s) could not be uniquely located; "
                        "refresh the baseline and retry"
                    ),
                    source_fingerprint=source_fingerprint,
                )
            )
    if required_regions:
        protected_region_proofs, missing_region_proofs = build_protected_region_proofs(
            xhtml,
            protected_region_expectations,
        )
        proven_region_ids = tuple(proof.occurrence_id for proof in protected_region_proofs)
        if len(set(required_regions)) != len(required_regions):
            unspliced_regions = required_regions
        else:
            preserved_region_ids = set(preserved_regions)
            proven_region_id_set = set(proven_region_ids)
            missing_region_id_set = set(missing_region_proofs)
            unspliced_regions = tuple(
                occurrence_id
                for occurrence_id in required_regions
                if occurrence_id not in preserved_region_ids
                or occurrence_id not in proven_region_id_set
                or occurrence_id in missing_region_id_set
            )
        if unspliced_regions:
            diagnostics.append(
                make_diagnostic(
                    code="protected-region-splice-incomplete",
                    category="structural_loss",
                    severity="blocking",
                    message=(
                        f"{len(unspliced_regions)} protected region(s) could not be proven in final storage; "
                        "refresh the baseline and retry"
                    ),
                    source_fingerprint=source_fingerprint,
                )
            )
    if base_artifact is not None:
        preserved_subtree_ids = set(preserved_subtrees)
        for item in base_artifact.diagnostics:
            if item.code not in _LOCALIZED_REMOTE_PRESERVABLE_CODES or item.blocking:
                continue
            owning_subtree = next(
                (
                    subtree
                    for subtree in base_artifact.remote_subtrees
                    if item.location.semantic_path[: len(subtree.remote_node_path)] == subtree.remote_node_path
                ),
                None,
            )
            if owning_subtree is not None and owning_subtree.node_id not in preserved_subtree_ids:
                diagnostics.append(
                    replace(
                        item,
                        severity="blocking",
                        message=f"{item.message}; the containing subtree was edited and cannot be preserved exactly",
                    )
                )
    warnings = tuple(parse_warnings) + tuple(render_warnings)
    diagnostics.extend(diagnostics_from_messages(warnings=warnings, losses=(), source=markdown))
    if unmapped_storage:
        diagnostics.append(
            make_diagnostic(
                code="semantic-source-map-incomplete",
                category="structural_loss",
                severity="blocking",
                message="one or more candidate storage leaves have no unique semantic owner",
                source_fingerprint=source_fingerprint,
            )
        )
    ordered = _diagnostics_fail_closed_on_identity_collision(
        diagnostics,
        source_fingerprint=source_fingerprint,
    )
    return CfxArtifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        xhtml=xhtml,
        candidate_storage_sha256=hashlib.sha256(xhtml.encode()).hexdigest(),
        presentation=_merge_protected_presentation(extract_presentation(document), base_artifact),
        diagnostics=ordered,
        document=document,
        push_safe=not any(item.blocking for item in ordered),
        edited_operation_to_storage=operation_to_storage,
        candidate_storage_to_operation=storage_to_operation,
        edit_operations=edit_operations,
        serializer_ancestor_node_ids=_serializer_ancestor_node_ids(edit_operations, source_map_entries),
        semantic_nodes=semantic_nodes,
        source_map_entries=source_map_entries,
        unmapped_storage_leaves=unmapped_storage,
        semantic_match_base_nodes=op_base_nodes or (),
        attachments=_merge_rendered_attachments(
            document,
            xhtml,
            native_attachments,
            opts.attachment_filename_map,
        ),
        preserved_regions=preserved_regions,
        required_regions=required_regions,
        unspliced_regions=unspliced_regions,
        protected_region_proofs=protected_region_proofs,
        preserved_subtrees=preserved_subtrees,
        required_subtrees=required_subtrees,
        unspliced_subtrees=unspliced_subtrees,
        preserved_subtree_proofs=preserved_subtree_proofs,
        protected_region_changes=protected_region_changes,
        task_status_changes=task_status_changes,
        splice_proof_complete=(
            splice_source_matches
            and not protected_region_changes
            and not unmapped_storage
            and not unspliced_regions
            and not unspliced_subtrees
            and required_regions == preserved_regions
            and tuple(proof.occurrence_id for proof in protected_region_proofs) == preserved_regions
            and required_subtrees == preserved_subtrees
        ),
    )


def to_cfx_artifact(
    markdown: str,
    *,
    presentation: PresentationArtifact | None = None,
    base_artifact: MarkdownArtifact | None = None,
    splice_source: str | None = None,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
    _skip_markdown_authoritative_tables: bool = False,
) -> CfxArtifact:
    """Convert managed Markdown and attach a source-bound ownership proof.

    The core renderer is called independently for M0 and M1.  This avoids a
    recursion guard entirely: nested baseline construction cannot leak state to
    another invocation or thread.
    """

    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    markdown_authoritative_tables = (
        not _skip_markdown_authoritative_tables
        and base_artifact is not None
        and splice_source is not None
        and _markdown_authoritative_table_presentation(base_artifact, splice_source)
    )
    effective_base = (
        _without_markdown_authoritative_table_protection(base_artifact)
        if markdown_authoritative_tables and base_artifact is not None
        else base_artifact
    )
    candidate = _to_cfx_artifact_core(
        markdown,
        presentation=presentation,
        base_artifact=effective_base,
        splice_source=splice_source,
        options=opts,
        macros=registry,
        markdown_authoritative_tables=markdown_authoritative_tables,
    )
    if effective_base is None or splice_source is None:
        return candidate

    baseline = _to_cfx_artifact_core(
        effective_base.markdown,
        presentation=effective_base.presentation,
        base_artifact=effective_base,
        splice_source=splice_source,
        options=opts,
        macros=registry,
        markdown_authoritative_tables=markdown_authoritative_tables,
    )
    # Derive the user edit operations against the C0 baseline so their tombstones
    # cover leaves the S0->C0 round-trip introduced (empty-paragraph <br>, code
    # normalization). The remote-subtree splice and protected regions are keyed to
    # S0 node ids, so documents carrying them keep the proven S0 operation base;
    # the managed full-migration path strips both, so it takes the C0 base.
    use_c0_ops = not (effective_base.remote_subtrees or effective_base.protected_regions)
    presentation_changed = _presentation_changed(
        presentation if presentation is not None else effective_base.presentation,
        effective_base,
    )
    operation_candidate = candidate
    if presentation_changed or use_c0_ops:
        operation_candidate = _to_cfx_artifact_core(
            markdown,
            presentation=effective_base.presentation if presentation_changed else presentation,
            base_artifact=effective_base,
            splice_source=splice_source,
            options=opts,
            macros=registry,
            operation_base=(baseline.semantic_nodes, baseline.source_map_entries) if use_c0_ops else None,
            markdown_authoritative_tables=markdown_authoritative_tables,
        )
    proof_candidate = _with_operation_layer(candidate, operation_candidate, c0_bound=use_c0_ops)
    supplemental_diagnostics = collateral_diagnostics(splice_source, baseline.xhtml)
    ownership_base_stream = _diagnostics_fail_closed_on_identity_collision(
        (*effective_base.diagnostics, *supplemental_diagnostics),
        source_fingerprint=preservation_signature(splice_source),
    )
    ownership_diagnostics_by_source = {
        _diagnostic_source_key(diagnostic): diagnostic for diagnostic in ownership_base_stream
    }
    ownership_base_diagnostics = tuple(ownership_diagnostics_by_source.values())
    (
        ownership_base_diagnostics_list,
        ownership_storage_to_node,
        ownership_node_to_storage,
        ownership_source_map_entries,
        ownership_unmapped_storage,
    ) = _bind_exact_migration_occurrences(
        list(ownership_base_diagnostics),
        dict(effective_base.base_storage_to_node),
        dict(effective_base.base_node_to_storage),
        tuple(effective_base.source_map_entries),
        tuple(effective_base.unmapped_storage_leaves),
    )
    ownership_semantic_nodes = tuple(
        replace(node, leaves=ownership_node_to_storage.get(node.node_id, ())) for node in effective_base.semantic_nodes
    )
    ownership_base = replace(
        effective_base,
        diagnostics=tuple(ownership_base_diagnostics_list),
        base_storage_to_node=ownership_storage_to_node,
        base_node_to_storage=ownership_node_to_storage,
        semantic_nodes=ownership_semantic_nodes,
        source_map_entries=ownership_source_map_entries,
        unmapped_storage_leaves=ownership_unmapped_storage,
    )
    # The witness and the validator re-derive the operation base from this; it must
    # match the base the operations were actually built against: C0 for the
    # migration path, the S0 migration-bound base otherwise.
    op_base_entries = baseline.source_map_entries if use_c0_ops else ownership_base.source_map_entries
    atomic_migration_storage = {
        diagnostic.occurrence_id: resolved
        for diagnostic in ownership_base.diagnostics
        if (
            resolved := _resolve_atomic_storage_migration(
                splice_source,
                ownership_base.diagnostics,
                diagnostic,
            )
        )
        is not None
    }
    ownership_presentation_authority = (
        PresentationArtifact()
        if markdown_authoritative_tables
        else _presentation_without_protected_tables(
            presentation if presentation is not None else effective_base.presentation,
            effective_base,
        )
    )
    ownership_options_sha256 = _conversion_options_sha256(opts)
    composed_migration = _compose_atomic_migrations(splice_source, ownership_base.diagnostics)
    proof = prove_candidate_ownership(
        source_storage=splice_source,
        base_artifact=ownership_base,
        baseline_candidate=baseline,
        edited_markdown=strip_control_comments(markdown, preserve_asset_markers=True),
        final_candidate=proof_candidate,
        converter=f"cfxmark/{__version__}",
        profile=opts.profile,
        atomic_migration_storage=atomic_migration_storage,
        composed_migration_storage=composed_migration if composed_migration[1] else None,
        attachment_filename_map=opts.attachment_filename_map,
        match_base_semantic_nodes=operation_candidate.semantic_match_base_nodes,
        match_base_source_map_entries=op_base_entries,
        presentation_authority=ownership_presentation_authority,
        markdown_authoritative_tables=markdown_authoritative_tables,
        options_sha256=ownership_options_sha256,
        registry=registry,
    )
    diagnostics = list(candidate.diagnostics)
    ownership_diagnostics: list[ConversionDiagnostic] = []
    existing_codes = {item.code for item in diagnostics}
    proof_codes = tuple(
        dict.fromkeys(code for code in (proof.blocking_code, *proof.fatal_diagnostic_codes) if code is not None)
    )
    for ordinal, code in enumerate(proof_codes):
        if code in existing_codes:
            continue
        ownership_diagnostic = _ownership_diagnostic(
            code,
            source_fingerprint=proof.artifacts.source_storage_sha256,
            ordinal=ordinal,
        )
        diagnostics.append(ownership_diagnostic)
        ownership_diagnostics.append(ownership_diagnostic)
    if proof_codes:
        diagnostics.append(
            _ownership_diagnostic(
                "full-replacement-available",
                source_fingerprint=proof.artifacts.source_storage_sha256,
                ordinal=len(proof_codes),
            )
        )
    ordered = _diagnostics_fail_closed_on_identity_collision(
        diagnostics,
        source_fingerprint=proof.artifacts.source_storage_sha256,
    )
    report_diagnostics = tuple(
        {
            _diagnostic_source_key(item): item
            for item in (*ownership_base.diagnostics, *candidate.diagnostics, *ownership_diagnostics)
        }.values()
    )
    source_migration_report = MigrationReport.build(
        occurrences=occurrences_from_diagnostics(
            report_diagnostics,
            source_storage_sha256=effective_base.source_storage_sha256,
        )
    )
    source_bound_candidate = replace(
        _with_operation_layer(candidate, operation_candidate, c0_bound=use_c0_ops),
        diagnostics=ordered,
        push_safe=candidate.push_safe and not proof.fatal,
        ownership_proof_required=True,
        source_storage_sha256=effective_base.source_storage_sha256,
        source_migration_report=source_migration_report,
        ownership_proof=proof,
        ownership_proof_sha256=proof.sha256(),
        ownership_final_change_count=len(proof.final_changes),
        ownership_base_source_map_entries=op_base_entries,
        ownership_base_semantic_nodes=operation_candidate.semantic_match_base_nodes,
        ownership_baseline_source_map_entries=baseline.source_map_entries,
        ownership_baseline_semantic_nodes=baseline.semantic_nodes,
        ownership_baseline_matches_source_storage=baseline.xhtml == splice_source,
        ownership_attachment_filename_map=opts.attachment_filename_map,
        ownership_remote_subtrees=effective_base.remote_subtrees,
        ownership_presentation_authority=ownership_presentation_authority,
        ownership_markdown_authoritative_tables=markdown_authoritative_tables,
        ownership_options=opts,
        ownership_options_sha256=ownership_options_sha256,
    )
    return ManagedCfxArtifact(**{item.name: getattr(source_bound_candidate, item.name) for item in fields(CfxArtifact)})


def _contains_opaque(document: Document, *, allowed_paths: frozenset[tuple[str, ...]] = frozenset()) -> bool:
    """Return whether an AST has opaque content outside authenticated transport."""

    found = False

    def visit(node: object) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, (InlineOpaque, OpaqueBlock)):
            if node.source_path in allowed_paths:
                return
            found = True
            return
        if isinstance(node, UnsupportedMarker):
            found = True
            return
        for attr in ("children", "items", "cells", "body"):
            value = getattr(node, attr, None)
            if isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)
        header = getattr(node, "header", None)
        if header is not None:
            visit(header)

    visit(document)
    return found


# ---------------------------------------------------------------------------
# Jira wiki markup
# ---------------------------------------------------------------------------

_XHTML_UNAMBIGUOUS_RE = re.compile(r'^<[^>]+xmlns(:[a-z]+)?="')


def _resolve_input_format(
    source: str,
    hint: Literal["auto", "markdown", "xhtml"],
    warnings: list[str],
) -> str:
    if hint != "auto":
        return hint
    s = source.lstrip()
    if s.startswith(("<?xml", "<!DOCTYPE", "<ac:", "<ri:")):
        return "xhtml"
    if _XHTML_UNAMBIGUOUS_RE.match(s):
        return "xhtml"
    if s.startswith("<"):
        warnings.append(
            "input_format=auto: source starts with '<' but no Confluence-specific "
            'tokens matched; defaulting to markdown. Pass input_format="markdown" '
            'or input_format="xhtml" to be explicit.'
        )
        return "markdown"
    return "markdown"


def to_jira_wiki(
    source: str,
    *,
    input_format: Literal["auto", "markdown", "xhtml"] = "auto",
    section: str | None = None,
    drop_leading_notice: tuple[re.Pattern[str], ...] = (),
    heading_promotion: Literal["confluence", "jira", "none"] = "confluence",
    code_language_map: dict[str, str] | None = None,
    options: ConversionOptions | None = None,
    macros: MacroRegistry | None = None,
) -> ConversionResult:
    """Convert Markdown or Confluence XHTML to Jira wiki markup.

    :param source: Source text (Markdown or Confluence Storage XHTML).
    :param input_format: ``"auto"`` (default), ``"markdown"``, or
        ``"xhtml"``. Auto-detect uses a narrow set of tokens to
        identify Confluence XHTML; ambiguous ``<tag>`` input defaults
        to markdown with a warning.
    :param section: If set, only the content of the first H2 section
        whose title equals this string is rendered. ``jira_wiki`` will
        be ``None`` if the section is not found.
    :param drop_leading_notice: If the first block is a paragraph
        whose flattened text matches any of these patterns, it is
        silently removed before rendering.
    :param heading_promotion: Heading level mapping policy —
        ``"confluence"`` (default, H3→``h2``, H4→``h3``, …) for
        pushing to a Confluence page where the page title occupies
        the top slot, or ``"jira"``/``"none"`` for a 1:1 identity
        mapping when pushing to a Jira issue ``description``.
    :param code_language_map: Optional mapping of code block language
        identifiers to normalized forms (e.g.
        ``{"ts": "javascript"}``). ``None`` (default) passes
        languages through unchanged.
    :param options: Conversion options. Only
        :attr:`ConversionOptions.passthrough_html_comment_prefixes`
        affects the Jira wiki pipeline — matching HTML comments on
        the Markdown side are captured as
        :class:`PassthroughComment` nodes and then dropped from the
        rendered Jira output (they are local metadata, not content).
    :param macros: Macro registry. Defaults to
        :data:`cfxmark.macros.default_registry`.
    :returns: A :class:`ConversionResult` with ``jira_wiki``,
        ``warnings`` and ``document`` populated.
    """
    from cfxmark.renderers.jira_wiki import render_jira_wiki

    _validate_public_text_input(source)
    opts = options or DEFAULT_OPTIONS
    registry = macros or default_registry
    warnings: list[str] = []
    fmt = _resolve_input_format(source, input_format, warnings)
    if fmt == "xhtml":
        document, parse_warnings, parse_losses = parse_cfx_with_diagnostics(source, registry=registry)
    else:
        document, parse_warnings = parse_md(
            source,
            registry=registry,
            passthrough_html_comment_prefixes=opts.passthrough_html_comment_prefixes,
        )
        parse_losses = []
    warnings.extend(parse_warnings)
    wiki_out, render_warnings = render_jira_wiki(
        document,
        section=section,
        drop_leading_notice=drop_leading_notice,
        heading_promotion=heading_promotion,
        code_language_map=code_language_map,
    )
    warnings.extend(render_warnings)
    return ConversionResult(
        jira_wiki=wiki_out,
        warnings=tuple(warnings),
        document=document,
        push_safe=not parse_losses,
        losses=tuple(parse_losses),
    )


# ---------------------------------------------------------------------------
# Jira wiki → Markdown (experimental, lossy)
# ---------------------------------------------------------------------------


def from_jira_wiki(
    source: str,
    *,
    options: ConversionOptions | None = None,
) -> ConversionResult:
    """Parse Jira wiki markup and re-emit it as canonical Markdown.

    **Experimental and lossy.** The Jira wiki dialect is looser and
    less formally specified than the Confluence Storage Format, and
    several inline constructs (``{color}`` colour emphasis, ``~sub~``
    / ``+ins+`` / ``^sup^``, ``{panel}`` styling) have no equivalent on
    the Markdown side and are reported through ``warnings`` on the
    returned :class:`ConversionResult`: the body reads differently but
    every word survives.

    User mentions are not in that group. ``[~alice]`` is removed
    outright, so it is reported through ``losses`` — part of the body
    is not there, and a caller deciding whether it may summarise from
    this Markdown needs that distinction rather than a warning it
    shares with a lost colour. See :mod:`cfxmark.parsers.jira_wiki`
    for the full contract.

    :param source: Raw Jira wiki markup text. Empty / ``None`` inputs
        return a :class:`ConversionResult` with an empty Markdown
        body, no warnings and no losses.
    :param options: Conversion options for the Markdown render phase.
        Style fields (``bullet_marker``, ``code_fence``) apply; the
        ``passthrough_html_comment_prefixes`` field is ignored
        because Jira wiki has no HTML comment syntax.
    :returns: A :class:`ConversionResult` with ``markdown``,
        ``warnings``, ``attachments`` and ``document`` populated.
        ``attachments`` lists every ``[^filename]`` / ``!file!``
        referenced by the source in document order (deduplicated),
        so the caller can fetch the bytes out-of-band.
    """

    opts = options or DEFAULT_OPTIONS
    if not source:
        return ConversionResult(
            markdown="",
            warnings=(),
            attachments=(),
            document=Document(children=()),
        )
    _validate_public_text_input(source)
    document, parse_warnings, parse_losses, attachments = parse_jira_wiki(source)
    markdown = render_md(document, options=opts.md_options())
    return ConversionResult(
        markdown=markdown,
        warnings=tuple(parse_warnings),
        # Reported apart from warnings because they mean different things to a
        # reader: a warning says the body reads differently, a loss says part of
        # it is not there. A caller deciding whether it may summarise from this
        # Markdown needs the second, and used to be handed only the first.
        losses=tuple(parse_losses),
        attachments=attachments,
        document=document,
    )


__all__ = [
    "ConversionOptions",
    "ConversionResult",
    "DEFAULT_OPTIONS",
    "to_cfx",
    "to_cfx_artifact",
    "to_md",
    "to_md_artifact",
    "to_jira_wiki",
    "from_jira_wiki",
]
