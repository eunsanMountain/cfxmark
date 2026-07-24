"""Ownership of every published storage change.

Plan reference: `.omx/plans/atls-0.3.0-markdown-first-informed-migration-plan.md`
section 7.3.

The 0.3.0 safety claim is not that nothing is lost. Losses are allowed once the
user consents. The claim is that nothing is lost *without being explained*, and
that reduces to one invariant over the storage diff between the fresh remote and
the candidate about to be published:

    every changed leaf belongs to exactly one migration occurrence,
    or to exactly one user edit operation.

A leaf with no owner is a change nobody described — silent loss seen from the
inside. A leaf with two owners means the report cannot say which decision
produced it, so an approval would cover something the user never reviewed.

The previous design tried to prove *preservation*; this proves *explanation
completeness*. The invariant is just as strict, but it now serves the question
users actually ask: what will change, and did I agree to it?

Five artifacts anchor the comparison (plan 7.3):

    S0  fresh remote storage
    M0  canonical base Markdown regenerated from S0
    C0  baseline candidate rendered from M0
    M1  the user's edited Markdown
    C1  final candidate rendered from M1
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# (semantic_path, field, attribute_name, occurrence_ordinal)
LeafIdentity = tuple[tuple[str, ...], str, str | None, int]

# A leaf may be attributed to one owner or, in the failure case worth reporting,
# to several.
OwnerRef = str | tuple[str, ...]


@dataclass(frozen=True)
class ChangeArtifacts:
    """The five hashes a later push re-derives and compares byte for byte."""

    source_storage_sha256: str
    base_markdown_sha256: str
    baseline_candidate_sha256: str
    edited_markdown_sha256: str
    final_candidate_sha256: str
    converter: str = "cfxmark/unknown"
    profile: str = "unknown"
    attribution_witness_sha256: str = ""


@dataclass(frozen=True)
class LeafChange:
    identity: LeafIdentity
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None


@dataclass(frozen=True)
class LeafSnapshot:
    """One source-storage leaf identity and its value fingerprint, never raw content."""

    identity: LeafIdentity
    value_fingerprint: str


@dataclass(frozen=True)
class OwnedChange:
    identity: LeafIdentity
    owner: str
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None


@dataclass(frozen=True)
class SupersessionWitness:
    """Proof that a would-be migration/user overlap is a superseded structural shift.

    A leaf that both a migration occurrence and a user operation change is normally
    fatal: the report cannot say which decision produced the final value. For a
    derived *positional* leaf (``field == "topology"``) the two can instead be a
    disclosed composition -- the migration reshaped a node into C0, and a user
    structural operation then removed that exact C0 node. This tuple records the
    mechanical binding so both the prover and the independent validator re-derive
    it identically and compare byte for byte:

        migration created the C0 node whose leaf is ``c0_intermediate_fingerprint``;
        the user operation ``user_operation_id`` removed that same C0 leaf via a
        ``tombstone_kind`` tombstone; nothing else owns the final leaf.

    Only then does the overlap dissolve to a user-owned intended change. A missing
    or non-unique witness leaves the leaf fatal.
    """

    final_leaf: LeafIdentity
    before_fingerprint: str | None
    after_fingerprint: str | None
    migration_occurrence_id: str
    c0_intermediate_fingerprint: str
    user_operation_id: str
    tombstone_kind: str


@dataclass(frozen=True)
class OwnershipResult:
    artifacts: ChangeArtifacts
    source_leaves: tuple[LeafSnapshot, ...] = ()
    final_changes: tuple[LeafChange, ...] = ()
    intended: tuple[OwnedChange, ...] = ()
    migration: tuple[OwnedChange, ...] = ()
    unclassified: tuple[LeafChange, ...] = ()
    multiple_owners: tuple[LeafChange, ...] = ()
    overlapping: tuple[LeafChange, ...] = ()
    supersession_witnesses: tuple[SupersessionWitness, ...] = ()
    fatal_diagnostic_codes: tuple[str, ...] = ()
    preserved_count: int = field(default=0)

    def canonical_json(self) -> str:
        """Serialize the complete proof without raw Markdown or storage bytes."""

        def change_document(change: LeafChange | OwnedChange) -> dict[str, object]:
            semantic_path, field_name, attribute_name, ordinal = change.identity
            document: dict[str, object] = {
                "identity": [list(semantic_path), field_name, attribute_name, ordinal],
                "before_fingerprint": change.before_fingerprint,
                "after_fingerprint": change.after_fingerprint,
            }
            if isinstance(change, OwnedChange):
                document["owner"] = change.owner
            return document

        def snapshot_document(snapshot: LeafSnapshot) -> dict[str, object]:
            semantic_path, field_name, attribute_name, ordinal = snapshot.identity
            return {
                "identity": [list(semantic_path), field_name, attribute_name, ordinal],
                "value_fingerprint": snapshot.value_fingerprint,
            }

        def witness_document(witness: SupersessionWitness) -> dict[str, object]:
            semantic_path, field_name, attribute_name, ordinal = witness.final_leaf
            return {
                "final_leaf": [list(semantic_path), field_name, attribute_name, ordinal],
                "before_fingerprint": witness.before_fingerprint,
                "after_fingerprint": witness.after_fingerprint,
                "migration_occurrence_id": witness.migration_occurrence_id,
                "c0_intermediate_fingerprint": witness.c0_intermediate_fingerprint,
                "user_operation_id": witness.user_operation_id,
                "tombstone_kind": witness.tombstone_kind,
            }

        return json.dumps(
            {
                "artifacts": {
                    "source_storage_sha256": self.artifacts.source_storage_sha256,
                    "base_markdown_sha256": self.artifacts.base_markdown_sha256,
                    "baseline_candidate_sha256": self.artifacts.baseline_candidate_sha256,
                    "edited_markdown_sha256": self.artifacts.edited_markdown_sha256,
                    "final_candidate_sha256": self.artifacts.final_candidate_sha256,
                    "converter": self.artifacts.converter,
                    "profile": self.artifacts.profile,
                    "attribution_witness_sha256": self.artifacts.attribution_witness_sha256,
                },
                "source_leaves": [snapshot_document(snapshot) for snapshot in self.source_leaves],
                "final_changes": [change_document(change) for change in self.final_changes],
                "intended": [change_document(change) for change in self.intended],
                "migration": [change_document(change) for change in self.migration],
                "unclassified": [change_document(change) for change in self.unclassified],
                "multiple_owners": [change_document(change) for change in self.multiple_owners],
                "overlapping": [change_document(change) for change in self.overlapping],
                "supersession_witnesses": [witness_document(witness) for witness in self.supersession_witnesses],
                "fatal_diagnostic_codes": list(self.fatal_diagnostic_codes),
                "preserved_count": self.preserved_count,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        """Return the immutable proof envelope fingerprint used by preflight."""

        return f"sha256:{hashlib.sha256(self.canonical_json().encode('utf-8')).hexdigest()}"

    @property
    def fatal(self) -> bool:
        return bool(self.overlapping or self.multiple_owners or self.unclassified or self.fatal_diagnostic_codes)

    @property
    def intended_operation_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(change.owner for change in self.intended))

    @property
    def migration_occurrence_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(change.owner for change in self.migration))

    @property
    def accepted_migration_occurrence_ids(self) -> tuple[str, ...]:
        """Exact migration identities this proof can hand to consent preflight."""

        return self.migration_occurrence_ids

    @property
    def unexpected(self) -> tuple[LeafChange, ...]:
        return self.unclassified

    @property
    def multiply_owned(self) -> tuple[LeafChange, ...]:
        return self.multiple_owners

    @property
    def blocking_code(self) -> str | None:
        """The single reported cause.

        Precedence is fixed rather than first-encountered so the answer never
        depends on iteration order: an overlap is a contradiction between two
        stated decisions and outranks a leaf that simply lost its owner.
        """
        if self.overlapping:
            return "migration-user-overlap-ambiguous"
        if self.multiple_owners:
            return "multiple-change-owners"
        if self.unclassified:
            return "unclassified-storage-change"
        if self.fatal_diagnostic_codes:
            return self.fatal_diagnostic_codes[0]
        return None


def _owner_count(owner: OwnerRef | None) -> int:
    if owner is None:
        return 0
    if isinstance(owner, tuple):
        return len(owner)
    return 1


def _sole_owner(owner: OwnerRef) -> str:
    return owner[0] if isinstance(owner, tuple) else owner


def classify_changes(
    *,
    artifacts: ChangeArtifacts,
    final_changes: Sequence[LeafChange] | Iterable[LeafChange],
    migration_owners: Mapping[LeafIdentity, OwnerRef],
    user_owners: Mapping[LeafIdentity, OwnerRef],
    preserved_count: int = 0,
    fatal_diagnostic_codes: Sequence[str] | Iterable[str] = (),
) -> OwnershipResult:
    """Attribute each changed leaf, failing closed on any ambiguity.

    Ordering of the input does not affect the verdict or the order of the
    reported groups: results follow the sorted leaf identity so the same diff
    always produces the same report.
    """
    intended: list[OwnedChange] = []
    migration: list[OwnedChange] = []
    unclassified: list[LeafChange] = []
    multiple_owners: list[LeafChange] = []
    overlapping: list[LeafChange] = []

    ordered_final_changes = tuple(sorted(final_changes, key=lambda item: item.identity))
    for change in ordered_final_changes:
        migration_owner = migration_owners.get(change.identity)
        user_owner = user_owners.get(change.identity)
        migration_count = _owner_count(migration_owner)
        user_count = _owner_count(user_owner)

        if migration_count and user_count:
            overlapping.append(change)
            continue
        if migration_count > 1 or user_count > 1:
            multiple_owners.append(change)
            continue
        if user_count == 1:
            assert user_owner is not None
            intended.append(
                OwnedChange(
                    identity=change.identity,
                    owner=_sole_owner(user_owner),
                    before_fingerprint=change.before_fingerprint,
                    after_fingerprint=change.after_fingerprint,
                )
            )
            continue
        if migration_count == 1:
            assert migration_owner is not None
            migration.append(
                OwnedChange(
                    identity=change.identity,
                    owner=_sole_owner(migration_owner),
                    before_fingerprint=change.before_fingerprint,
                    after_fingerprint=change.after_fingerprint,
                )
            )
            continue
        unclassified.append(change)

    return OwnershipResult(
        artifacts=artifacts,
        final_changes=ordered_final_changes,
        intended=tuple(intended),
        migration=tuple(migration),
        unclassified=tuple(unclassified),
        multiple_owners=tuple(multiple_owners),
        overlapping=tuple(overlapping),
        fatal_diagnostic_codes=tuple(dict.fromkeys(fatal_diagnostic_codes)),
        preserved_count=preserved_count,
    )


__all__ = [
    "ChangeArtifacts",
    "LeafChange",
    "LeafIdentity",
    "LeafSnapshot",
    "OwnedChange",
    "OwnershipResult",
    "SupersessionWitness",
    "classify_changes",
]
