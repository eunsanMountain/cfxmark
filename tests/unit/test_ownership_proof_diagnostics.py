"""W1b — typed ownership diagnostics at the managed fatal-proof boundary.

Covers the public :class:`OwnershipProofError` / :class:`OwnershipProofSummary`
and the internal, value-free :func:`storage_leaf_diff`.
"""

from __future__ import annotations

import dataclasses

import pytest

import cfxmark
from cfxmark.exceptions import DiagnosticSummary, OwnershipProofError, OwnershipProofSummary
from cfxmark.ownership_proof import StorageLeafDiff, storage_leaf_diff

EDITABLE = cfxmark.ConversionOptions(profile="editable")

# Reproducible fatal proof: duplicate list items make the intended source item
# ambiguous. The tight-list attribution must not guess which identical ``li``
# owns the inline-code edit, so the proof is fatal with one unclassified leaf.
_FATAL_SOURCE = "<ul><li>run the plain command now</li><li>run the plain command now</li></ul>"


def _fatal_managed_candidate() -> tuple[cfxmark.CfxArtifact, str]:
    base = dataclasses.replace(
        cfxmark.to_md_artifact(_FATAL_SOURCE, options=EDITABLE),
        protected_regions=(),
        remote_subtrees=(),
    )
    edited = base.markdown.replace("plain command", "`plain` command", 1)
    candidate = cfxmark.to_cfx_artifact(
        edited,
        base_artifact=base,
        splice_source=_FATAL_SOURCE,
        options=EDITABLE,
    )
    assert candidate.ownership_proof is not None
    assert candidate.ownership_proof.fatal is True
    return candidate, _FATAL_SOURCE


def test_ownership_proof_error_is_a_conversion_error_subtype() -> None:
    assert issubclass(OwnershipProofError, cfxmark.ConversionError)
    assert issubclass(OwnershipProofError, cfxmark.CfxmarkError)


def test_fatal_managed_proof_raises_ownership_proof_error_with_summary() -> None:
    candidate, source = _fatal_managed_candidate()

    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)

    summary = excinfo.value.summary
    assert isinstance(summary, OwnershipProofSummary)
    assert summary.fatal_class == "unclassified-storage-change"
    assert summary.unclassified_count == 1
    assert summary.multiple_owners_count == 0
    assert summary.overlap_count == 0
    # One canonical, value-free identity — the ``li`` topology leaf.
    assert summary.top_identities == ((("ul[0]", "li[0]"), "topology", None, 0),)
    # The blocking conversion diagnostic is projected to code + resolution only.
    assert DiagnosticSummary(code="unclassified-storage-change", resolution="keep-remote") in summary.diagnostics


def test_fatal_boundary_still_caught_as_conversion_error() -> None:
    candidate, source = _fatal_managed_candidate()
    # Existing ``except ConversionError`` handlers must keep catching the boundary.
    with pytest.raises(cfxmark.ConversionError):
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)


def test_ownership_proof_summary_carries_no_values() -> None:
    candidate, source = _fatal_managed_candidate()
    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)
    summary = excinfo.value.summary

    serialized = repr(dataclasses.asdict(summary))
    # No value fingerprint and no raw source content may cross the boundary.
    assert "sha256:" not in serialized
    assert "plain" not in serialized
    assert "command" not in serialized
    # Every identity is the canonical 4-tuple, never a fingerprint pair.
    for identity in summary.top_identities:
        path, field, attribute_name, ordinal = identity
        assert isinstance(path, tuple)
        assert isinstance(field, str)
        assert attribute_name is None or isinstance(attribute_name, str)
        assert isinstance(ordinal, int)


