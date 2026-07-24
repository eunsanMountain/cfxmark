"""The migration report has to reach the caller (plan Story 1).

A report nothing can read is not a safety mechanism. Until it is exposed on the
public artifacts, every consumer is still left to interpret a raw diagnostic
tuple and decide for itself what blocks a push -- which is the arrangement the
report was written to replace.

The unknown-macro cases at the bottom pin a contract that turned out to be
narrower than it first looked. See the comment there.
"""

from __future__ import annotations

import pytest

from cfxmark import api
from cfxmark.api import ConversionOptions
from cfxmark.migration_report import MigrationReport

EDITABLE = ConversionOptions(profile="editable")
LEGACY = ConversionOptions(profile="legacy")

# An unknown macro, in the two shapes that store their payload differently: one
# keeps it in a parameter, the other in a CDATA body.
MACRO_PARAMETER_ONLY = (
    "<p>before</p>"
    '<ac:structured-macro ac:name="totally-unknown">'
    '<ac:parameter ac:name="key">value</ac:parameter>'
    "</ac:structured-macro>"
    "<p>after</p>"
)
MACRO_PLAIN_TEXT_BODY = (
    "<p>before</p>"
    '<ac:structured-macro ac:name="totally-unknown">'
    "<ac:plain-text-body><![CDATA[body payload]]></ac:plain-text-body>"
    "</ac:structured-macro>"
    "<p>after</p>"
)

LOSSY = "<p>a<!-- dropped comment --></p>"


# ---------------------------------------------------------------------------
# The report is reachable
# ---------------------------------------------------------------------------


def test_markdown_artifact_exposes_a_migration_report() -> None:
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    report = artifact.migration_report
    assert isinstance(report, MigrationReport)


def test_the_report_covers_every_diagnostic_the_artifact_carries() -> None:
    """No diagnostic may be dropped on the way into the report."""
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    assert {item.code for item in artifact.diagnostics} == {
        occurrence.code for occurrence in artifact.migration_report.occurrences
    }


def test_the_report_is_derived_from_this_artifacts_source() -> None:
    """Occurrence identity is bound to the storage it came from."""
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    other = api.to_md_artifact("<p>b<!-- dropped comment --></p>", options=LEGACY)
    assert artifact.migration_report.sha256() != other.migration_report.sha256()


def test_the_report_is_stable_across_reads() -> None:
    artifact = api.to_md_artifact(LOSSY, options=LEGACY)
    assert artifact.migration_report.sha256() == artifact.migration_report.sha256()


def test_candidate_artifact_exposes_a_migration_report() -> None:
    base = api.to_md_artifact(LOSSY, options=LEGACY)
    candidate = api.to_cfx_artifact(base.markdown, base_artifact=base, splice_source=LOSSY, options=LEGACY)
    assert isinstance(candidate.migration_report, MigrationReport)


# ---------------------------------------------------------------------------
# push_safe and the report must agree
# ---------------------------------------------------------------------------


def test_a_fatal_report_is_never_push_safe() -> None:
    """The invariant that makes the report load-bearing rather than decorative.

    push_safe is what a caller acts on. If it could be True while the report says
    fatal, the report would be an advisory note next to an unsafe green light.
    """
    for storage in (LOSSY, MACRO_PARAMETER_ONLY, MACRO_PLAIN_TEXT_BODY):
        for options in (LEGACY, EDITABLE):
            artifact = api.to_md_artifact(storage, options=options)
            if artifact.migration_report.fatal:
                assert not artifact.push_safe, f"fatal report but push_safe: {storage!r} {options.profile}"


def test_push_safe_implies_no_fatal_occurrence_in_the_candidate() -> None:
    base = api.to_md_artifact(LOSSY, options=LEGACY)
    candidate = api.to_cfx_artifact(base.markdown, base_artifact=base, splice_source=LOSSY, options=LEGACY)
    if candidate.push_safe:
        assert not candidate.migration_report.fatal


# ---------------------------------------------------------------------------
# Unknown macros
# ---------------------------------------------------------------------------
#
# These were reported as silent loss in the editable profile: zero diagnostics,
# push_safe True, and a bare "[Unsupported Confluence content]" placeholder where
# the macro had been. The first half is accurate; the conclusion was not.
#
# The macro is not lost. It becomes a protected region, its original storage is
# retained, and a push splices those exact bytes back -- verified below in both
# shapes. An empty diagnostic list is therefore the honest answer to "what was
# lost": nothing was.
#
# What is missing is disclosure, not preservation. The Markdown gives the reader
# no way to know that the placeholder stands for retained content, or that
# editing it will fail. That is Story 2's marker and notice, and the editing case
# is pinned here so the protection is not weakened while that lands.


@pytest.mark.parametrize(
    ("label", "storage"),
    [("parameter-only", MACRO_PARAMETER_ONLY), ("plain-text-body", MACRO_PLAIN_TEXT_BODY)],
)
def test_an_unknown_macro_survives_the_round_trip_in_the_editable_profile(label: str, storage: str) -> None:
    artifact = api.to_md_artifact(storage, options=EDITABLE)
    candidate = api.to_cfx_artifact(artifact.markdown, base_artifact=artifact, splice_source=storage, options=EDITABLE)
    assert "totally-unknown" in candidate.xhtml, f"{label}: the macro was dropped"
    assert candidate.push_safe


def test_the_macro_payload_itself_is_preserved_not_just_the_element() -> None:
    """A retained wrapper with a lost body would still be silent loss."""
    artifact = api.to_md_artifact(MACRO_PLAIN_TEXT_BODY, options=EDITABLE)
    candidate = api.to_cfx_artifact(
        artifact.markdown, base_artifact=artifact, splice_source=MACRO_PLAIN_TEXT_BODY, options=EDITABLE
    )
    assert "body payload" in candidate.xhtml
    parameter = api.to_md_artifact(MACRO_PARAMETER_ONLY, options=EDITABLE)
    parameter_candidate = api.to_cfx_artifact(
        parameter.markdown, base_artifact=parameter, splice_source=MACRO_PARAMETER_ONLY, options=EDITABLE
    )
    assert "value" in parameter_candidate.xhtml


@pytest.mark.parametrize(
    ("label", "storage"),
    [("parameter-only", MACRO_PARAMETER_ONLY), ("plain-text-body", MACRO_PLAIN_TEXT_BODY)],
)
def test_the_macro_is_covered_by_a_protected_region(label: str, storage: str) -> None:
    """Preservation is not incidental: it is the protected region doing its job."""
    artifact = api.to_md_artifact(storage, options=EDITABLE)
    assert artifact.protected_regions, f"{label}: nothing marks the macro as protected"


@pytest.mark.parametrize(
    ("label", "storage"),
    [("parameter-only", MACRO_PARAMETER_ONLY), ("plain-text-body", MACRO_PLAIN_TEXT_BODY)],
)
def test_editing_the_placeholder_is_refused_rather_than_silently_dropped(label: str, storage: str) -> None:
    """The failure mode that makes the missing disclosure matter.

    The placeholder reads like something to fill in. Doing so must not quietly
    delete the macro it stands for.
    """
    artifact = api.to_md_artifact(storage, options=EDITABLE)
    edited = artifact.markdown.replace("[Unsupported Confluence content]", "I typed over it")
    assert edited != artifact.markdown, f"{label}: the placeholder text changed shape"
    candidate = api.to_cfx_artifact(edited, base_artifact=artifact, splice_source=storage, options=EDITABLE)
    assert not candidate.push_safe
    assert candidate.migration_report.fatal
    assert "protected-region-edited" in {occurrence.code for occurrence in candidate.migration_report.occurrences}
