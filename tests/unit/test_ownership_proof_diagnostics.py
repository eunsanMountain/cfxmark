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

# Reproducible fatal proof (preflight W4 evidence): a single inline-code edit
# inside one list item decomposes the ``li`` topology leaf into an owner nobody
# can attribute, so the proof is fatal with exactly one unclassified leaf.
_FATAL_SOURCE = "<ul><li>run the plain command now</li></ul>"


def _fatal_managed_candidate() -> tuple[cfxmark.CfxArtifact, str]:
    base = dataclasses.replace(
        cfxmark.to_md_artifact(_FATAL_SOURCE, options=EDITABLE),
        protected_regions=(),
        remote_subtrees=(),
    )
    edited = base.markdown.replace("plain command", "`plain` command")
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