# A page with more fatal leaves than the display budget. Fourteen identical list
# items each receive an inline-code edit, so each topology leaf remains ambiguous
# and the proof is fatal with fourteen unclassified leaves.
def _many_leaf_fatal_candidate() -> tuple[cfxmark.CfxArtifact, str]:
    source = "<ul>" + "".join("<li>run the plain command now</li>" for _ in range(14)) + "</ul>"
    base = dataclasses.replace(
        cfxmark.to_md_artifact(source, options=EDITABLE),
        protected_regions=(),
        remote_subtrees=(),
    )
    candidate = cfxmark.to_cfx_artifact(
        base.markdown.replace("plain command", "`plain` command"),
        base_artifact=base,
        splice_source=source,
        options=EDITABLE,
    )
    assert candidate.ownership_proof is not None
    assert candidate.ownership_proof.fatal is True
    return candidate, source


def test_the_summary_carries_every_fatal_leaf_not_only_the_ten_it_displays() -> None:
    """The cap is a display budget, so something has to carry the rest.

    Both lists are the same value-free projection, so ten was never a redaction
    boundary -- and a caller diagnosing a corpus reads it as one at its peril. A
    harness grouped nine documents' root causes off `top_identities` and reported
    shared causes that were only shared *among the first ten* of a thirty-three leaf
    page. The count was always right there; the shapes it counted were not.
    """

    candidate, source = _many_leaf_fatal_candidate()

    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)
    summary = excinfo.value.summary

    assert summary.unclassified_count == 14
    assert len(summary.top_identities) == 10
    # The point of the field: the count and the shapes finally agree.
    assert len(summary.all_identities) == summary.unclassified_count
    assert len(set(summary.all_identities)) == 14


def test_the_displayed_leaves_are_the_first_page_of_the_full_set() -> None:
    """Not merely "both are populated". If the two were sorted independently they
    could disagree about which leaf is first, and a reader comparing an error message
    against a diagnosis would have no way to tell which one was lying."""

    candidate, source = _many_leaf_fatal_candidate()
    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)
    summary = excinfo.value.summary

    assert summary.top_identities == summary.all_identities[:10]
    assert list(summary.all_identities) == sorted(summary.all_identities)


def test_a_summary_whose_two_lists_disagree_cannot_be_constructed() -> None:
    """The invariant is enforced where the value is made, not only where it is read.

    Otherwise the guarantee holds for cfxmark's own construction and silently does
    not for anyone building this dataclass directly -- which the test above would
    never notice.
    """

    identities = tuple((("p[0]",), "text", None, n) for n in range(12))
    with pytest.raises(ValueError, match="first page"):
        OwnershipProofSummary(
            fatal_class="unclassified-storage-change",
            unclassified_count=12,
            multiple_owners_count=0,
            overlap_count=0,
            top_identities=identities[2:12],  # a different ten
            all_identities=identities,
        )


def test_the_full_leaf_list_carries_no_values_either() -> None:
    """The uncapped list is the one a diagnosis serializes to a receipt, so its
    value-freedom is the property that keeps author text out of a shared artifact.
    Asserted separately from `top_identities`: a projection applied to one list and
    not the other is exactly the mistake this would catch."""

    candidate, source = _many_leaf_fatal_candidate()
    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)
    summary = excinfo.value.summary

    serialized = repr(summary.all_identities)
    assert "sha256:" not in serialized
    assert "plain" not in serialized
    assert "command" not in serialized
    for path, field, attribute_name, ordinal in summary.all_identities:
        assert isinstance(path, tuple)
        assert isinstance(field, str)
        assert attribute_name is None or isinstance(attribute_name, str)
        assert isinstance(ordinal, int)


