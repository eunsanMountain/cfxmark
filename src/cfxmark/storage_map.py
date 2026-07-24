"""Canonical Confluence storage-leaf extraction and ownership joins."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

import lxml.etree as ET

from cfxmark.parsers.cfx import _parse_fragment_to_element
from cfxmark.presentation import parse_color
from cfxmark.source_map import SourceKind, SourceMapEntry, StorageLeafField, StorageLeafRef, fingerprint_semantic_value
from cfxmark.xml_ns import storage_node_name, storage_qualified_name, strip_ns

_TABLE_CELL_STYLE_PROPERTIES = frozenset({"background-color", "text-align"})
_STYLE_PROPERTY_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9-]*\Z")
OwnerToken = tuple[str, SourceKind, str]


def _qualified_name(name: str | bytes) -> str:
    return storage_qualified_name(name)


def storage_leaf_identity(leaf: StorageLeafRef) -> tuple[tuple[str, ...], str, str | None, int]:
    """Return the value-independent identity used to join base and candidate leaves."""

    return leaf.semantic_path, leaf.field, leaf.attribute_name, leaf.occurrence_ordinal


def style_property_attribute_name(property_name: str) -> str:
    """Return the synthetic attribute identity for one safely split CSS declaration."""

    return f"style:{property_name.lower()}"


def _typed_color_fingerprint_value(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        _exact, equality_key, _value_kind, _semantic = parse_color(value)
    except ValueError:
        return value
    return equality_key


def _typed_alignment_fingerprint_value(value: str) -> str:
    normalized = value.lower()
    return normalized if normalized in {"left", "center", "right"} else value


def _split_table_cell_style(value: str | bytes) -> tuple[tuple[str, str], ...] | None:
    """Split the exact table-cell style subset that cfxmark can own independently."""

    if isinstance(value, bytes):
        value = value.decode("utf-8")
    declarations: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_declaration in value.split(";"):
        declaration = raw_declaration.strip()
        if not declaration:
            continue
        name, separator, raw_value = declaration.partition(":")
        property_name = name.strip().lower()
        property_value = raw_value.strip()
        if (
            not separator
            or not property_value
            or _STYLE_PROPERTY_NAME_RE.fullmatch(property_name) is None
            or property_name not in _TABLE_CELL_STYLE_PROPERTIES
            or property_name in seen
        ):
            return None
        seen.add(property_name)
        declarations.append((property_name, property_value))
    return tuple(declarations) or None


@dataclass
class StorageOwnership:
    """Renderer-owned leaf attribution without serializing control attributes."""

    element_owners: dict[ET._Element, OwnerToken] = field(default_factory=dict)
    leaf_owners: dict[tuple[ET._Element, str, str | None], OwnerToken] = field(default_factory=dict)
    ambiguous_leaves: set[tuple[ET._Element, str, str | None]] = field(default_factory=set)

    def register_owner(self, owner: str, source_kind: SourceKind, node_or_occurrence_id: str) -> OwnerToken:
        return owner, source_kind, node_or_occurrence_id

    def claim_element(self, element: ET._Element, owner: OwnerToken | None) -> None:
        if owner is not None:
            self.element_owners[element] = owner

    def claim_leaf(
        self,
        element: ET._Element,
        field_name: str,
        owner: OwnerToken | None,
        *,
        attribute_name: str | None = None,
    ) -> None:
        if owner is None:
            return
        if attribute_name is not None:
            attribute_name = _qualified_name(attribute_name)
        key = (element, field_name, attribute_name)
        existing = self.leaf_owners.get(key)
        if existing is not None and existing != owner:
            self.ambiguous_leaves.add(key)
            return
        self.leaf_owners[key] = owner


@dataclass(frozen=True)
class StorageExtraction:
    leaves: tuple[StorageLeafRef, ...]
    entries: tuple[SourceMapEntry, ...]
    unmapped: tuple[StorageLeafRef, ...]
    ambiguous: tuple[StorageLeafRef, ...]


def _node_name(element: ET._Element) -> str:
    return storage_node_name(element)


def _leaf(
    path: tuple[str, ...],
    field_name: StorageLeafField,
    value: str | bytes,
    *,
    attribute_name: str | None = None,
    ordinal: int = 0,
) -> StorageLeafRef:
    return StorageLeafRef(
        semantic_path=path,
        field=field_name,
        attribute_name=attribute_name,
        occurrence_ordinal=ordinal,
        value_fingerprint=fingerprint_semantic_value(value),
    )


def extract_storage(
    root: ET._Element,
    *,
    ownership: StorageOwnership | None = None,
) -> StorageExtraction:
    """Extract actual XML text/tail/attribute/element/topology leaves.

    The synthetic fragment ``root`` itself is excluded. Whitespace-only tails
    between top-level storage blocks are server serialization trivia and are
    intentionally omitted; content-bearing text and nested whitespace remain
    part of the proof.
    """

    leaves: list[StorageLeafRef] = []
    grouped: dict[OwnerToken, list[StorageLeafRef]] = defaultdict(list)
    unmapped: list[StorageLeafRef] = []
    ambiguous: list[StorageLeafRef] = []

    def emit(
        leaf: StorageLeafRef,
        element: ET._Element,
        inherited_owner: OwnerToken | None,
        field_name: str,
        attribute_name: str | None = None,
    ) -> None:
        leaves.append(leaf)
        if ownership is None:
            return
        key = (element, field_name, attribute_name)
        if key in ownership.ambiguous_leaves:
            ambiguous.append(leaf)
            return
        owner = ownership.leaf_owners.get(key, ownership.element_owners.get(element, inherited_owner))
        if owner is None:
            unmapped.append(leaf)
            return
        grouped[owner].append(leaf)

    def visit(element: ET._Element, current: tuple[str, ...], inherited_owner: OwnerToken | None) -> None:
        owner = ownership.element_owners.get(element, inherited_owner) if ownership is not None else None
        name = _node_name(element)
        emit(_leaf(current, "element", name), element, inherited_owner, "element")
        if not isinstance(element.tag, str):
            payload = element.text or ""
            if payload:
                emit(_leaf(current, "text", payload), element, owner, "text")
            return

        child_names = [_node_name(child) for child in element]
        topology = json.dumps(child_names, ensure_ascii=False, separators=(",", ":"))
        emit(_leaf(current, "topology", topology), element, owner, "topology")
        for attribute_name, value in sorted(element.attrib.items(), key=lambda item: _qualified_name(item[0])):
            canonical_name = _qualified_name(attribute_name)
            if canonical_name == "style" and name in {"td", "th"}:
                style_declarations = _split_table_cell_style(value)
                if style_declarations is not None:
                    for property_name, property_value in style_declarations:
                        synthetic_name = style_property_attribute_name(property_name)
                        style_fingerprint_value = (
                            _typed_color_fingerprint_value(property_value)
                            if property_name == "background-color"
                            else _typed_alignment_fingerprint_value(property_value)
                        )
                        emit(
                            _leaf(current, "attribute", style_fingerprint_value, attribute_name=synthetic_name),
                            element,
                            owner,
                            "attribute",
                            synthetic_name,
                        )
                    continue
            attribute_fingerprint_value: str | bytes = (
                _typed_color_fingerprint_value(value)
                if canonical_name in {"data-highlight-colour", "bgcolor"}
                else value
            )
            emit(
                _leaf(current, "attribute", attribute_fingerprint_value, attribute_name=canonical_name),
                element,
                owner,
                "attribute",
                canonical_name,
            )
        if element.text is not None:
            emit(_leaf(current, "text", element.text), element, owner, "text")

        counts: dict[str, int] = defaultdict(int)
        for child in element:
            child_name = _node_name(child)
            ordinal = counts[child_name]
            counts[child_name] += 1
            child_path = current + (f"{child_name}[{ordinal}]",)
            visit(child, child_path, owner)
            if child.tail is not None:
                emit(_leaf(child_path, "tail", child.tail), child, owner, "tail")

    root_children = tuple(root)
    root_topology = json.dumps(
        [_node_name(child) for child in root_children], ensure_ascii=False, separators=(",", ":")
    )
    emit(
        _leaf(("#fragment[0]",), "topology", root_topology),
        root,
        None,
        "topology",
    )
    if root.text is not None:
        emit(_leaf(("#fragment[0]",), "text", root.text), root, None, "text")

    counts: dict[str, int] = defaultdict(int)
    for child in root:
        name = _node_name(child)
        ordinal = counts[name]
        counts[name] += 1
        child_path = (f"{name}[{ordinal}]",)
        visit(child, child_path, None)
        if child.tail is not None and not child.tail.isspace():
            emit(_leaf(child_path, "tail", child.tail), child, None, "tail")

    entries = tuple(
        SourceMapEntry(
            owner[0],
            owner[1],
            owner[2],
            tuple(owner_leaves),
        )
        for owner, owner_leaves in sorted(grouped.items())
    )
    return StorageExtraction(tuple(leaves), entries, tuple(unmapped), tuple(ambiguous))


def _coalesce_adjacent_inline_code(root: ET._Element) -> ET._Element:
    """Merge adjacent inline ``<code>`` runs that have no separator between them.

    Markdown cannot represent two adjacent inline code spans without a
    separator (`` `a``b` `` re-parses as one span ``a``b``), so a managed
    round-trip coalesces ``<code>a</code><code>b</code>`` into
    ``<code>ab</code>``. The rendered code text is identical; only the internal
    span boundary -- which the managed Markdown format cannot maintain -- is
    lost. Canonicalizing it here (like the entity/whitespace normalization XML
    parsing already erases) keeps the ownership proof from reporting a
    boundary-only difference as a real change.
    """

    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        run_start: ET._Element | None = None
        for child in list(parent):
            # "plain" means a text-only <code> with no children AND no attributes:
            # the storage extractor treats attributes as their own proof leaves, so
            # merging an attributed span would silently drop one. Only attribute-free
            # adjacent runs are a genuine serialization equivalence.
            is_plain_code = (
                isinstance(child.tag, str) and strip_ns(child.tag) == "code" and len(child) == 0 and not child.attrib
            )
            if (
                run_start is not None
                and is_plain_code
                and not run_start.tail  # truly adjacent: nothing (not even whitespace) between them
            ):
                run_start.text = (run_start.text or "") + (child.text or "")
                run_start.tail = child.tail
                parent.remove(child)
                continue
            run_start = child if is_plain_code else None
    return root


def extract_storage_source(source: str) -> StorageExtraction:
    return extract_storage(_coalesce_adjacent_inline_code(_parse_fragment_to_element(source)))


__all__ = [
    "OwnerToken",
    "StorageExtraction",
    "StorageOwnership",
    "extract_storage",
    "extract_storage_source",
    "storage_leaf_identity",
    "style_property_attribute_name",
]
