"""The migration registry must describe the diagnostics that actually exist.

This gate exists because the first version of the registry did not. It was
written from the plan's illustrative examples rather than from the pipeline, so
11 of the 24 emittable codes were missing. Every one of them fell through to the
fatal default, which would have blocked essentially every page the moment the
report was wired in — and no test noticed, because the tests used the same
invented code names as the registry.

Internal consistency is not enough here. The registry has to be checked against
the producers, mechanically, the same way the CLI inventory is checked against
the Typer tree.

The first version of this gate only understood a literal ``code="..."`` argument.
That left five codes invisible — they reach ``make_diagnostic`` through a
variable — so they were absent from the registry and fell through to fatal
exactly like the original eleven. Matching syntax is not an inventory; the code
argument has to be resolved. See ``tests/support/diagnostic_extraction.py``.
"""

from __future__ import annotations

import pathlib

import pytest

from cfxmark.diagnostics import _EXACT_MESSAGE_CODES
from cfxmark.migration_report import (
    _PRODUCER_EFFECTS,
    _SYNTHETIC_EFFECTS,
    CANONICAL_EQUIVALENT_CODES,
    effect_for_code,
)
from tests.support.diagnostic_extraction import CodeCallsite, DiagnosticExtractor

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "cfxmark"

CONSENTABLE = {"converted", "removed", "unsupported"}

# Constant lookup tables a producer may resolve its code through. Passing the
# real objects keeps the gate honest: a renamed or re-keyed table changes what
# the resolver sees instead of quietly matching a stale literal.
_TABLES = {"_EXACT_MESSAGE_CODES": _EXACT_MESSAGE_CODES}


def _callsites() -> list[CodeCallsite]:
    return DiagnosticExtractor(SRC, tables=_TABLES).callsites()


def _emittable_codes() -> dict[str, str]:
    """Every diagnostic code the package can emit, with where it comes from."""
    found: dict[str, str] = {code: "diagnostics._EXACT_MESSAGE_CODES" for code, _c, _s in _EXACT_MESSAGE_CODES.values()}
    for site in _callsites():
        for code in site.codes:
            found.setdefault(code, f"{site.origin}:{site.line}")
    return found


def test_every_diagnostic_callsite_resolves_to_known_codes() -> None:
    """A producer this gate cannot read is a producer it cannot police.

    Failing here rather than skipping is the point. The alternative — ignoring
    arguments that are not literals — is what let five codes emit for months
    without an effect classification.
    """
    unresolved = [site for site in _callsites() if site.unresolved]
    assert not unresolved, (
        "make_diagnostic callsites whose code argument could not be resolved:\n  "
        + "\n  ".join(f"{site.origin}:{site.line}" for site in unresolved)
        + "\nExtend tests/support/diagnostic_extraction.py, or pass a literal code."
    )


def test_dynamic_callsites_are_still_covered_by_the_registry() -> None:
    """The codes behind a variable get no exemption from classification."""
    offenders: list[str] = []
    for site in _callsites():
        if site.literal:
            continue
        for code in sorted(site.codes):
            if code not in _PRODUCER_EFFECTS:
                offenders.append(f"{code}  ({site.origin}:{site.line})")
    assert not offenders, "dynamically selected codes missing from _PRODUCER_EFFECTS:\n  " + "\n  ".join(offenders)


def test_the_inventory_still_sees_the_known_dynamic_producers() -> None:
    """Guards the resolver itself.

    If a refactor breaks resolution, the two tests above could pass trivially by
    finding nothing at all. These codes are only reachable through a variable, so
    their presence proves the dynamic paths are still being followed.
    """
    dynamic = {code for site in _callsites() if not site.literal for code in site.codes}
    for code in (
        "table-topology-changed",  # api.py, chosen by a conditional expression
        "table-presentation-ambiguous",
        "semantic-mapping-ambiguous",  # api.py, read off the exception class
        "empty-paragraph-dropped",  # artifacts.py, unpacked from a specs tuple
        "list-item-paragraph-unwrapped",
        "trailing-space-trimmed",
        "table-cell-background-untracked",  # artifacts.py, assigned in a branch
        "html-tag-stripped",  # diagnostics.py, unpacked from a spec assignment
    ):
        assert code in dynamic, f"{code} is no longer reached through a dynamic callsite"


def test_every_emittable_code_is_registered() -> None:
    """The failure this gate was built for."""
    emittable = _emittable_codes()
    missing = sorted(code for code in emittable if code not in _PRODUCER_EFFECTS)
    assert not missing, (
        "diagnostic codes are emitted but absent from _PRODUCER_EFFECTS, so they "
        "silently resolve to fatal:\n  " + "\n  ".join(f"{code}  ({emittable[code]})" for code in missing)
    )


def test_the_registry_does_not_invent_producers() -> None:
    """A registered code with no producer is an unverified guess.

    Synthetic ownership codes are exempt: they are raised by the proof, not by a
    converter, so they legitimately have no diagnostic producer.
    """
    emittable = set(_emittable_codes())
    invented = sorted(code for code in _PRODUCER_EFFECTS if code not in emittable)
    assert not invented, f"registered as producer codes but never emitted: {invented}"


