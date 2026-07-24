from __future__ import annotations

import pytest

import cfxmark
from cfxmark.source_map import (
    EditOperation,
    SemanticMappingAmbiguous,
    SemanticNode,
    SourceMapEntry,
    SourceMapError,
    StorageLeafRef,
    build_base_source_maps,
    build_candidate_source_maps,
    fingerprint_semantic_value,
    match_edit_operations,
    operation_id,
)


def _leaf(path: str, value: str) -> StorageLeafRef:
    return StorageLeafRef((path,), "text", None, 0, fingerprint_semantic_value(value))


def _siblings(values: list[str], *, edited: bool = False, payload_free: bool = False) -> list[SemanticNode]:
    occurrence: dict[str, int] = {}
    nodes: list[SemanticNode] = []
    for ordinal, value in enumerate(values):
        count = occurrence.get(value, 0)
        occurrence[value] = count + 1
        local_id = f"{'edited' if edited else 'base'}-{value.lower()}-{count}"
        nodes.append(
            SemanticNode(
                local_id,
                "paragraph",
                None,
                ordinal,
                fingerprint_semantic_value(value),
                leaves=() if edited else (_leaf(f"p[{ordinal + 1}]", value),),
                payload_free=payload_free,
            )
        )
    return nodes


@pytest.mark.parametrize(
    ("edited_values", "updated_base_id"),
    [
        (["A", "Y", "X", "B"], "base-x-0"),
        (["A", "X", "Y", "B"], "base-x-1"),
    ],
)
def test_duplicate_sibling_update_uses_unique_minimum_mapping(edited_values: list[str], updated_base_id: str) -> None:
    result = match_edit_operations(_siblings(["A", "X", "X", "B"]), _siblings(edited_values, edited=True))

    assert [operation.kind for operation in result.operations] == ["update"]
    assert result.operations[0].base_node_id == updated_base_id
    assert result.operations[0].edited_node_id == updated_base_id


def test_duplicate_sibling_delete_tie_fails_closed() -> None:
    with pytest.raises(SemanticMappingAmbiguous, match="tied") as error:
        match_edit_operations(_siblings(["A", "X", "X", "B"]), _siblings(["A", "X", "B"], edited=True))

    assert error.value.code == "semantic-mapping-ambiguous"


def test_unchanged_duplicate_siblings_pair_by_order() -> None:
    result = match_edit_operations(
        _siblings(["A", "X", "X", "B"]),
        _siblings(["A", "X", "X", "B"], edited=True),
    )

    assert result.operations == ()
    assert result.base_to_edited_source["base-x-0"] == "edited-x-0"
    assert result.base_to_edited_source["base-x-1"] == "edited-x-1"


def test_large_duplicate_alignment_fails_before_quadratic_allocation() -> None:
    with pytest.raises(SemanticMappingAmbiguous, match="alignment safety budget"):
        match_edit_operations(
            _siblings(["X"] * 1_000),
            _siblings(["X"] * 1_000, edited=True),
        )


def test_unique_delete_carries_all_base_subtree_leaves_as_tombstones() -> None:
    base = _siblings(["A", "X", "B"])
    result = match_edit_operations(base, _siblings(["A", "B"], edited=True))

    deleted = next(operation for operation in result.operations if operation.kind == "delete")
    assert deleted.base_node_id == "base-x-0"
    assert deleted.deleted_leaf_tombstones == base[1].leaves


def test_exact_sibling_anchors_partition_update_and_trailing_insert_alignment() -> None:
    result = match_edit_operations(
        _siblings(["A", "B", "C", "D"]),
        _siblings(["A", "B changed", "C", "D", "E"], edited=True),
    )

    assert [operation.kind for operation in result.operations] == ["update", "insert"]
    assert result.operations[0].base_node_id == "base-b-0"
    assert result.operations[1].edited_node_id is not None


