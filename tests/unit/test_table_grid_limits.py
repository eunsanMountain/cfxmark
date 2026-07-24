from __future__ import annotations

import pytest

from cfxmark.ast import CellType, Document, Table, TableCell, TableRow, Text
from cfxmark.exceptions import ConversionError
from cfxmark.presentation import extract_presentation
from cfxmark.renderers.md import MarkdownRenderOptions, render_md
from cfxmark.table_grid import TableGridLimitExceeded, bounded_table_grid_width


def _cell(text: str, *, colspan: int = 1, rowspan: int = 1) -> TableCell:
    return TableCell(
        kind=CellType.DATA,
        children=(Text(content=text),),
        colspan=colspan,
        rowspan=rowspan,
    )


def test_exact_grid_slot_boundary_uses_renderer_placement() -> None:
    allowed = (TableRow(cells=(_cell("H", colspan=1000),)),) + tuple(
        TableRow(cells=(_cell(str(index)),)) for index in range(9)
    )
    rejected = allowed + (TableRow(cells=(_cell("overflow"),)),)

    assert bounded_table_grid_width(allowed) == 1000
    with pytest.raises(TableGridLimitExceeded):
        bounded_table_grid_width(rejected)


def test_direct_ast_large_span_fails_before_renderer_or_presentation_allocation() -> None:
    table = Table(header=None, body=(TableRow(cells=(_cell("X", colspan=1001),)),))
    document = Document(children=(table,))

    with pytest.raises(ConversionError, match="safe editable limit"):
        render_md(document, options=MarkdownRenderOptions(profile="editable"))
    with pytest.raises(TableGridLimitExceeded):
        extract_presentation(document)