def test_synthetic_and_producer_codes_do_not_overlap() -> None:
    assert not (set(_SYNTHETIC_EFFECTS) & set(_PRODUCER_EFFECTS))


def test_public_ownership_codes_are_real_fatal_producers() -> None:
    """A public proof failure cannot bypass producer inventory as synthetic."""
    ownership_codes = {
        "migration-user-overlap-ambiguous",
        "multiple-change-owners",
        "unclassified-storage-change",
    }
    assert _SYNTHETIC_EFFECTS == {}
    assert {_PRODUCER_EFFECTS[code] for code in ownership_codes} == {"fatal"}


def test_generic_conversion_note_is_not_treated_as_harmless() -> None:
    """conversion-note is the code for a message nobody classified.

    Its meaning is "we do not know what happened here", so it must fail closed
    rather than pass as an informational aside.
    """
    assert effect_for_code("conversion-note") == "fatal"


@pytest.mark.parametrize(
    "code",
    [
        "semantic-source-map-incomplete",
        "semantic-mapping-ambiguous",
        "table-presentation-ambiguous",
        "managed-base-roundtrip-changed",
        "protected-region-edited",
        "protected-region-splice-incomplete",
        "remote-subtree-splice-incomplete",
        "splice-source-mismatch",
        "table-cell-span-overlap",
        "table-cell-span-limit-exceeded",
    ],
)
def test_proof_failures_stay_fatal_and_are_never_downgraded(code: str) -> None:
    """These say the candidate proof does not hold.

    Downgrading any of them to a consentable loss would let a user approve a
    publication whose correctness was never established — the loss would not
    just be accepted, it would be unquantified.
    """
    assert effect_for_code(code) == "fatal"


@pytest.mark.parametrize(
    "code",
    [
        "table-cell-background-untracked",
        "table-cell-background-unrenderable",
        "table-column-alignment-mixed",
        "layout-flattened",
        "xml-comment-dropped",
        "html-comment-dropped",
        "html-tag-stripped",
        "attachment-url-components-unrenderable",
        "body-only-table-header-synthesized",
        "empty-paragraph-dropped",
        "table-topology-changed",
    ],
)
def test_explainable_losses_are_consentable_rather_than_fatal(code: str) -> None:
    """Plan 7.1: a describable loss is a decision, not a wall.

    Blocking these was the 0.2.x-to-0.3.0 failure that made nearly every
    non-trivial page unpublishable. They lose information, the report says
    exactly what, and the user decides.
    """
    assert effect_for_code(code) in CONSENTABLE


def test_source_bound_table_background_is_preserved_not_consentable_loss() -> None:
    assert effect_for_code("table-cell-background-omitted") == "preserved"


@pytest.mark.parametrize("code", ["list-item-paragraph-unwrapped", "trailing-space-trimmed"])
def test_normalized_losses_still_require_consent_without_evidence(code: str) -> None:
    """Plan P3: whitespace and wrapper canonicalisation are consent, not silence.

    These render identically, which is exactly the argument that would justify an
    exemption -- but the exemption needs fixed-point evidence, and none has been
    produced. Until then they behave like any other describable loss.
    """
    assert effect_for_code(code) == "normalized"
    assert code not in CANONICAL_EQUIVALENT_CODES


def test_ambiguity_and_describable_table_loss_are_classified_apart() -> None:
    """The pair that motivated the distinction, kept side by side on purpose."""
    assert effect_for_code("table-presentation-ambiguous") == "fatal"
    assert effect_for_code("table-topology-changed") in CONSENTABLE


def test_canonical_equivalent_allowlist_entries_have_a_real_producer() -> None:
    """An exemption may only be granted to something that actually exists."""
    emittable = set(_emittable_codes())
    unbacked = sorted(code for code in CANONICAL_EQUIVALENT_CODES if code not in emittable)
    assert not unbacked, f"consent exemption granted to codes with no producer: {unbacked}"


def test_canonical_equivalent_allowlist_entries_are_normalized() -> None:
    for code in CANONICAL_EQUIVALENT_CODES:
        assert effect_for_code(code) == "normalized", code


def test_allowlist_is_empty_until_fixed_point_evidence_exists() -> None:
    """Plan 7.1 requires per-code fixed-point and ownership evidence.

    Kept as an explicit assertion rather than a comment: adding an entry should
    make this fail, forcing whoever adds it to also add the evidence and update
    this test deliberately.
    """
    assert frozenset() == CANONICAL_EQUIVALENT_CODES


def test_registry_effects_are_all_valid() -> None:
    valid = {"preserved", "converted", "normalized", "removed", "unsupported", "fatal"}
    for code, effect in {**_PRODUCER_EFFECTS, **_SYNTHETIC_EFFECTS}.items():
        assert effect in valid, f"{code} -> {effect}"