def test_two_sibling_inserts_have_a_real_parent_topology_operation() -> None:
    base = [
        SemanticNode(
            "document",
            "Document",
            None,
            0,
            fingerprint_semantic_value("A:B"),
            local_fingerprint=fingerprint_semantic_value("Document"),
        ),
        SemanticNode("base-a", "paragraph", "document", 0, fingerprint_semantic_value("A")),
        SemanticNode("base-b", "paragraph", "document", 1, fingerprint_semantic_value("B")),
    ]
    edited = [
        SemanticNode(
            "edited-document",
            "Document",
            None,
            0,
            fingerprint_semantic_value("A:B:C:D"),
            local_fingerprint=fingerprint_semantic_value("Document"),
        ),
        SemanticNode("edited-a", "paragraph", "edited-document", 0, fingerprint_semantic_value("A")),
        SemanticNode("edited-b", "paragraph", "edited-document", 1, fingerprint_semantic_value("B")),
        SemanticNode("edited-c", "paragraph", "edited-document", 2, fingerprint_semantic_value("C")),
        SemanticNode("edited-d", "paragraph", "edited-document", 3, fingerprint_semantic_value("D")),
    ]

    result = match_edit_operations(base, edited)

    root_update = next(operation for operation in result.operations if operation.base_node_id == "document")
    assert root_update.kind == "update"
    assert root_update.topology_only is True
    assert [operation.kind for operation in result.operations].count("insert") == 2


def test_unique_full_subtree_move_retains_stable_id_and_correspondence() -> None:
    base = [
        SemanticNode("left", "section", None, 0, fingerprint_semantic_value("left:X"), explicit_anchor="left"),
        SemanticNode("moved", "paragraph", "left", 0, fingerprint_semantic_value("X"), leaves=(_leaf("left/p", "X"),)),
        SemanticNode("right", "section", None, 1, fingerprint_semantic_value("right:"), explicit_anchor="right"),
    ]
    edited = [
        SemanticNode("edited-left", "section", None, 0, fingerprint_semantic_value("left:"), explicit_anchor="left"),
        SemanticNode(
            "edited-right", "section", None, 1, fingerprint_semantic_value("right:X"), explicit_anchor="right"
        ),
        SemanticNode("edited-moved", "paragraph", "edited-right", 0, fingerprint_semantic_value("X")),
    ]

    result = match_edit_operations(base, edited)

    move = next(operation for operation in result.operations if operation.kind == "move")
    assert move == EditOperation(
        "move",
        "moved",
        "moved",
        "left",
        "right",
        0,
        0,
        relocated_leaf_tombstones=base[1].leaves,
    )
    assert result.base_to_edited_source["moved"] == "edited-moved"
    assert result.edited_source_to_stable["edited-moved"] == "moved"


def test_base_and_candidate_leaf_maps_are_exact_inverses() -> None:
    first = _leaf("p[1]", "A")
    second = _leaf("p[2]", "B")
    entries = (
        SourceMapEntry("op-b", "ast-node", "node-b", (second,)),
        SourceMapEntry("op-a", "ast-node", "node-a", (first,)),
    )

    storage_to_node, node_to_storage = build_base_source_maps(entries)
    operation_to_storage, storage_to_operation = build_candidate_source_maps(entries)

    assert storage_to_node == {first: "node-a", second: "node-b"}
    assert node_to_storage == {"node-a": (first,), "node-b": (second,)}
    assert operation_to_storage == {"op-a": (first,), "op-b": (second,)}
    assert storage_to_operation == {first: "op-a", second: "op-b"}
    assert operation_id(EditOperation("delete", "node-a", None, None, None, 0, None, (first,))).startswith("op-")


def test_leaf_owned_by_two_nodes_fails_closed() -> None:
    leaf = _leaf("p[1]", "A")
    entries = (
        SourceMapEntry("op-a", "ast-node", "node-a", (leaf,)),
        SourceMapEntry("op-b", "ast-node", "node-b", (leaf,)),
    )

    with pytest.raises(SourceMapError, match="owned by both"):
        build_base_source_maps(entries)


