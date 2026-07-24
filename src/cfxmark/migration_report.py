"""Canonical migration report: what a user is asked to consent to.

Plan reference: `.omx/plans/atls-0.3.0-markdown-first-informed-migration-plan.md`
sections 7.1 and 7.2.

Markdown cannot express everything Confluence storage holds, so publishing a
page as Markdown may drop or convert information. This module makes that visible
instead of silent: every impact becomes an atomic occurrence with a typed effect,
and the set of occurrences hashes to a fingerprint that a later write must match
exactly.

Two properties make the fingerprint mean something.

Determinism: the same input must always produce the same bytes. Anything that
varies between runs — dict iteration order, hash seeds, Unicode form — would let
an approved report differ from the one actually published.

A consent boundary: only fields that change *what happens to the page* are
hashed. Rewording a human-facing summary must not invalidate an approval the
user already gave, and conversely no change to code, effect, location or the
before/after values may slip through unnoticed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA = "cfxmark-migration-report-v1"

MigrationEffect = Literal["preserved", "converted", "normalized", "removed", "unsupported", "fatal"]

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Effects that change the published page and therefore need an explicit
# decision. `preserved` does not; `normalized` is conditional (see below);
# `fatal` cannot be consented to at all.
_CONSENT_EFFECTS: frozenset[str] = frozenset({"converted", "removed", "unsupported"})

# A `normalized` occurrence may pass without consent only when its code is
# listed here, which per plan 7.1 requires a storage->Markdown->storage
# fixed-point test, an identical semantic signature and readable projection, and
# a raw structural diff fully owned by that occurrence.
#
# Deliberately empty in 0.3.0. An earlier revision listed three plausible-looking
# codes that no producer actually emits and for which none of that evidence
# existed, which would have granted a consent exemption to whatever later
# claimed those names. An allowlist that can be populated by guesswork is worse
# than no allowlist: it converts an unproven assumption into a silent approval.
# Entries are added only alongside a real producer and its fixed-point evidence.
CANONICAL_EQUIVALENT_CODES: frozenset[str] = frozenset()

# Codes emitted by the real diagnostic producers. Kept in sync with the pipeline
# by tests/unit/test_diagnostic_inventory.py, which extracts every emittable code
# and fails when one is missing here. The previous revision was written from the
# plan's illustrative examples rather than from the code, and 11 of the 24 real
# codes were absent — all of which would have defaulted to fatal and blocked
# essentially every page once wired in.
_PRODUCER_EFFECTS: dict[str, MigrationEffect] = {
    # Converted: deliberately expressed a different way.
    "emoticon-to-unicode": "converted",
    # Removed: remote information the candidate does not carry. Lossy but
    # explainable, so a user can consent to it.
    "attachment-url-components-unrenderable": "removed",
    "body-only-table-header-synthesized": "removed",
    "html-tag-stripped": "removed",
    "layout-flattened": "removed",
    # Omitted from the Markdown body, but reattached from fresh source-bound
    # presentation authority when a managed candidate is built.
    "table-cell-background-omitted": "preserved",
    "table-cell-background-unrenderable": "removed",
    "table-cell-background-untracked": "removed",
    "table-column-alignment-mixed": "removed",
    "xml-comment-dropped": "removed",
    # A user-authored HTML comment (<!-- ... -->) dropped on the Markdown->storage
    # push. It holds author-only text that never renders, so the loss is nameable
    # and consentable rather than fatal — the push-side mirror of the pull-side
    # xml-comment-dropped above. No storage-ownership wiring is needed: a comment
    # produces no rendered element, so it creates no S0->C1 storage delta.
    "html-comment-dropped": "removed",
    # An empty paragraph carries no content, only vertical space. Naming the
    # loss is enough; blocking a page over it is the 0.3.0 mistake.
    "empty-paragraph-dropped": "removed",
    # A remote-only decoration/metadata attribute (li data-uuid, heading style,
    # table class, ...) has no Markdown projection. The drop is exact and
    # per-attribute, so a user can consent to it instead of failing closed.
    "element-attribute-omitted": "removed",
    # The round-trip re-renders an attribute value in its canonical form (a
    # color style coarsened to its palette token). Same rendered meaning,
    # different storage bytes.
    "element-attribute-normalized": "normalized",
    # Markdown table cells cannot carry block paragraphs; several cell
    # paragraphs collapse into one line-broken cell at a named location.
    "table-cell-paragraphs-collapsed": "removed",
    # Markdown tables cannot express <colgroup> column widths; the subtree is
    # dropped whole at a named location, so the loss is bounded and consentable.
    "table-colgroup-omitted": "removed",
    # A nested ordered list whose first number is not 1 cannot interrupt inline
    # list-item text in Markdown. This is an UNREPRESENTABLE shape, not a
    # consentable rewrite: it fails closed (fatal) and requires a manual remote
    # rewrite to a flat list. Fatal keeps it out of _MIGRATION_EFFECTS, so it can
    # never become a migration owner or be consented to; it is named only so an
    # operator can fix it. (Registered rather than left to the fatal default so the
    # diagnostic inventory gate stays satisfied for this emitted code.)
    "nested-ordered-list-start-unrepresentable": "fatal",
    # The table's shape changed, so its pulled presentation could not be
    # reattached. The affected table is identified and its presentation is
    # dropped whole, so the loss is bounded and can be described exactly --
    # unlike table-presentation-ambiguous below, where the target is unknown.
    "table-topology-changed": "removed",
    # Normalized: the candidate renders the same but the storage bytes differ.
    # Consent is still required while CANONICAL_EQUIVALENT_CODES is empty,
    # because no fixed-point evidence has been produced for these yet.
    "list-item-paragraph-unwrapped": "normalized",
    # The table-cell sibling of the list-item unwrap above: same rendered
    # output, different storage bytes.
    "table-cell-paragraph-unwrapped": "normalized",
    "trailing-space-trimmed": "normalized",
    # Unsupported: cfxmark cannot interpret the construct at all.
    "unsupported-confluence-content": "unsupported",
    # Fatal: no coherent candidate or proof exists, so consent cannot apply.
    #
    # conversion-note is the fallback code for producer messages this contract
    # has never classified. Treating it as informational would make the unknown
    # case the silent case, which is the failure mode the whole report exists to
    # prevent.
    "conversion-note": "fatal",
    "asset-marker-edited": "fatal",
    "managed-base-roundtrip-changed": "fatal",
    "migration-user-overlap-ambiguous": "fatal",
    "multiple-change-owners": "fatal",
    "protected-region-edited": "fatal",
    "protected-region-splice-incomplete": "fatal",
    "remote-subtree-splice-incomplete": "fatal",
    "semantic-mapping-ambiguous": "fatal",
    # The presentation authority cannot say which table it belongs to. A report
    # that cannot name its target cannot be consented to: approving it would
    # authorise an unknown change to an unknown table, which is precisely the
    # ambiguity the ownership invariant refuses elsewhere.
    "table-presentation-ambiguous": "fatal",
    "semantic-source-map-incomplete": "fatal",
    "splice-source-mismatch": "fatal",
    "table-cell-background-conflict": "fatal",
    "table-cell-background-invalid": "fatal",
    "table-cell-span-limit-exceeded": "fatal",
    "table-cell-span-overlap": "fatal",
    "table-style-edit-not-supported": "fatal",
    "unclassified-storage-change": "fatal",
}

# A proof code belongs here only while it has no diagnostic producer. Public
# ownership failures are now real producer callsites and therefore live in the
# audited producer registry above.
_SYNTHETIC_EFFECTS: dict[str, MigrationEffect] = {}

_EFFECT_BY_CODE: dict[str, MigrationEffect] = {**_PRODUCER_EFFECTS, **_SYNTHETIC_EFFECTS}


def effect_for_code(code: str) -> MigrationEffect:
    """Resolve a producer code to its effect, defaulting to fatal.

    An unregistered code means a producer emitted something this contract has
    never classified. Treating that as a harmless note would let new losses
    reach a page under an approval that never described them, so the default
    fails closed and forces the code to be registered deliberately.
    """
    return _EFFECT_BY_CODE.get(code, "fatal")


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _reject_raw_payload(name: str, value: str | None) -> None:
    if value is None:
        return
    if not _FINGERPRINT_RE.match(value):
        raise ValueError(
            f"{name} must be a sha256 fingerprint, not a raw payload; "
            "storage and Markdown content must never enter the report"
        )


@dataclass(frozen=True)
class LocationComponent:
    """One typed step of a location path.

    Typed components rather than a rendered XPath string: a string would invite
    parsing it back, and any change to its formatting would silently change the
    hash without any change to what the location means.
    """

    kind: str
    stable_id: str | None = None
    anchor: str | None = None
    name: str | None = None
    ordinal: int | None = None

    def canonical(self) -> dict[str, Any]:
        document: dict[str, Any] = {"kind": _nfc(self.kind)}
        if self.stable_id is not None:
            document["stable_id"] = _nfc(self.stable_id)
        if self.anchor is not None:
            document["anchor"] = _nfc(self.anchor)
        if self.name is not None:
            document["name"] = _nfc(self.name)
        if self.ordinal is not None:
            document["ordinal"] = self.ordinal
        return document


def canonical_location(location_identity: tuple[LocationComponent, ...]) -> str:
    return json.dumps(
        [component.canonical() for component in location_identity],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def occurrence_identity(
    *,
    source_storage_sha256: str,
    code: str,
    location_identity: tuple[LocationComponent, ...],
    ordinal: int,
) -> str:
    """Derive a stable id for one atomic occurrence.

    The ordinal distinguishes repeated atoms sharing a code and location; it is
    assigned from canonical source order, never from iteration order or object
    identity, so the same document always yields the same ids.
    """
    digest = hashlib.sha256()
    digest.update(b"cfxmark-migration-occurrence-v1\0")
    digest.update(source_storage_sha256.encode("utf-8"))
    digest.update(b"\0")
    digest.update(_nfc(code).encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical_location(location_identity).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(ordinal).encode("utf-8"))
    return f"mig_occ_sha256:{digest.hexdigest()}"


@dataclass(frozen=True)
class MigrationOccurrence:
    """One atomic impact. Consent-relevant fields first, display-only after."""

    occurrence_id: str
    code: str
    effect: MigrationEffect
    category: str
    location_identity: tuple[LocationComponent, ...]
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None

    # Display-only. Excluded from the hash so wording can improve without
    # invalidating an approval.
    before_summary: str | None = field(default=None, compare=False)
    after_summary: str | None = field(default=None, compare=False)
    user_impact: str | None = field(default=None, compare=False)
    suggested_workflow: str | None = field(default=None, compare=False)
    display_label: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _reject_raw_payload("before_fingerprint", self.before_fingerprint)
        _reject_raw_payload("after_fingerprint", self.after_fingerprint)

    def consent_relevant(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "occurrence_id": self.occurrence_id,
            "code": _nfc(self.code),
            "effect": self.effect,
            "category": _nfc(self.category),
            "location_identity": [component.canonical() for component in self.location_identity],
        }
        if self.before_fingerprint is not None:
            document["before_fingerprint"] = self.before_fingerprint
        if self.after_fingerprint is not None:
            document["after_fingerprint"] = self.after_fingerprint
        return document

    def _sort_key(self) -> tuple[str, str, str]:
        return canonical_location(self.location_identity), _nfc(self.code), self.occurrence_id

    @property
    def consent_required(self) -> bool:
        if self.effect in _CONSENT_EFFECTS:
            return True
        if self.effect == "normalized":
            return self.code not in CANONICAL_EQUIVALENT_CODES
        return False


@dataclass(frozen=True)
class MigrationGroup:
    """A derived, display-oriented view over occurrences sharing a code and scope.

    A table with 220 styled cells produces 220 atoms. Those atoms stay
    authoritative for consent, but showing them one by one buries the reader, so
    the human summary reads them as a single line per code per block.
    """

    code: str
    effect: MigrationEffect
    scope: LocationComponent | None
    count: int
    occurrence_ids: tuple[str, ...]


@dataclass(frozen=True)
class MigrationReport:
    schema: str
    occurrences: tuple[MigrationOccurrence, ...]

    @classmethod
    def build(cls, *, occurrences: tuple[MigrationOccurrence, ...]) -> MigrationReport:
        """Sort into canonical order so callers cannot influence the hash."""
        return cls(schema=SCHEMA, occurrences=tuple(sorted(occurrences, key=lambda item: item._sort_key())))

    def canonical_json(self) -> str:
        consent_occurrences = [occurrence for occurrence in self.occurrences if occurrence.effect != "preserved"]
        return json.dumps(
            {
                "schema": self.schema,
                "occurrences": [occurrence.consent_relevant() for occurrence in consent_occurrences],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_json().encode('utf-8')).hexdigest()}"

    @property
    def fatal(self) -> bool:
        return any(occurrence.effect == "fatal" for occurrence in self.occurrences)

    @property
    def candidate_available(self) -> bool:
        """A fatal occurrence means no coherent candidate exists to publish."""
        return not self.fatal

    @property
    def consent_required(self) -> bool:
        return any(occurrence.consent_required for occurrence in self.occurrences)

    def counts(self) -> dict[str, int]:
        """Derived summary. Never part of the hash: it is a view of the atoms.

        Counts sum occurrences, not groups, so a grouped display can never make
        220 removals look like one.
        """
        totals: dict[str, int] = {}
        for occurrence in self.occurrences:
            totals[occurrence.effect] = totals.get(occurrence.effect, 0) + 1
        return totals

    def grouped(self) -> tuple[MigrationGroup, ...]:
        """Collapse repeated atoms by code and enclosing scope, for display only.

        Order follows the canonical occurrence order, so the grouped view is as
        deterministic as the report it derives from.
        """
        buckets: dict[tuple[str, LocationComponent | None], list[MigrationOccurrence]] = {}
        for occurrence in self.occurrences:
            scope = occurrence.location_identity[0] if occurrence.location_identity else None
            buckets.setdefault((occurrence.code, scope), []).append(occurrence)
        return tuple(
            MigrationGroup(
                code=code,
                effect=items[0].effect,
                scope=scope,
                count=len(items),
                occurrence_ids=tuple(item.occurrence_id for item in items),
            )
            for (code, scope), items in buckets.items()
        )


def _location_components(location: Any) -> tuple[LocationComponent, ...]:
    """Project a DiagnosticLocation onto typed components.

    Taken structurally rather than by importing the diagnostics module, which
    would make the dependency circular once diagnostics start reporting through
    this contract.
    """
    components: list[LocationComponent] = [
        LocationComponent(kind="path", name=step) for step in getattr(location, "semantic_path", ())
    ]
    row = getattr(location, "table_row", None)
    column = getattr(location, "table_column", None)
    if row is not None or column is not None:
        components.append(LocationComponent(kind="cell", anchor=_a1_anchor(row, column)))
    return tuple(components)


def _column_letters(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA, in the spreadsheet convention."""
    letters = ""
    current = index
    while True:
        letters = chr(ord("A") + current % 26) + letters
        current = current // 26 - 1
        if current < 0:
            return letters


