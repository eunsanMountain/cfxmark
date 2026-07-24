"""Canonical migration report contract (plan sections 7.1 and 7.2, Story 1).

The report is what a user consents to. If two runs over the same input can
produce different bytes, the fingerprint stops meaning anything and consent
degrades into a rubber stamp, so determinism is tested here as a property of
the report rather than assumed from the producer.

The other half is the split between what consent covers and what it does not:
rewording a human-facing summary must not invalidate an approval, while any
change to code, effect, location or the before/after values must.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unicodedata

import pytest

from cfxmark.migration_report import (
    CANONICAL_EQUIVALENT_CODES,
    LocationComponent,
    MigrationOccurrence,
    MigrationReport,
    effect_for_code,
    occurrence_identity,
)

SOURCE_SHA = "sha256:" + "a" * 64


def _location(*, table: str = "t1", cell: str = "B3") -> tuple[LocationComponent, ...]:
    return (
        LocationComponent(kind="table", stable_id=table),
        LocationComponent(kind="cell", anchor=cell),
        LocationComponent(kind="attribute", name="background"),
    )


def _occurrence(
    *,
    code: str = "table-cell-background-untracked",
    effect: str = "removed",
    category: str = "presentation_loss",
    location: tuple[LocationComponent, ...] | None = None,
    ordinal: int = 0,
    before_fingerprint: str = "sha256:" + "b" * 64,
    **display: str,
) -> MigrationOccurrence:
    identity = location if location is not None else _location()
    return MigrationOccurrence(
        occurrence_id=occurrence_identity(
            source_storage_sha256=SOURCE_SHA,
            code=code,
            location_identity=identity,
            ordinal=ordinal,
        ),
        code=code,
        effect=effect,
        category=category,
        location_identity=identity,
        before_fingerprint=before_fingerprint,
        after_fingerprint=None,
        **display,
    )


# ---------------------------------------------------------------------------
# Effect classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "effect"),
    [
        ("emoticon-to-unicode", "converted"),
        ("unsupported-confluence-content", "unsupported"),
        ("table-cell-background-untracked", "removed"),
        ("semantic-mapping-ambiguous", "fatal"),
        ("table-cell-span-overlap", "fatal"),
        ("unclassified-storage-change", "fatal"),
    ],
)
def test_effect_is_resolved_from_the_code_registry(code: str, effect: str) -> None:
    assert effect_for_code(code) == effect


def test_an_unregistered_code_is_fatal_rather_than_assumed_harmless() -> None:
    """A new producer message must not silently become a consentable loss."""
    assert effect_for_code("some-code-nobody-registered") == "fatal"


# ---------------------------------------------------------------------------
# Consent boundaries
# ---------------------------------------------------------------------------


def test_preserved_needs_no_consent() -> None:
    report = MigrationReport.build(occurrences=(_occurrence(code="link-preserved", effect="preserved"),))
    assert report.consent_required is False
    assert report.fatal is False


@pytest.mark.parametrize("effect", ["converted", "removed", "unsupported"])
def test_lossy_effects_require_consent(effect: str) -> None:
    report = MigrationReport.build(occurrences=(_occurrence(effect=effect),))
    assert report.consent_required is True
    assert report.fatal is False


def test_normalized_requires_consent_unless_the_code_is_allowlisted() -> None:
    """Plan 7.1: class/style may not be declared cosmetic-equivalent wholesale.

    The allowlist is empty in 0.3.0, so every normalized occurrence currently
    needs consent. The exemption path is still exercised here so that adding an
    entry cannot quietly change the rule for everything else.
    """
    assert (
        MigrationReport.build(
            occurrences=(_occurrence(code="style-attribute-reordered", effect="normalized"),)
        ).consent_required
        is True
    )
    for allowlisted in CANONICAL_EQUIVALENT_CODES:
        assert (
            MigrationReport.build(occurrences=(_occurrence(code=allowlisted, effect="normalized"),)).consent_required
            is False
        )


def test_fatal_cannot_be_waived_by_consent() -> None:
    report = MigrationReport.build(occurrences=(_occurrence(code="semantic-mapping-ambiguous", effect="fatal"),))
    assert report.fatal is True
    assert report.candidate_available is False


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------


def test_canonical_json_uses_sorted_keys_and_compact_separators() -> None:
    report = MigrationReport.build(occurrences=(_occurrence(),))
    payload = report.canonical_json()

    assert ", " not in payload and '": ' not in payload, "separators must be (',', ':')"
    document = json.loads(payload)
    assert list(document) == sorted(document)
    assert payload == payload.encode("utf-8").decode("utf-8")


def test_absent_optional_fields_are_omitted_rather_than_null() -> None:
    report = MigrationReport.build(occurrences=(_occurrence(),))
    item = json.loads(report.canonical_json())["occurrences"][0]

    assert "after_fingerprint" not in item
    assert None not in item.values()


def test_display_only_fields_never_reach_the_hash() -> None:
    """Improving wording must not invalidate an approval the user already gave."""
    plain = MigrationReport.build(occurrences=(_occurrence(),))
    described = MigrationReport.build(
        occurrences=(
            _occurrence(
                before_summary="a red cell",
                after_summary="a plain cell",
                user_impact="The highlight colour disappears.",
                suggested_workflow="patch-text",
                display_label="Table 1, cell B3",
            ),
        )
    )

    assert described.sha256() == plain.sha256()
    assert "user_impact" not in plain.canonical_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "layout-flattened"),
        ("effect", "converted"),
        ("category", "structural_loss"),
        ("before_fingerprint", "sha256:" + "c" * 64),
    ],
)
def test_consent_relevant_fields_change_the_hash(field: str, value: str) -> None:
    baseline = MigrationReport.build(occurrences=(_occurrence(),))
    mutated = MigrationReport.build(occurrences=(_occurrence(**{field: value}),))
    assert mutated.sha256() != baseline.sha256()


def test_location_change_changes_the_hash() -> None:
    baseline = MigrationReport.build(occurrences=(_occurrence(),))
    mutated = MigrationReport.build(occurrences=(_occurrence(location=_location(cell="C4")),))
    assert mutated.sha256() != baseline.sha256()


def test_text_is_normalized_to_nfc_before_hashing() -> None:
    decomposed = unicodedata.normalize("NFD", "Café")
    composed = unicodedata.normalize("NFC", "Café")
    assert decomposed != composed

    left = MigrationReport.build(
        occurrences=(_occurrence(location=(LocationComponent(kind="macro", name=decomposed),)),)
    )
    right = MigrationReport.build(
        occurrences=(_occurrence(location=(LocationComponent(kind="macro", name=composed),)),)
    )
    assert left.sha256() == right.sha256()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_item_order_is_independent_of_insertion_order() -> None:
    first = _occurrence(location=_location(cell="A1"), ordinal=0)
    second = _occurrence(location=_location(cell="B2"), ordinal=1)

    assert MigrationReport.build(occurrences=(first, second)).sha256() == (
        MigrationReport.build(occurrences=(second, first)).sha256()
    )


def test_occurrence_identity_is_domain_separated_and_ordinal_scoped() -> None:
    same_location = _location()
    first = occurrence_identity(source_storage_sha256=SOURCE_SHA, code="x", location_identity=same_location, ordinal=0)
    second = occurrence_identity(source_storage_sha256=SOURCE_SHA, code="x", location_identity=same_location, ordinal=1)
    other_code = occurrence_identity(
        source_storage_sha256=SOURCE_SHA, code="y", location_identity=same_location, ordinal=0
    )

    assert first != second, "repeated atoms at one location must stay distinct"
    assert first != other_code
    assert first.startswith("mig_occ_sha256:")


def test_hash_is_stable_across_interpreter_hash_seeds() -> None:
    """A report whose bytes depend on PYTHONHASHSEED cannot anchor consent."""
    script = (
        "import json;"
        "from cfxmark.migration_report import LocationComponent, MigrationOccurrence, MigrationReport,"
        " occurrence_identity;"
        f"loc=(LocationComponent(kind='table',stable_id='t1'),LocationComponent(kind='cell',anchor='B3'));"
        f"occ=MigrationOccurrence(occurrence_id=occurrence_identity(source_storage_sha256='{SOURCE_SHA}',"
        "code='c',location_identity=loc,ordinal=0),code='c',effect='removed',category='presentation_loss',"
        "location_identity=loc,before_fingerprint='sha256:'+'b'*64);"
        "print(MigrationReport.build(occurrences=(occ,)).sha256())"
    )
    digests = set()
    for seed in ("0", "1", "12345"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            check=True,
        )
        digests.add(completed.stdout.strip())
    assert len(digests) == 1, digests


# ---------------------------------------------------------------------------
# Payload safety
# ---------------------------------------------------------------------------


def test_report_never_carries_raw_storage_or_markdown() -> None:
    """Only fingerprints cross this boundary; payloads would leak page content."""
    secret = "CONFIDENTIAL BODY TEXT"
    report = MigrationReport.build(
        occurrences=(
            _occurrence(
                before_summary=f"was {secret}",
                user_impact=f"removes {secret}",
            ),
        )
    )
    assert secret not in report.canonical_json()

    with pytest.raises(ValueError, match="raw payload"):
        MigrationOccurrence(
            occurrence_id="mig_occ_sha256:" + "0" * 64,
            code="c",
            effect="removed",
            category="presentation_loss",
            location_identity=_location(),
            before_fingerprint="<p>raw storage</p>",
        )


def test_counts_are_derived_and_not_part_of_the_hash() -> None:
    report = MigrationReport.build(
        occurrences=(
            _occurrence(location=_location(cell="A1"), ordinal=0),
            _occurrence(location=_location(cell="B2"), ordinal=1, effect="converted"),
        )
    )
    assert report.counts() == {"removed": 1, "converted": 1}
    assert "counts" not in report.canonical_json()


# ---------------------------------------------------------------------------
# Grouping for display (plan section 7.2)
# ---------------------------------------------------------------------------


def test_repeated_occurrences_group_by_code_and_scope() -> None:
    """220 styled cells must read as one line, not 220 of them.

    The atoms stay authoritative for consent; grouping is a derived view so a
    large page does not drown the reader in identical entries.
    """
    occurrences = tuple(_occurrence(location=_location(cell=f"A{index}"), ordinal=index) for index in range(220))
    report = MigrationReport.build(occurrences=occurrences)

    groups = report.grouped()
    assert len(groups) == 1
    group = groups[0]
    assert group.code == "table-cell-background-untracked"
    assert group.effect == "removed"
    assert group.count == 220
    assert group.scope == LocationComponent(kind="table", stable_id="t1")
    assert len(group.occurrence_ids) == 220


def test_groups_separate_distinct_codes_and_scopes() -> None:
    report = MigrationReport.build(
        occurrences=(
            _occurrence(location=_location(table="t1", cell="A1"), ordinal=0),
            _occurrence(location=_location(table="t2", cell="A1"), ordinal=1),
            _occurrence(code="layout-flattened", effect="removed", location=_location(table="t1", cell="A1")),
        )
    )
    assert len({(group.code, group.scope) for group in report.grouped()}) == 3


def test_group_counts_sum_atoms_not_groups() -> None:
    """Plan 7.2: an effect count is the number of occurrences, not of groups."""
    report = MigrationReport.build(
        occurrences=tuple(_occurrence(location=_location(cell=f"A{i}"), ordinal=i) for i in range(5))
    )
    assert len(report.grouped()) == 1
    assert report.counts() == {"removed": 5}


def test_grouping_is_not_part_of_the_hash() -> None:
    report = MigrationReport.build(occurrences=(_occurrence(),))
    assert "grouped" not in report.canonical_json()
    assert "scope" not in report.canonical_json()


# ---------------------------------------------------------------------------
# Bridge from the existing diagnostic stream
# ---------------------------------------------------------------------------


def test_diagnostics_translate_to_occurrences_with_stable_identity() -> None:
    from cfxmark.diagnostics import diagnostics_from_messages
    from cfxmark.migration_report import occurrences_from_diagnostics

    diagnostics = diagnostics_from_messages(
        warnings=(),
        losses=["Confluence layout structure is flattened in Markdown"],
        source="<p>x</p>",
    )
    first = occurrences_from_diagnostics(diagnostics, source_storage_sha256=SOURCE_SHA)
    second = occurrences_from_diagnostics(diagnostics, source_storage_sha256=SOURCE_SHA)

    assert len(first) == 1
    assert first[0].code == "layout-flattened"
    assert first[0].effect == "removed"
    assert first[0].occurrence_id == second[0].occurrence_id


def test_repeated_diagnostics_at_one_location_keep_distinct_occurrence_ids() -> None:
    """Deduplicating here would let one approval cover several source constructs.

    The location is pinned to the same value on purpose. Without that the two
    diagnostics land on distinct synthetic paths (source[0], source[1]) and the
    ids differ for a reason unrelated to the ordinal, so the ordinal itself
    would never be exercised.
    """
    from cfxmark.diagnostics import DiagnosticLocation, diagnostics_from_messages
    from cfxmark.migration_report import occurrences_from_diagnostics

    message = "Confluence layout structure is flattened in Markdown"
    location = DiagnosticLocation(semantic_path=("table[0]", "tr[1]"))
    diagnostics = diagnostics_from_messages(
        warnings=(),
        losses=[message, message],
        source="<p>x</p>",
        loss_locations=[location, location],
    )
    occurrences = occurrences_from_diagnostics(diagnostics, source_storage_sha256=SOURCE_SHA)

    assert len(occurrences) == 2
    assert occurrences[0].location_identity == occurrences[1].location_identity
    assert occurrences[0].occurrence_id != occurrences[1].occurrence_id


def test_an_unknown_producer_message_becomes_a_fatal_occurrence() -> None:
    from cfxmark.diagnostics import diagnostics_from_messages
    from cfxmark.migration_report import occurrences_from_diagnostics

    diagnostics = diagnostics_from_messages(
        warnings=(), losses=["something no registry has ever seen"], source="<p>x</p>"
    )
    report = MigrationReport.build(
        occurrences=occurrences_from_diagnostics(diagnostics, source_storage_sha256=SOURCE_SHA)
    )
    assert report.fatal is True
    assert report.candidate_available is False


def test_translation_carries_no_producer_message_into_the_hash() -> None:
    """Producer wording is display-only; it must not anchor consent."""
    from cfxmark.diagnostics import diagnostics_from_messages
    from cfxmark.migration_report import occurrences_from_diagnostics

    diagnostics = diagnostics_from_messages(
        warnings=(),
        losses=["Confluence layout structure is flattened in Markdown"],
        source="<p>x</p>",
    )
    report = MigrationReport.build(
        occurrences=occurrences_from_diagnostics(diagnostics, source_storage_sha256=SOURCE_SHA)
    )
    assert "flattened in Markdown" not in report.canonical_json()