def test_payload_free_run_expansion_resolves_instead_of_tie() -> None:
    # A payload-free node replaced by several distinct payload-free nodes is a
    # run substitution with no exact survivor: the tie over which new node is
    # the "update" is ownership-neutral, so it resolves to delete + inserts.
    result = match_edit_operations(
        _siblings(["X"], payload_free=True),
        _siblings(["P", "Q"], edited=True, payload_free=True),
    )

    kinds = sorted(operation.kind for operation in result.operations)
    assert kinds == ["delete", "insert", "insert"]


def test_payload_free_uneven_run_replacement_resolves_instead_of_tie() -> None:
    # A distinct 2-node run replaced by a distinct 3-node run ties on which pairs
    # up; with exact anchors A and B unchanged and no zero-cost survivor in the
    # middle, it resolves to deleting the old run and inserting the new one.
    result = match_edit_operations(
        _siblings(["A", "X", "Y", "B"], payload_free=True),
        _siblings(["A", "P", "Q", "R", "B"], edited=True, payload_free=True),
    )

    kinds = sorted(operation.kind for operation in result.operations)
    assert kinds == ["delete", "delete", "insert", "insert", "insert"]


def test_duplicate_sibling_delete_stays_fatal_even_when_payload_free() -> None:
    # Payload-free is not enough: an exact zero-cost survivor makes the tie about
    # which identical sibling was deleted, which changes tombstone provenance.
    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _siblings(["A", "X", "X", "B"], payload_free=True),
            _siblings(["A", "X", "B"], edited=True, payload_free=True),
        )


def test_payload_bearing_run_tie_stays_fatal() -> None:
    # The default fail-closed payload flag keeps an ambiguous run of
    # identity-bearing nodes fatal, even without an exact survivor.
    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _siblings(["X"], payload_free=False),
            _siblings(["P", "Q"], edited=True, payload_free=False),
        )


# --------------------------------------------------------------------------- #
# W-TIE: tie neutrality is gated only over correspondence-eligible nodes.
#
# A tie participant can only ever be a `pair` transition, and the alignment DP
# offers a `pair` only between equal node types.  A node whose node_type is
# absent on the opposite side is therefore a *forced* delete/insert in every
# minimum script and must not drag a legitimate paragraph tie into the
# payload-free gate.  The tests below exercise that boundary.
# --------------------------------------------------------------------------- #

EDITABLE = cfxmark.ConversionOptions(profile="editable")

# Production (`artifacts._subtree_payload_free` over `_OWNERSHIP_NEUTRAL_NODE_TYPES`)
# treats a Paragraph subtree as payload-free, but Table, CodeBlock and
# DirectiveMacro carry remote-backed identity and are deliberately NOT neutral.
# The node_type strings here are test-local labels -- the matcher only compares
# them for equality -- while payload_free mirrors that production classification
# per node type.
_PAYLOAD_FREE_BY_TYPE = {
    "paragraph": True,
    "table": False,
    "code_block": False,
    "directive_macro": False,
}


def _typed_siblings(specs: list[tuple[str, str]], *, edited: bool = False) -> list[SemanticNode]:
    """Build a mixed-type sibling run with per-node ``(node_type, value)``.

    Unlike :func:`_siblings`, which emits a paragraph-only run sharing a single
    ``payload_free`` flag, each node here takes its ``payload_free`` value from
    the production classification for its type (paragraph -> True; table /
    code_block / directive_macro -> False).  ``value`` controls the subtree
    fingerprint so callers can make two nodes byte-identical or distinct.
    """

    occurrence: dict[str, int] = {}
    nodes: list[SemanticNode] = []
    for ordinal, (node_type, value) in enumerate(specs):
        count = occurrence.get(value, 0)
        occurrence[value] = count + 1
        local_id = f"{'edited' if edited else 'base'}-{value.lower()}-{count}"
        nodes.append(
            SemanticNode(
                local_id,
                node_type,
                None,
                ordinal,
                fingerprint_semantic_value(value),
                leaves=() if edited else (_leaf(f"n[{ordinal + 1}]", value),),
                payload_free=_PAYLOAD_FREE_BY_TYPE[node_type],
            )
        )
    return nodes


# --- Condition 3b: positive corpus (must resolve, not raise) --------------- #


