"""Typed table presentation extraction and application."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

from cfxmark.ast import (
    CellAlign,
    CellBackground,
    ColorSourceEncoding,
    ColorValueKind,
    Document,
    Node,
    RenderStrategy,
    Table,
    TableCell,
    TableRow,
)
from cfxmark.table_grid import bounded_table_grid_layout

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_RE = re.compile(r"^(rgb|rgba)\((.*)\)$", re.IGNORECASE)
_NAMED_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_TOKEN_RE = re.compile(r"^var\((--ds-[A-Za-z0-9-]+)(?:\s*,\s*[^()]+)?\)$")
_SEMANTIC_NAMES = {"red": "red", "green": "green", "blue": "blue", "grey": "grey", "gray": "grey"}


@dataclass(frozen=True)
class ColorCapability:
    capability_id: str
    source_encoding: ColorSourceEncoding
    value_kind: ColorValueKind
    render_strategy: RenderStrategy


@dataclass(frozen=True)
class ColorCapabilityMatrix:
    capabilities: tuple[ColorCapability, ...] = ()

    def resolve(self, source_encoding: ColorSourceEncoding, value_kind: ColorValueKind) -> ColorCapability | None:
        matches = [
            item
            for item in self.capabilities
            if item.source_encoding == source_encoding and item.value_kind == value_kind
        ]
        if len(matches) != 1:
            return None
        return matches[0]


DEFAULT_COLOR_CAPABILITIES = ColorCapabilityMatrix()


@dataclass(frozen=True)
class TableCellPresentation:
    table_id: str
    topology_hash: str
    anchor_row_index: int
    anchor_column_index: int
    background: CellBackground | None
    source_style: str | None = None
    align: CellAlign = CellAlign.NONE


@dataclass(frozen=True)
class TableCellAddress:
    table_id: str
    topology_hash: str
    logical_row_index: int
    logical_column_index: int
    anchor_row_index: int
    anchor_column_index: int
    is_anchor: bool


@dataclass(frozen=True)
class TableTopologyAuthority:
    """Pulled table topology that Markdown syntax cannot express directly."""

    table_id: str
    topology_hash: str
    header_present: bool
    mixed_alignment: bool = False


@dataclass(frozen=True)
class PresentationArtifact:
    cells: tuple[TableCellPresentation, ...] = ()
    addresses: tuple[TableCellAddress, ...] = ()
    ambiguous_table_ids: tuple[str, ...] = ()
    tables: tuple[TableTopologyAuthority, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.cells or self.tables)


def _channel(value: str) -> int:
    value = value.strip()
    if value.endswith("%"):
        number = float(value[:-1])
        if not 0 <= number <= 100:
            raise ValueError("RGB percentage out of range")
        return round(number * 255 / 100)
    number = int(value)
    if not 0 <= number <= 255:
        raise ValueError("RGB channel out of range")
    return number


def _alpha(value: str) -> float:
    value = value.strip()
    number = float(value[:-1]) / 100 if value.endswith("%") else float(value)
    if not 0 <= number <= 1:
        raise ValueError("alpha channel out of range")
    return number


def parse_color(value: str) -> tuple[str, str, ColorValueKind, str | None]:
    """Return canonical exact value, equality key, value kind and semantic."""

    exact = value.strip()
    match = _HEX_RE.fullmatch(exact)
    if match:
        digits = match.group(1).lower()
        if len(digits) in {3, 4}:
            digits = "".join(character * 2 for character in digits)
        if len(digits) == 6:
            digits += "ff"
        rgba = tuple(int(digits[index : index + 2], 16) for index in range(0, 8, 2))
        alpha = rgba[3] / 255
        canonical = f"#{digits[:6]}" if rgba[3] == 255 else f"#{digits}"
        return canonical, f"rgba:{rgba[0]},{rgba[1]},{rgba[2]},{alpha:g}", "hex", None

    match = _RGB_RE.fullmatch(exact)
    if match:
        function = match.group(1).lower()
        parts = [part.strip() for part in match.group(2).split(",")]
        expected = 3 if function == "rgb" else 4
        if len(parts) != expected:
            raise ValueError("wrong RGB channel count")
        red, green, blue = (_channel(part) for part in parts[:3])
        alpha = 1.0 if expected == 3 else _alpha(parts[3])
        canonical = f"rgb({red},{green},{blue})" if alpha == 1 else f"rgba({red},{green},{blue},{alpha:g})"
        value_kind: ColorValueKind = "rgb" if function == "rgb" else "rgba"
        return canonical, f"rgba:{red},{green},{blue},{alpha:g}", value_kind, None

    if _TOKEN_RE.fullmatch(exact):
        return exact, f"token:{exact}", "design-token", None

    if _NAMED_RE.fullmatch(exact):
        canonical = exact.lower()
        return canonical, f"named:{canonical}", "named", _SEMANTIC_NAMES.get(canonical)
    raise ValueError("unsupported color grammar")


def _css_background(style: str) -> str | None:
    values: list[str] = []
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        if name.strip().lower() == "background-color":
            values.append(value.strip())
    if not values:
        return None
    if len(set(values)) != 1:
        raise ValueError("conflicting CSS background-color declarations")
    return values[0]


def parse_cell_background(
    attributes: Mapping[str, str],
    *,
    capabilities: ColorCapabilityMatrix = DEFAULT_COLOR_CAPABILITIES,
) -> tuple[CellBackground | None, tuple[str, ...]]:
    candidates: list[tuple[ColorSourceEncoding, str]] = []
    if attributes.get("data-highlight-colour"):
        candidates.append(("data-highlight-colour", attributes["data-highlight-colour"]))
    if attributes.get("bgcolor"):
        candidates.append(("bgcolor", attributes["bgcolor"]))
    if attributes.get("style"):
        try:
            css_value = _css_background(attributes["style"])
        except ValueError:
            return None, ("table cell background sources conflict",)
        if css_value:
            candidates.append(("css-background-color", css_value))
    if not candidates:
        return None, ()

    parsed: list[tuple[ColorSourceEncoding, str, str, ColorValueKind, str | None]] = []
    try:
        for source_encoding, raw in candidates:
            exact, equality_key, value_kind, semantic = parse_color(raw)
            parsed.append((source_encoding, exact, equality_key, value_kind, semantic))
    except ValueError:
        return None, ("table cell background value is invalid",)
    if len({item[2] for item in parsed}) != 1:
        return None, ("table cell background sources conflict",)

    source_encoding, exact, equality_key, value_kind, semantic = parsed[0]
    capability = capabilities.resolve(source_encoding, value_kind)
    return (
        CellBackground(
            exact=exact,
            equality_key=equality_key,
            source_encoding=source_encoding,
            value_kind=value_kind,
            semantic=semantic,
            capability_id=capability.capability_id if capability else None,
            render_strategy=capability.render_strategy if capability else None,
        ),
        (),
    )


def _iter_tables(value: Any) -> tuple[Table, ...]:
    found: list[Table] = []

    def visit(current: Any) -> None:
        if isinstance(current, Table):
            found.append(current)
            return
        if isinstance(current, Node):
            for item in fields(current):
                child = getattr(current, item.name)
                if isinstance(child, Node):
                    visit(child)
                elif isinstance(child, tuple):
                    for member in child:
                        if isinstance(member, Node):
                            visit(member)

    visit(value)
    return tuple(found)


def table_topology_hash(table: Table) -> str:
    rows = ((table.header,) if table.header is not None else ()) + table.body
    topology = [[(cell.kind.value, cell.colspan, cell.rowspan) for cell in row.cells] for row in rows]
    encoded = json.dumps(topology, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _table_base_id(table: Table) -> tuple[str, str]:
    topology_hash = table_topology_hash(table)
    rows = ((table.header,) if table.header is not None else ()) + table.body

    def identity_payload(value: Any) -> Any:
        if isinstance(value, CellBackground):
            return None
        if isinstance(value, Node):
            return {
                "type": type(value).__name__,
                "fields": {
                    dataclass_field.name: identity_payload(getattr(value, dataclass_field.name))
                    for dataclass_field in fields(value)
                    if dataclass_field.name != "background"
                },
            }
        if isinstance(value, tuple):
            return [identity_payload(item) for item in value]
        if hasattr(value, "value"):
            return value.value
        return value

    semantic_cells = identity_payload(rows)
    semantic_hash = hashlib.sha256(
        json.dumps(semantic_cells, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = hashlib.sha256(f"cfxmark-table-v2\0{topology_hash}\0{semantic_hash}".encode()).hexdigest()[:16]
    return f"table-{identity}", topology_hash


def _table_identities(tables: tuple[Table, ...]) -> tuple[tuple[str, str, bool], ...]:
    base = tuple(_table_base_id(table) for table in tables)
    counts = Counter(table_id for table_id, _topology_hash in base)
    seen: Counter[str] = Counter()
    identities: list[tuple[str, str, bool]] = []
    for table_id, topology_hash in base:
        ambiguous = counts[table_id] > 1
        if ambiguous:
            duplicate_ordinal = seen[table_id]
            seen[table_id] += 1
            table_id = f"{table_id}-duplicate-{duplicate_ordinal}"
        identities.append((table_id, topology_hash, ambiguous))
    return tuple(identities)


def _anchor_cells(table: Table) -> tuple[tuple[int, int, TableCell], ...]:
    rows: tuple[TableRow, ...] = ((table.header,) if table.header is not None else ()) + table.body
    layout = bounded_table_grid_layout(rows)
    anchors: list[tuple[int, int, TableCell]] = []
    for row_index, (row, columns) in enumerate(zip(rows, layout.anchor_columns, strict=True)):
        for cell, column_index in zip(row.cells, columns, strict=True):
            anchors.append((row_index, column_index, cell))
    return tuple(anchors)


def extract_presentation(document: Document) -> PresentationArtifact:
    cells: list[TableCellPresentation] = []
    addresses: list[TableCellAddress] = []
    ambiguous_table_ids: list[str] = []
    tables = _iter_tables(document)
    identities = _table_identities(tables)
    for table, (table_id, topology_hash, ambiguous) in zip(tables, identities, strict=True):
        if ambiguous:
            ambiguous_table_ids.append(table_id)
        for row_index, column_index, cell in _anchor_cells(table):
            for logical_row_index in range(row_index, row_index + cell.rowspan):
                for logical_column_index in range(column_index, column_index + cell.colspan):
                    addresses.append(
                        TableCellAddress(
                            table_id=table_id,
                            topology_hash=topology_hash,
                            logical_row_index=logical_row_index,
                            logical_column_index=logical_column_index,
                            anchor_row_index=row_index,
                            anchor_column_index=column_index,
                            is_anchor=(logical_row_index, logical_column_index) == (row_index, column_index),
                        )
                    )
            if cell.background is None and cell.source_style is None:
                continue
            cells.append(
                TableCellPresentation(
                    table_id=table_id,
                    topology_hash=topology_hash,
                    anchor_row_index=row_index,
                    anchor_column_index=column_index,
                    background=cell.background,
                    source_style=cell.source_style,
                    align=cell.align,
                )
            )
    return PresentationArtifact(
        cells=tuple(cells),
        addresses=tuple(
            sorted(addresses, key=lambda item: (item.table_id, item.logical_row_index, item.logical_column_index))
        ),
        ambiguous_table_ids=tuple(ambiguous_table_ids),
        tables=tuple(
            TableTopologyAuthority(
                table_id=table_id,
                topology_hash=topology_hash,
                header_present=table.header is not None,
                mixed_alignment=any(
                    len({cell.align for cell in row.cells}) > 1
                    for row in (((table.header,) if table.header is not None else ()) + table.body)
                )
                or any(alignment.value == "none" for alignment in table.alignments)
                and any(
                    cell.align.value != "none"
                    for row in (((table.header,) if table.header is not None else ()) + table.body)
                    for cell in row.cells
                ),
            )
            for table, (table_id, topology_hash, _ambiguous) in zip(tables, identities, strict=True)
        ),
    )


def presentation_cell_occurrence_id(
    table_id: str,
    topology_hash: str,
    anchor_row_index: int,
    anchor_column_index: int,
) -> str:
    """Return the content-free identity used to authorize one cell style."""

    payload = json.dumps(
        [
            "cfxmark-presentation-cell-v1",
            table_id,
            topology_hash,
            anchor_row_index,
            anchor_column_index,
        ],
        separators=(",", ":"),
    ).encode()
    return f"presentation-cell-{hashlib.sha256(payload).hexdigest()}"


def presentation_cell_occurrences(document: Document) -> dict[int, str]:
    """Map every anchor ``TableCell`` object to its typed presentation ID."""

    occurrences: dict[int, str] = {}
    tables = _iter_tables(document)
    for table, (table_id, topology_hash, _ambiguous) in zip(tables, _table_identities(tables), strict=True):
        for row_index, column_index, cell in _anchor_cells(table):
            occurrences[id(cell)] = presentation_cell_occurrence_id(
                table_id,
                topology_hash,
                row_index,
                column_index,
            )
    return occurrences


def apply_presentation_with_authority(
    document: Document,
    artifact: PresentationArtifact,
) -> tuple[Document, tuple[str, ...], dict[int, str]]:
    """Apply presentation while retaining pulled table/cell authority IDs."""

    diagnostics: list[str] = ["table presentation identity is ambiguous"] if artifact.ambiguous_table_ids else []
    candidate_tables = _iter_tables(document)
    if artifact.tables:
        if len(candidate_tables) != len(artifact.tables):
            diagnostics.append("table presentation topology changed")
        else:
            normalized_by_object: dict[int, Table] = {}
            for candidate, authority in zip(candidate_tables, artifact.tables, strict=True):
                if authority.header_present:
                    normalized_by_object[id(candidate)] = candidate
                    continue
                header = candidate.header
                if header is None or any(cell.children for cell in header.cells):
                    diagnostics.append("table presentation topology changed")
                    normalized_by_object[id(candidate)] = candidate
                    continue
                normalized = replace(candidate, header=None)
                if table_topology_hash(normalized) != authority.topology_hash:
                    diagnostics.append("table presentation topology changed")
                    normalized_by_object[id(candidate)] = candidate
                    continue
                normalized_by_object[id(candidate)] = normalized

            def normalize_headers(current: Any) -> Any:
                if isinstance(current, Table):
                    return normalized_by_object.get(id(current), current)
                if not isinstance(current, Node):
                    return current
                changes: dict[str, Any] = {}
                for dataclass_field in fields(current):
                    child = getattr(current, dataclass_field.name)
                    if isinstance(child, Node):
                        updated = normalize_headers(child)
                    elif isinstance(child, tuple):
                        updated = tuple(
                            normalize_headers(member) if isinstance(member, Node) else member for member in child
                        )
                    else:
                        continue
                    if updated != child:
                        changes[dataclass_field.name] = updated
                return replace(current, **changes) if changes else current

            document = normalize_headers(document)

    by_table: dict[str, list[TableCellPresentation]] = {}
    for cell in artifact.cells:
        by_table.setdefault(cell.table_id, []).append(cell)
    used: set[tuple[str, int, int]] = set()
    authority_by_object: dict[int, str] = {}
    table_ordinal = 0
    candidate_tables = _iter_tables(document)
    candidate_identity = _table_identities(candidate_tables)
    unmatched_ordinals = set(range(len(candidate_tables)))
    expected_by_ordinal: dict[int, list[TableCellPresentation]] = {}

    for table_id, expected in by_table.items():
        exact = [
            ordinal
            for ordinal in unmatched_ordinals
            if candidate_identity[ordinal][0] == table_id
            and all(item.topology_hash == candidate_identity[ordinal][1] for item in expected)
            and not candidate_identity[ordinal][2]
        ]
        if len(exact) == 1:
            ordinal = exact[0]
            expected_by_ordinal[ordinal] = expected
            unmatched_ordinals.remove(ordinal)
        elif len(exact) > 1:
            diagnostics.append("table presentation identity is ambiguous")

    for table_id, expected in by_table.items():
        if any(item.table_id == table_id for items in expected_by_ordinal.values() for item in items):
            continue
        topology_hashes = {item.topology_hash for item in expected}
        candidates = [
            ordinal
            for ordinal in unmatched_ordinals
            if candidate_identity[ordinal][1] in topology_hashes and not candidate_identity[ordinal][2]
        ]
        if len(candidates) == 1:
            ordinal = candidates[0]
            expected_by_ordinal[ordinal] = expected
            unmatched_ordinals.remove(ordinal)
        else:
            diagnostics.append("table presentation identity is ambiguous")

    def rewrite(current: Any) -> Any:
        nonlocal table_ordinal
        if isinstance(current, Table):
            ordinal = table_ordinal
            table_ordinal += 1
            _table_id, topology_hash, ambiguous = candidate_identity[ordinal]
            expected = expected_by_ordinal.get(ordinal, [])
            authority = artifact.tables[ordinal] if ordinal < len(artifact.tables) else None
            if ambiguous:
                diagnostics.append("table presentation identity is ambiguous")
                return current
            if expected and any(item.topology_hash != topology_hash for item in expected):
                diagnostics.append("table presentation topology changed")
                return current
            replacements = {(item.anchor_row_index, item.anchor_column_index): item for item in expected}
            rows = ((current.header,) if current.header is not None else ()) + current.body
            new_rows: list[TableRow] = []
            anchor_by_cell = {
                id(cell): (anchor_row, anchor_column) for anchor_row, anchor_column, cell in _anchor_cells(current)
            }
            for row in rows:
                new_cells: list[TableCell] = []
                for cell in row.cells:
                    anchor = anchor_by_cell[id(cell)]
                    item = replacements.get(anchor)
                    if item is not None:
                        if (
                            item.source_style is not None
                            and cell.align != item.align
                            and not (
                                authority is not None and authority.mixed_alignment and cell.align == CellAlign.NONE
                            )
                        ):
                            diagnostics.append("table presentation edit is not supported")
                        used.add((item.table_id, *anchor))
                        cell = replace(
                            cell,
                            background=item.background,
                            align=item.align,
                            source_style=item.source_style,
                        )
                    new_cells.append(cell)
                new_rows.append(replace(row, cells=tuple(new_cells)))
            header = new_rows[0] if current.header is not None else None
            body = tuple(new_rows[1:] if current.header is not None else new_rows)
            return replace(current, header=header, body=body)
        if not isinstance(current, Node):
            return current
        changes: dict[str, Any] = {}
        for dataclass_field in fields(current):
            child = getattr(current, dataclass_field.name)
            if isinstance(child, Node):
                updated = rewrite(child)
            elif isinstance(child, tuple):
                updated = tuple(rewrite(member) if isinstance(member, Node) else member for member in child)
            else:
                continue
            if updated != child:
                changes[dataclass_field.name] = updated
        return replace(current, **changes) if changes else current

    updated_document = rewrite(document)
    # ``rewrite`` deliberately preserves object identity when applying the same
    # presentation is a value-level no-op.  Build the authority map from the
    # objects that are actually present in the returned document; recording the
    # temporary ``replace(cell, ...)`` objects would make an independent replay
    # point at identities that the renderer can never observe.
    authority_by_object = {}
    for ordinal, table in enumerate(_iter_tables(updated_document)):
        expected = expected_by_ordinal.get(ordinal, [])
        cells_by_anchor = {(row, column): cell for row, column, cell in _anchor_cells(table)}
        for item in expected:
            actual_cell = cells_by_anchor.get((item.anchor_row_index, item.anchor_column_index))
            if actual_cell is None:
                continue
            authority_by_object[id(actual_cell)] = presentation_cell_occurrence_id(
                item.table_id,
                item.topology_hash,
                item.anchor_row_index,
                item.anchor_column_index,
            )
    expected_keys = {(item.table_id, item.anchor_row_index, item.anchor_column_index) for item in artifact.cells}
    if expected_keys != used:
        diagnostics.append("table presentation is ambiguous or missing")
    return updated_document, tuple(dict.fromkeys(diagnostics)), authority_by_object


def apply_presentation(document: Document, artifact: PresentationArtifact) -> tuple[Document, tuple[str, ...]]:
    updated, diagnostics, _authority = apply_presentation_with_authority(document, artifact)
    return updated, diagnostics


__all__ = [
    "ColorCapability",
    "ColorCapabilityMatrix",
    "DEFAULT_COLOR_CAPABILITIES",
    "PresentationArtifact",
    "TableCellAddress",
    "TableCellPresentation",
    "TableTopologyAuthority",
    "apply_presentation",
    "apply_presentation_with_authority",
    "extract_presentation",
    "parse_cell_background",
    "parse_color",
    "presentation_cell_occurrence_id",
    "presentation_cell_occurrences",
    "table_topology_hash",
]