def _a1_anchor(row: int | None, column: int | None) -> str:
    """Address a cell the way the reader sees it.

    Internally rows and columns are 0-based, but a report is read against the
    table on screen, where the first cell is A1 and never r0c0. Translating at
    the boundary keeps the indices honest inside and the anchor usable outside.
    """
    column_part = _column_letters(column) if column is not None else "?"
    row_part = str(row + 1) if row is not None else "?"
    return f"{column_part}{row_part}"


def occurrences_from_diagnostics(
    diagnostics: Any,
    *,
    source_storage_sha256: str,
) -> tuple[MigrationOccurrence, ...]:
    """Translate the producer diagnostic stream into consent-relevant atoms.

    Producer wording stays out of the result entirely: the message is a display
    concern, and letting it anchor consent would invalidate approvals whenever a
    sentence is reworded.

    Repeated diagnostics stay repeated. Each is an occurrence-scoped
    authorization record, so collapsing duplicates here would let one approval
    silently cover several source constructs.
    """
    ordinals: dict[tuple[str, str], int] = {}
    occurrences: list[MigrationOccurrence] = []
    for diagnostic in diagnostics:
        code = diagnostic.code
        location_identity = _location_components(diagnostic.location)
        key = (code, canonical_location(location_identity))
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        occurrences.append(
            MigrationOccurrence(
                occurrence_id=occurrence_identity(
                    source_storage_sha256=source_storage_sha256,
                    code=code,
                    location_identity=location_identity,
                    ordinal=ordinal,
                ),
                code=code,
                effect=effect_for_code(code),
                category=diagnostic.category,
                location_identity=location_identity,
                display_label=diagnostic.message,
            )
        )
    return tuple(occurrences)


__all__ = [
    "CANONICAL_EQUIVALENT_CODES",
    "SCHEMA",
    "LocationComponent",
    "MigrationEffect",
    "MigrationGroup",
    "MigrationOccurrence",
    "MigrationReport",
    "canonical_location",
    "effect_for_code",
    "occurrence_identity",
    "occurrences_from_diagnostics",
]