def test_summary_and_diagnostic_summary_are_immutable() -> None:
    summary = OwnershipProofSummary(
        fatal_class="unclassified-storage-change",
        unclassified_count=1,
        multiple_owners_count=0,
        overlap_count=0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.fatal_class = "tampered"  # type: ignore[misc]
    diagnostic = DiagnosticSummary(code="x", resolution="y")
    with pytest.raises(dataclasses.FrozenInstanceError):
        diagnostic.code = "z"  # type: ignore[misc]


def test_storage_leaf_diff_classifies_by_identity_only() -> None:
    changed = storage_leaf_diff("<p>alpha</p>", "<p>bravo</p>")
    assert isinstance(changed, StorageLeafDiff)
    assert changed.only_left == ()
    assert changed.only_right == ()
    assert len(changed.value_differs) >= 1

    added = storage_leaf_diff("<p>alpha</p>", "<p>alpha</p><p>bravo</p>")
    assert added.only_left == ()
    # The new paragraph's leaves appear only on the right; the enclosing
    # fragment's topology leaf differs because a child was added.
    assert len(added.only_right) >= 1

    # Identities only — no leaf value / fingerprint text appears anywhere.
    serialized = repr(dataclasses.asdict(changed)) + repr(dataclasses.asdict(added))
    assert "sha256:" not in serialized
    assert "alpha" not in serialized
    assert "bravo" not in serialized
    for identity in (*changed.value_differs, *added.only_right):
        assert len(identity) == 4


def test_internal_leaf_diff_types_are_not_public() -> None:
    # StorageLeafDiff / storage_leaf_diff stay internal (preflight R1 decision).
    assert "StorageLeafDiff" not in cfxmark.__all__
    assert "storage_leaf_diff" not in cfxmark.__all__
    assert not hasattr(cfxmark, "storage_leaf_diff")
    # The two W1b public names ARE exported.
    assert "OwnershipProofError" in cfxmark.__all__
    assert "OwnershipProofSummary" in cfxmark.__all__


def _table(tag: str) -> str:
    return f"| h{tag} | v |\n| --- | --- |\n| c{tag} | 1 |\n"


def _fatal_summary(markdown: str) -> OwnershipProofSummary:
    storage = cfxmark.to_cfx_artifact(markdown).xhtml
    base = dataclasses.replace(
        cfxmark.to_md_artifact(storage, options=EDITABLE),
        protected_regions=(),
        remote_subtrees=(),
    )
    candidate = cfxmark.to_cfx_artifact(markdown, base_artifact=base, splice_source=storage, options=EDITABLE)
    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)
    return excinfo.value.summary


def test_table_presentation_failure_names_which_table() -> None:
    """A table-presentation failure has to say which table it is about.

    It is not leaf-based, so ``top_identities`` is empty for it — before this the
    summary said a table could not be matched and nothing else, which on a page with
    sixty tables is not an answer. The position is an integer in document order; the
    table identity is derived from cell text and deliberately does not cross here.
    """

    summary = _fatal_summary("# T\n\n" + _table("a") + "\n" + _table("b") + "\n" + _table("b"))

    assert summary.fatal_class == "table-presentation-ambiguous"
    assert summary.top_identities == ()
    # The two colliding tables, and not the distinct first one.
    assert sorted(item.table_ordinal for item in summary.diagnostics) == [1, 2]


def test_table_ordinal_is_none_when_no_single_table_is_at_fault() -> None:
    """Failures that are about the document, not one table, must not invent a position."""

    summary = _fatal_summary("# T\n\n" + _table("a") + "\n" + _table("a"))

    assert all(item.table_ordinal is not None for item in summary.diagnostics)
    # A non-table fatal keeps the field absent rather than defaulting to table 0.
    candidate, source = _fatal_managed_candidate()
    with pytest.raises(OwnershipProofError) as excinfo:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=source, options=EDITABLE)
    assert all(item.table_ordinal is None for item in excinfo.value.summary.diagnostics)


def test_diagnostic_summary_stays_value_free() -> None:
    """Adding a position must not open a hole: only a code, a resolution, an int."""

    summary = _fatal_summary("# T\n\n" + _table("SENTINEL") + "\n" + _table("SENTINEL"))

    serialized = repr(dataclasses.asdict(summary))
    assert "SENTINEL" not in serialized
    for item in summary.diagnostics:
        assert isinstance(item.code, str)
        assert isinstance(item.resolution, str)
        assert item.table_ordinal is None or isinstance(item.table_ordinal, int)
