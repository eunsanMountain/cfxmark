"""Bounded logical-grid sizing shared by table parsers and renderers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cfxmark.ast import TableRow

MAX_TABLE_SPAN = 1_000
MAX_TABLE_GRID_SLOTS = 10_000


class TableGridError(ValueError):
    """Base class for unsafe or ambiguous table topology."""


class TableGridLimitExceeded(TableGridError):
    """Raised before an untrusted table can allocate an oversized grid."""


class TableGridOverlap(TableGridError):
    """Raised when a cell span crosses an inherited or same-row cell."""


@dataclass(frozen=True)
class TableGridLayout:
    width: int
    anchor_columns: tuple[tuple[int, ...], ...]


def bounded_table_grid_layout(
    rows: Sequence[TableRow],
    *,
    max_span: int = MAX_TABLE_SPAN,
    max_slots: int = MAX_TABLE_GRID_SLOTS,
) -> TableGridLayout:
    """Place every anchor exactly once while enforcing topology and allocation bounds."""

    if not rows:
        return TableGridLayout(width=0, anchor_columns=())
    logical_cells = 0
    width = 0
    pending: dict[int, int] = {}
    anchors_by_row: list[tuple[int, ...]] = []
    for row in rows:
        occupied = {column for column, remaining in pending.items() if remaining > 0}
        next_pending = {column: remaining - 1 for column, remaining in pending.items() if remaining - 1 > 0}
        column = 0
        row_anchors: list[int] = []
        for cell in row.cells:
            while column in occupied:
                column += 1
                if column * len(rows) > max_slots:
                    raise TableGridLimitExceeded("table grid exceeds the safe editable limit")
            if cell.colspan < 1 or cell.rowspan < 1 or cell.colspan > max_span or cell.rowspan > max_span:
                raise TableGridLimitExceeded("table span exceeds the safe editable limit")
            if column + cell.colspan > max_slots:
                raise TableGridLimitExceeded("table grid exceeds the safe editable limit")
            covered = range(column, column + cell.colspan)
            if any(covered_column in occupied for covered_column in covered):
                raise TableGridOverlap("table cell spans overlap in the logical grid")
            logical_cells += cell.colspan * cell.rowspan
            if logical_cells > max_slots:
                raise TableGridLimitExceeded("table grid exceeds the safe editable limit")
            row_anchors.append(column)
            occupied.update(covered)
            if cell.rowspan > 1:
                for span_column in covered:
                    next_pending[span_column] = cell.rowspan - 1
            column += cell.colspan
        anchors_by_row.append(tuple(row_anchors))
        if occupied:
            width = max(width, max(occupied) + 1)
        if width * len(rows) > max_slots:
            raise TableGridLimitExceeded("table grid exceeds the safe editable limit")
        pending = next_pending
    return TableGridLayout(width=width, anchor_columns=tuple(anchors_by_row))


def bounded_table_grid_width(
    rows: Sequence[TableRow],
    *,
    max_span: int = MAX_TABLE_SPAN,
    max_slots: int = MAX_TABLE_GRID_SLOTS,
) -> int:
    """Return the exact validated logical width."""

    return bounded_table_grid_layout(rows, max_span=max_span, max_slots=max_slots).width


__all__ = [
    "MAX_TABLE_GRID_SLOTS",
    "MAX_TABLE_SPAN",
    "TableGridError",
    "TableGridLayout",
    "TableGridLimitExceeded",
    "TableGridOverlap",
    "bounded_table_grid_layout",
    "bounded_table_grid_width",
]