def test_trailing_payload_insert_with_unique_alignment_still_resolves() -> None:
    # Positive #1: [P] -> [P1, Table].  Not even a tie (the paragraph update is
    # the only cost-2 script), so this passed before W-TIE; it is a regression
    # guard that a single trailing payload insert stays a clean update+insert.
    result = match_edit_operations(
        _typed_siblings([("paragraph", "Orig")]),
        _typed_siblings([("paragraph", "First"), ("table", "Tbl")], edited=True),
    )

    assert sorted(operation.kind for operation in result.operations) == ["insert", "update"]


def test_forced_table_insert_does_not_make_paragraph_tie_fatal() -> None:
    # Positive #2, the measured repro: [P] -> [P1, P2, Table] is a paragraph tie
    # (P updates into either new paragraph at equal cost).  The edited-only Table
    # is a forced insert -- its type is absent on the base side -- so it must not
    # be dragged into the payload-free gate.  Fatal before W-TIE; must resolve to
    # delete P + insert P1, P2, Table now.
    base = _typed_siblings([("paragraph", "Orig")])
    edited = _typed_siblings([("paragraph", "First"), ("paragraph", "Second"), ("table", "Tbl")], edited=True)
    result = match_edit_operations(base, edited)

    assert sorted(operation.kind for operation in result.operations) == ["delete", "insert", "insert", "insert"]
    # The one deletion is the base paragraph, carrying its subtree as tombstones.
    deleted = [operation for operation in result.operations if operation.kind == "delete"]
    assert len(deleted) == 1
    assert deleted[0].base_node_id == "base-orig-0"
    assert deleted[0].deleted_leaf_tombstones == base[0].leaves


def test_forced_code_block_insert_does_not_make_paragraph_tie_fatal() -> None:
    # Positive #3: the CodeBlock variant of the repro.
    result = match_edit_operations(
        _typed_siblings([("paragraph", "Orig")]),
        _typed_siblings([("paragraph", "First"), ("paragraph", "Second"), ("code_block", "Code")], edited=True),
    )

    assert sorted(operation.kind for operation in result.operations) == ["delete", "insert", "insert", "insert"]


@pytest.mark.parametrize(
    "edited_specs",
    [
        [("table", "Tbl"), ("paragraph", "First"), ("paragraph", "Second")],
        [("paragraph", "First"), ("table", "Tbl"), ("paragraph", "Second")],
        [("paragraph", "First"), ("paragraph", "Second"), ("table", "Tbl")],
    ],
)
def test_forced_payload_insert_position_is_irrelevant(edited_specs: list[tuple[str, str]]) -> None:
    # Positive #4: the forced payload insert is exempt regardless of where it
    # sits in the run (leading, middle, trailing).
    result = match_edit_operations(
        _typed_siblings([("paragraph", "Orig")]),
        _typed_siblings(edited_specs, edited=True),
    )

    assert sorted(operation.kind for operation in result.operations) == ["delete", "insert", "insert", "insert"]


@pytest.mark.parametrize(
    "edited_specs",
    [
        [("paragraph", "F"), ("paragraph", "S"), ("paragraph", "T"), ("table", "Tbl")],
        [("table", "Tbl"), ("paragraph", "F"), ("paragraph", "S"), ("paragraph", "T")],
    ],
)
def test_forced_payload_insert_in_larger_run_both_orders(edited_specs: list[tuple[str, str]]) -> None:
    # Positive #5: a larger paragraph tie (one base paragraph into three) with a
    # forced payload insert, in both orders.
    result = match_edit_operations(
        _typed_siblings([("paragraph", "Orig")]),
        _typed_siblings(edited_specs, edited=True),
    )

    assert sorted(operation.kind for operation in result.operations) == [
        "delete",
        "insert",
        "insert",
        "insert",
        "insert",
    ]


