"""How long does proving a candidate take on a document made of tables?

A real page took 356 seconds to preflight, and the first guess at why was
wrong: the actual remote splice is about one second of it. The cost is in the
machinery around it -- recomputing the same operation id, hashing dataclasses
that carry thousands of tombstones, and scanning tombstone prefixes
quadratically.

Tables are the shape that produces it. Every cell is a semantic node with
leaves under it, so a page of tables has an operation count that grows with
cells rather than with blocks, and the per-operation costs above are what
multiply.

Marked `benchmark` so the ordinary suite stays fast. The budget is deliberately
loose compared with the real target: this fixture is a fraction of the live
page, and a synthetic number that drifts is worse than none. Live timing is the
gate; this exists so a regression shows up without a network.
"""

from __future__ import annotations

import time
from collections import Counter

import pytest

import cfxmark

pytestmark = pytest.mark.benchmark

EDITABLE = cfxmark.ConversionOptions(profile="editable")

#: Rows and columns chosen to reach a few thousand cells -- enough that the
#: quadratic terms are visible and the test still finishes in seconds.
ROWS = 40
COLUMNS = 6
TABLES = 8


def _table(index: int) -> str:
    header = "".join(f"<th>h{column}</th>" for column in range(COLUMNS))
    rows = "".join(
        "<tr>" + "".join(f"<td><p>t{index} r{row} c{column}</p></td>" for column in range(COLUMNS)) + "</tr>"
        for row in range(ROWS)
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"


@pytest.fixture(scope="module")
def storage() -> str:
    return "".join(_table(index) for index in range(TABLES))


def test_a_document_of_tables_preflights_within_budget(storage: str) -> None:
    """The candidate path, end to end, on the shape that produces the cost."""

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    edited = base.markdown.replace("t0 r0 c0", "t0 r0 c0 edited")

    started = time.monotonic()
    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )
    elapsed = time.monotonic() - started

    print(f"\n{TABLES} tables x {ROWS}x{COLUMNS} cells: preflight {elapsed:.1f}s")
    assert candidate is not None
    assert elapsed < 60.0, f"preflight took {elapsed:.1f}s on {TABLES * ROWS * COLUMNS} cells"


def test_no_operation_has_its_id_computed_twice(storage: str) -> None:
    """The first bottleneck, pinned as the property that actually holds.

    `operation_id` sorts and JSON-serialises an operation's tombstone arrays.
    Measured on the live page it ran 85,070 times over 53,629 operations, so
    most of that work was answering a question already answered.

    Asserted per operation rather than as a ratio of totals. A ratio needs a
    module-level counter, which accumulates across tests and can be satisfied by
    calls this test never made -- and it would FAIL if a better optimisation
    removed the duplicate calls entirely, which is the wrong way round for a
    performance test to break.

    "At most once per operation" survives both: it is the invariant the cache
    exists to provide, and a future change that stops calling twice keeps it.
    """

    from cfxmark import source_map

    computed_per_operation: Counter[int] = Counter()
    #: Keeps every operation alive for the duration. Without it CPython reuses
    #: the address of a collected object, two unrelated operations share an
    #: `id()`, and the count says "computed twice" about something that was
    #: computed once each. Which is precisely why the cache lives on the object
    #: rather than in a map keyed by `id()` -- the same hazard, and there it
    #: would hand one operation's ID to another.
    alive: list[source_map.EditOperation] = []
    real = source_map._compute_operation_id

    def counting(operation: source_map.EditOperation) -> str:
        alive.append(operation)
        computed_per_operation[id(operation)] += 1
        return real(operation)

    source_map._compute_operation_id = counting  # type: ignore[assignment]
    try:
        base = cfxmark.to_md_artifact(storage, options=EDITABLE)
        edited = base.markdown.replace("t0 r0 c0", "t0 r0 c0 edited")
        cfxmark.to_cfx_artifact(
            edited,
            presentation=base.presentation,
            base_artifact=base,
            splice_source=storage,
            options=EDITABLE,
        )
    finally:
        source_map._compute_operation_id = real  # type: ignore[assignment]

    repeated = {key: count for key, count in computed_per_operation.items() if count > 1}
    print(f"\noperation_id computed for {len(computed_per_operation)} operations, {len(repeated)} more than once")
    assert not repeated, f"{len(repeated)} operations had their ID computed more than once"
