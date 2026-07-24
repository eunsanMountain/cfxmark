"""Table-cell backgrounds are per-cell decisions (plan Story 2, step 5).

They were reported as a single diagnostic carrying a count: "Markdown omits 3
table-cell background value(s)". That is a summary pretending to be a finding.
Consent is granted per occurrence, so one aggregate means approving three
distinct losses with one decision and no way to say which cells they were.

So each styled cell becomes its own atom, anchored in A1 notation because that
is how a reader locates a cell in the table in front of them -- r0c1 asks them
to translate. The atoms stay authoritative; grouped() collapses them for display
so a 220-cell table reads as one line instead of 220.
"""

from __future__ import annotations

from cfxmark import api
from cfxmark.api import ConversionOptions

LEGACY = ConversionOptions(profile="legacy")

# Three styled cells in one table: two in the first body row, one in the second.
THREE_STYLED_CELLS = (
    "<table><tbody>"
    '<tr><td style="background-color: #ff0000">a</td>'
    '<td style="background-color: #00ff00">b</td></tr>'
    '<tr><td style="background-color: #0000ff">c</td><td>d</td></tr>'
    "</tbody></table>"
)

ONE_STYLED_CELL = '<table><tbody><tr><td style="background-color: #ff0000">a</td><td>b</td></tr></tbody></table>'


def _background_occurrences(storage: str) -> list:
    report = api.to_md_artifact(storage, options=LEGACY).migration_report
    return [item for item in report.occurrences if item.code.startswith("table-cell-background")]


def test_each_styled_cell_is_its_own_occurrence() -> None:
    """The defect: three losses collapsed into one consent decision."""
    assert len(_background_occurrences(THREE_STYLED_CELLS)) == 3


def test_one_styled_cell_yields_one_occurrence() -> None:
    assert len(_background_occurrences(ONE_STYLED_CELL)) == 1


def test_a_table_with_no_styling_reports_nothing() -> None:
    assert _background_occurrences("<table><tbody><tr><td>a</td></tr></tbody></table>") == []


def test_occurrences_are_distinguishable_from_each_other() -> None:
    """Atoms that share an identity cannot be consented to separately."""
    occurrences = _background_occurrences(THREE_STYLED_CELLS)
    assert len({item.occurrence_id for item in occurrences}) == len(occurrences)


def test_each_cell_is_anchored_in_a1_notation() -> None:
    """A reader locates B1 in the table in front of them; r0c1 needs translating."""
    anchors = set()
    for occurrence in _background_occurrences(THREE_STYLED_CELLS):
        cells = [item for item in occurrence.location_identity if item.kind == "cell"]
        assert len(cells) == 1, "a cell-scoped loss must carry exactly one cell anchor"
        anchors.add(cells[0].anchor)
    assert anchors == {"A1", "B1", "A2"}


def test_anchors_are_one_based() -> None:
    """The regression that a 0-based anchor would pass silently."""
    (occurrence,) = _background_occurrences(ONE_STYLED_CELL)
    (cell,) = [item for item in occurrence.location_identity if item.kind == "cell"]
    assert cell.anchor == "A1"
    assert "0" not in cell.anchor


def test_the_display_view_collapses_a_table_to_one_line() -> None:
    """Atomic for consent, grouped for reading."""
    report = api.to_md_artifact(THREE_STYLED_CELLS, options=LEGACY).migration_report
    groups = [item for item in report.grouped() if item.code.startswith("table-cell-background")]
    assert len(groups) == 1
    assert groups[0].count == 3


def test_the_group_still_points_at_every_atom_it_summarises() -> None:
    """A summary that loses its members cannot be expanded or audited."""
    report = api.to_md_artifact(THREE_STYLED_CELLS, options=LEGACY).migration_report
    (group,) = [item for item in report.grouped() if item.code.startswith("table-cell-background")]
    atoms = {item.occurrence_id for item in _background_occurrences(THREE_STYLED_CELLS)}
    assert set(group.occurrence_ids) == atoms


def test_scope_is_the_tables_stable_identity_not_its_position() -> None:
    """Position identity would re-point an occurrence when tables move.

    A consent record naming table[0] means something different after a table is
    inserted above it. The content-derived id does not move with the document.
    """
    artifact = api.to_md_artifact(THREE_STYLED_CELLS, options=LEGACY)
    table_id = artifact.presentation.tables[0].table_id
    for occurrence in _background_occurrences(THREE_STYLED_CELLS):
        scopes = [item.name for item in occurrence.location_identity if item.kind == "path"]
        assert scopes == [f"table:{table_id}"]
        assert not any(scope.startswith("table[") for scope in scopes), "positional scope reintroduced"


def test_reordering_tables_keeps_each_loss_with_its_own_table() -> None:
    """The failure positional scope would cause, stated as a document edit."""
    first = '<table><tbody><tr><td style="background-color: #ff0000">a</td></tr></tbody></table>'
    second = '<table><tbody><tr><td style="background-color: #00ff00">z</td></tr></tbody></table>'
    forward = _background_occurrences(first + second)
    backward = _background_occurrences(second + first)
    scope_of = lambda items: {  # noqa: E731
        item.name for occurrence in items for item in occurrence.location_identity if item.kind == "path"
    }
    assert scope_of(forward) == scope_of(backward)


def test_the_background_message_does_not_promise_preservation() -> None:
    """The managed file does not hold the colour, so it must not imply it does.

    Calling it preserved reads as "publishing is harmless", which is the
    opposite of the decision this occurrence exists to put in front of a user.
    """
    artifact = api.to_md_artifact(ONE_STYLED_CELL, options=LEGACY)
    (omission,) = [item for item in artifact.diagnostics if item.code == "table-cell-background-omitted"]
    assert "reattaches the exact remote presentation value" in omission.message
    assert "remove" not in omission.message


def test_preserved_cell_inventory_keeps_identity_but_not_consent_hash() -> None:
    """Preserved inventory is source-bound without entering migration consent."""
    left = api.to_md_artifact(ONE_STYLED_CELL, options=LEGACY).migration_report
    right = api.to_md_artifact(
        '<table><tbody><tr><td>a</td><td style="background-color: #ff0000">b</td></tr></tbody></table>',
        options=LEGACY,
    ).migration_report
    assert left.sha256() == right.sha256()
    assert {item.occurrence_id for item in left.occurrences} != {item.occurrence_id for item in right.occurrences}