def test_forced_payload_delete_amid_paragraph_tie_is_classified() -> None:
    # Positive #6: [P, Macro] -> [P1, P2].  The base-only DirectiveMacro is a
    # forced delete (its type is absent on the edited side) sitting inside a
    # paragraph tie.  It must be exempt from the gate yet still emitted as a
    # delete -- the macro is classified, never silently dropped.
    base = _typed_siblings([("paragraph", "Orig"), ("directive_macro", "Macro")])
    edited = _typed_siblings([("paragraph", "First"), ("paragraph", "Second")], edited=True)
    result = match_edit_operations(base, edited)

    assert sorted(operation.kind for operation in result.operations) == ["delete", "delete", "insert", "insert"]
    macro_delete = next(
        operation
        for operation in result.operations
        if operation.kind == "delete" and operation.base_node_id == "base-macro-0"
    )
    assert macro_delete.deleted_leaf_tombstones == base[1].leaves


# --- Condition 3d: the neutral resolution emits the FULL id arrays --------- #


def test_neutral_resolution_emits_exempted_payload_node_not_filtered_subset() -> None:
    # Guards the drop-vs-exempt ambiguity: the neutrality gate is evaluated over
    # the correspondence-eligible subset ([P1, P2], Table excluded), but the
    # resolution must return the FULL edited array so the exempted Table is still
    # emitted as an insert.  Returning the filtered subset would silently drop
    # the Table (data loss) and leave only two inserts.
    base = _typed_siblings([("paragraph", "Orig")])
    edited = _typed_siblings([("paragraph", "First"), ("paragraph", "Second"), ("table", "Tbl")], edited=True)
    result = match_edit_operations(base, edited)

    inserts = [operation for operation in result.operations if operation.kind == "insert"]
    assert len(inserts) == 3
    table_source_id = next(node.node_id for node in edited if node.node_type == "table")
    table_stable_id = result.edited_source_to_stable[table_source_id]
    assert any(operation.edited_node_id == table_stable_id for operation in inserts)


# --- Condition 3c: adversarial corpus (must stay fatal) -------------------- #


def test_same_type_payload_run_expansion_stays_fatal() -> None:
    # Adversarial #1: [Table_b] -> [Table_e1, Table_e2] (distinct).  Table exists
    # on both sides, so the run is a genuine same-type payload tie and stays
    # fatal -- W-TIE never exempts a correspondence-eligible payload node.
    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _typed_siblings([("table", "TableB")]),
            _typed_siblings([("table", "TableE1"), ("table", "TableE2")], edited=True),
        )


def test_same_type_payload_run_contraction_stays_fatal() -> None:
    # Adversarial #2: [Table_a, Table_b] -> [Table_e].  A base-only ambiguous
    # payload delete with the type present on both sides stays fatal.
    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _typed_siblings([("table", "TableA"), ("table", "TableB")]),
            _typed_siblings([("table", "TableE")], edited=True),
        )


def test_genuine_macro_tie_stays_fatal() -> None:
    # Adversarial #5: [Macro_b] -> [Macro_e1, Macro_e2].  A macro tie participant
    # (same type both sides) carries remote-backed identity and stays fatal.
    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _typed_siblings([("directive_macro", "MacroB")]),
            _typed_siblings([("directive_macro", "MacroE1"), ("directive_macro", "MacroE2")], edited=True),
        )


def test_ambiguous_duplicate_payload_collapse_stays_fatal() -> None:
    # Adversarial #6 (reorder / duplicate form): two byte-identical payload
    # tables collapse to one.  Which duplicate survives is genuinely ambiguous
    # (same type both sides + a zero-cost survivor), so it stays fatal.
    #
    # NOTE: the design's literal "distinct reorder" phrasing is NOT fatal in the
    # real matcher -- see the companion test below -- so this duplicate-collapse
    # shape carries the adversarial intent (same-type payload ambiguity must fail
    # closed).
    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _typed_siblings([("table", "Dup"), ("table", "Dup")]),
            _typed_siblings([("table", "Dup")], edited=True),
        )


def test_distinct_payload_reorder_resolves_to_safe_moves() -> None:
    # Companion to adversarial #6: reordering two DISTINCT payload tables is NOT
    # an ambiguity -- each table is globally unique by fingerprint, so the matcher
    # re-pairs them and emits moves.  This proves the fail-closed boundary keys on
    # genuine ambiguity, not on "a payload node participated", and that W-TIE does
    # not over-reject a legitimate payload reorder.
    result = match_edit_operations(
        _typed_siblings([("table", "TableA"), ("table", "TableB")]),
        _typed_siblings([("table", "TableB"), ("table", "TableA")], edited=True),
    )

    assert sorted(operation.kind for operation in result.operations) == ["move", "move"]


