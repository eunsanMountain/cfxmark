"""Every published change must have exactly one owner (plan section 7.3, Story 3).

The 0.3.0 safety claim is not "nothing is lost" — losses are allowed once the
user consents. It is "nothing is lost without being explained". That reduces to
a single invariant over the storage diff between the fresh remote and the
candidate about to be published: every changed leaf belongs to exactly one
migration occurrence or exactly one user edit.

A leaf with no owner is a change nobody described, which is what silent loss
looks like from the inside. A leaf with two owners means the report cannot say
which decision produced it, so an approval would cover something the user never
saw. Both fail closed; neither can be waived by consent.
"""

from __future__ import annotations

import pytest

from cfxmark.change_ownership import (
    ChangeArtifacts,
    LeafChange,
    classify_changes,
)


def _change(path: str, before: str | None = "a", after: str | None = "b") -> LeafChange:
    return LeafChange(identity=(path, "text", None, 0), before_fingerprint=before, after_fingerprint=after)


def _artifacts() -> ChangeArtifacts:
    return ChangeArtifacts(
        source_storage_sha256="sha256:" + "0" * 64,
        base_markdown_sha256="sha256:" + "1" * 64,
        baseline_candidate_sha256="sha256:" + "2" * 64,
        edited_markdown_sha256="sha256:" + "3" * 64,
        final_candidate_sha256="sha256:" + "4" * 64,
    )


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_a_change_owned_by_one_user_edit_is_intended() -> None:
    change = _change("/p[0]")
    result = classify_changes(
        artifacts=_artifacts(),
        final_changes=(change,),
        migration_owners={},
        user_owners={change.identity: "op-1"},
    )
    assert result.fatal is False
    assert [item.identity for item in result.intended] == [change.identity]
    assert result.migration == ()


def test_a_change_owned_by_one_migration_occurrence_is_declared() -> None:
    change = _change("/table[0]")
    result = classify_changes(
        artifacts=_artifacts(),
        final_changes=(change,),
        migration_owners={change.identity: "mig_occ_sha256:abc"},
        user_owners={},
    )
    assert result.fatal is False
    assert [item.identity for item in result.migration] == [change.identity]


def test_an_unowned_change_is_fatal() -> None:
    """The silent-loss case: something moved and no decision explains it."""
    change = _change("/p[0]")
    result = classify_changes(artifacts=_artifacts(), final_changes=(change,), migration_owners={}, user_owners={})
    assert result.fatal is True
    assert result.blocking_code == "unclassified-storage-change"
    assert [item.identity for item in result.unclassified] == [change.identity]


def test_a_change_claimed_by_both_a_migration_and_a_user_edit_is_fatal() -> None:
    """0.3.0 sets no precedence here on purpose.

    Picking a winner would let an approval silently cover a user edit, or a user
    edit silently carry a loss the report attributed elsewhere.
    """
    change = _change("/td[3]")
    result = classify_changes(
        artifacts=_artifacts(),
        final_changes=(change,),
        migration_owners={change.identity: "mig_occ_sha256:abc"},
        user_owners={change.identity: "op-1"},
    )
    assert result.fatal is True
    assert result.blocking_code == "migration-user-overlap-ambiguous"


def test_two_user_operations_claiming_one_leaf_is_fatal() -> None:
    change = _change("/p[0]")
    result = classify_changes(
        artifacts=_artifacts(),
        final_changes=(change,),
        migration_owners={},
        user_owners={change.identity: ("op-1", "op-2")},
    )
    assert result.fatal is True
    assert result.blocking_code == "multiple-change-owners"


def test_overlap_outranks_unclassified_when_both_are_present() -> None:
    """Precedence is fixed so the reported cause does not depend on scan order."""
    overlapping = _change("/a[0]")
    orphan = _change("/b[0]")
    result = classify_changes(
        artifacts=_artifacts(),
        final_changes=(overlapping, orphan),
        migration_owners={overlapping.identity: "mig_occ_sha256:abc"},
        user_owners={overlapping.identity: "op-1"},
    )
    assert result.blocking_code == "migration-user-overlap-ambiguous"


# ---------------------------------------------------------------------------
# Preserved inventory
# ---------------------------------------------------------------------------


def test_preserved_leaves_are_counted_but_own_nothing() -> None:
    """Plan 7.3: preserved is an inventory, not an owner of any changed leaf."""
    change = _change("/p[0]")
    result = classify_changes(
        artifacts=_artifacts(),
        final_changes=(change,),
        migration_owners={},
        user_owners={change.identity: "op-1"},
        preserved_count=142,
    )
    assert result.preserved_count == 142
    assert len(result.intended) == 1
    assert result.fatal is False


def test_an_empty_diff_is_clean() -> None:
    result = classify_changes(artifacts=_artifacts(), final_changes=(), migration_owners={}, user_owners={})
    assert result.fatal is False
    assert result.intended == () and result.migration == ()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_classification_is_independent_of_input_order() -> None:
    first = _change("/a[0]")
    second = _change("/b[0]")
    owners = {first.identity: "op-1", second.identity: "op-2"}

    forward = classify_changes(
        artifacts=_artifacts(), final_changes=(first, second), migration_owners={}, user_owners=owners
    )
    backward = classify_changes(
        artifacts=_artifacts(), final_changes=(second, first), migration_owners={}, user_owners=owners
    )
    assert [item.identity for item in forward.intended] == [item.identity for item in backward.intended]


@pytest.mark.parametrize(
    ("before", "after"),
    [("a", None), (None, "b")],
)
def test_one_sided_transitions_still_need_an_owner(before: str | None, after: str | None) -> None:
    """A deletion or an insertion is a change like any other."""
    change = _change("/p[0]", before=before, after=after)
    result = classify_changes(artifacts=_artifacts(), final_changes=(change,), migration_owners={}, user_owners={})
    assert result.fatal is True
    assert result.blocking_code == "unclassified-storage-change"


def test_artifacts_are_reported_alongside_the_verdict() -> None:
    """The five hashes are the evidence a later push re-derives and compares."""
    artifacts = _artifacts()
    result = classify_changes(artifacts=artifacts, final_changes=(), migration_owners={}, user_owners={})
    assert result.artifacts == artifacts
    assert result.artifacts.source_storage_sha256.startswith("sha256:")