def test_correspondence_eligibility_discriminates_same_paragraph_tie() -> None:
    # Adversarial #7, the discriminator.  Same paragraph tie, opposite verdicts:
    #   * [P] -> [P1, P2, Table_e]      PASSES  (Table edited-only -> exempt)
    #   * [P, Table_b] -> [P1, P2, Te]  FATAL   (Table on both sides -> eligible)
    # Proves the gate keys on correspondence-eligibility, not "a table is present".
    passing = match_edit_operations(
        _typed_siblings([("paragraph", "Orig")]),
        _typed_siblings([("paragraph", "First"), ("paragraph", "Second"), ("table", "TableE")], edited=True),
    )
    assert sorted(operation.kind for operation in passing.operations) == ["delete", "insert", "insert", "insert"]

    with pytest.raises(SemanticMappingAmbiguous, match="tied"):
        match_edit_operations(
            _typed_siblings([("paragraph", "Orig"), ("table", "TableB")]),
            _typed_siblings([("paragraph", "First"), ("paragraph", "Second"), ("table", "TableE")], edited=True),
        )


# --- Condition 1 (safety-critical): payload MOVE via the neutral path ------ #


def _anchored_section(node_id: str, ordinal: int, anchor: str) -> SemanticNode:
    return SemanticNode(node_id, "section", None, ordinal, fingerprint_semantic_value(anchor), explicit_anchor=anchor)


def _child(node_id: str, parent: str, ordinal: int, node_type: str, value: str) -> SemanticNode:
    return SemanticNode(
        node_id,
        node_type,
        parent,
        ordinal,
        fingerprint_semantic_value(value),
        leaves=(_leaf(f"{parent}/{node_id}", value),) if node_type != "section" else (),
        payload_free=_PAYLOAD_FREE_BY_TYPE.get(node_type, False),
    )


def test_payload_move_engages_via_neutral_unmatched_roots_path() -> None:
    # Safety property P.  A payload-bearing Table that is a globally-unique
    # relocation across parents, sitting inside a paragraph tie, must resolve to
    # a MOVE with stable identity.  W-TIE is what routes it there: without the
    # fix its source parent's tie ([P, Table] -> [P1, P2]) fails closed before
    # the move pass ever runs.  This exercises the neutral -> unmatched-roots ->
    # _globally_unique_move_pairs path for a payload node type (the existing move
    # test drives the exact-pairs/anchor path instead).
    base = [
        _anchored_section("secA", 0, "A"),
        _anchored_section("secB", 1, "B"),
        _child("P", "secA", 0, "paragraph", "Orig"),
        _child("TBL", "secA", 1, "table", "TableContent"),  # relocates A -> B
        _child("Q", "secB", 0, "paragraph", "QOrig"),
    ]
    edited = [
        _anchored_section("e-secA", 0, "A"),
        _anchored_section("e-secB", 1, "B"),
        _child("P1", "e-secA", 0, "paragraph", "First"),
        _child("P2", "e-secA", 1, "paragraph", "Second"),  # secA: [P] -> [P1, P2] tie
        _child("Q1", "e-secB", 0, "paragraph", "QFirst"),
        _child("Q2", "e-secB", 1, "paragraph", "QSecond"),  # secB: [Q] -> [Q1, Q2, TBL] tie
        _child("e-TBL", "e-secB", 2, "table", "TableContent"),  # same fingerprint -> unique move key
    ]
    result = match_edit_operations(base, edited)

    moves = [operation for operation in result.operations if operation.kind == "move"]
    assert len(moves) == 1
    move = moves[0]
    assert move.base_node_id == "TBL"
    assert move.old_parent_id == "secA"
    assert move.new_parent_id == "secB"
    assert move.old_parent_id != move.new_parent_id
    assert move.relocated_leaf_tombstones == base[3].leaves  # the Table subtree travels with the move
    # Stable identity is preserved across the relocation.
    assert result.base_to_edited_source["TBL"] == "e-TBL"
    assert result.edited_source_to_stable["e-TBL"] == "TBL"


# --- Condition 2 (safety-critical): non-unique payload relocation ---------- #


def test_non_unique_payload_relocation_preserves_content_as_tombstones() -> None:
    # Safety property.  Two byte-identical Tables relocate A -> B inside a
    # paragraph tie.  Their move key is not globally unique, so they cannot
    # re-pair as a move; each becomes a delete + insert.  No content is lost:
    # every base leaf (both tables and the paragraph) is carried as a delete
    # tombstone, so it is owned by a real operation rather than silently dropped.
    # The only residual is a possible opaque-id re-mint on the re-emitted copies.
    base = [
        _anchored_section("secA", 0, "A"),
        _anchored_section("secB", 1, "B"),
        _child("P", "secA", 0, "paragraph", "Orig"),
        _child("T1", "secA", 1, "table", "Dup"),
        _child("T2", "secA", 2, "table", "Dup"),  # byte-identical to T1
    ]
    edited = [
        _anchored_section("e-secA", 0, "A"),
        _anchored_section("e-secB", 1, "B"),
        _child("P1", "e-secA", 0, "paragraph", "First"),
        _child("P2", "e-secA", 1, "paragraph", "Second"),  # secA: [P, T1, T2] -> [P1, P2] tie
        _child("eT1", "e-secB", 0, "table", "Dup"),
        _child("eT2", "e-secB", 1, "table", "Dup"),  # both duplicates land under B
    ]
    result = match_edit_operations(base, edited)

    assert [operation.kind for operation in result.operations if operation.kind == "move"] == []
    assert "T1" not in result.base_to_edited_source
    assert "T2" not in result.base_to_edited_source
    deleted_leaf_paths = {
        leaf.semantic_path
        for operation in result.operations
        if operation.kind == "delete"
        for leaf in operation.deleted_leaf_tombstones
    }
    # No content dropped: the paragraph and both duplicate tables are tombstoned.
    assert deleted_leaf_paths == {("secA/P",), ("secA/T1",), ("secA/T2",)}


# --- Conditions 3b + 2 at the managed layer (reachable end-to-end) --------- #


def test_managed_paragraph_tie_with_payload_insert_is_push_safe() -> None:
    # The measured repro driven end-to-end through the managed pipeline: a single
    # source paragraph split into two paragraphs plus a new table.  Fatal before
    # W-TIE (push_safe False); now the ownership proof classifies the complete
    # final diff with nothing unclassified, so it is push-safe and validates.
    storage = "<p>Original paragraph content here</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "First paragraph\n\nSecond paragraph\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n",
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    proof = candidate.ownership_proof
    assert proof is not None
    assert proof.fatal is False
    assert proof.unclassified == ()  # no content escapes ownership
    assert proof.multiple_owners == ()
    assert proof.overlapping == ()
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


def test_managed_paragraph_tie_beside_opaque_macro_preserves_bytes() -> None:
    # The paragraph tie coexists with an opaque unknown macro.  W-TIE unblocks the
    # tie while the macro subtree is preserved byte-for-byte in the emitted
    # storage -- a real byte-preservation assertion at the storage layer, not a
    # mere storage_semantically_equal check.
    raw_macro = (
        '<ac:structured-macro ac:name="mystery">'
        "<ac:plain-text-body><![CDATA[secret <x>]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )
    storage = raw_macro + "<p>Original body text</p>"
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.managed_markdown.replace(
            "Original body text",
            "First body line\n\nSecond body line\n\n| A |\n| --- |\n| 1 |",
        ),
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )

    assert candidate.push_safe is True
    proof = candidate.ownership_proof
    assert proof is not None
    assert proof.fatal is False
    assert proof.unclassified == ()
    assert raw_macro in candidate.xhtml  # opaque subtree byte-preserved
    assert candidate.preserved_subtrees or candidate.preserved_regions
