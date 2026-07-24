"""W3 — storage round-trip supported-corpus manifest, dispatched by disposition.

The versioned manifest at ``tests/fixtures/storage_roundtrip_manifest.json`` is
the single source of truth for which synthetic storage fixtures round-trip
losslessly, which disclose a bounded loss, and which fail closed. Each fixture
declares exactly one disposition and this test asserts the matching contract, so
the supported boundary cannot drift silently.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import cfxmark

_MANIFEST_PATH = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "storage_roundtrip_manifest.json"
_EDITABLE = cfxmark.ConversionOptions(profile="editable")
_CODE_MACRO = 'ac:name="code"'


def _load_manifest() -> dict[str, object]:
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    return manifest


_FIXTURES = _load_manifest()["fixtures"]
_IDS = [fixture["id"] for fixture in _FIXTURES]


def _round_trip(storage: str) -> str:
    markdown = cfxmark.to_md(storage, options=_EDITABLE).markdown
    return cfxmark.to_cfx(markdown, options=_EDITABLE).xhtml


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_manifest_fixture_matches_its_disposition(fixture: dict[str, object]) -> None:
    storage = str(fixture["storage"])
    disposition = fixture["disposition"]
    round_tripped = _round_trip(storage)
    artifact = cfxmark.to_md_artifact(storage, options=_EDITABLE)
    report = artifact.migration_report
    equal = cfxmark.storage_semantically_equal(storage, round_tripped)
    # A code macro may appear in the round trip only if the source already had
    # one -- the renderer must never CREATE a code macro from an over-indented list.
    code_macro_created = _CODE_MACRO in round_tripped and _CODE_MACRO not in storage

    if disposition == "lossless":
        assert equal, f"{fixture['id']}: expected a lossless semantic round trip"
        assert not code_macro_created, f"{fixture['id']}: renderer created a spurious code macro"
        assert artifact.push_safe is True, f"{fixture['id']}: a lossless fixture must be push-safe"
        assert not report.consent_required, f"{fixture['id']}: a lossless fixture must not require consent"
    elif disposition == "disclosed_loss":
        loss_code = str(fixture["loss_code"])
        # Never silent: the loss makes the page non-push-safe and there IS a real
        # storage difference to disclose.
        assert artifact.push_safe is False, f"{fixture['id']}: a disclosed loss must not be push-safe"
        assert not equal, f"{fixture['id']}: a disclosed loss must have a real storage difference"
        resolution_ids = {resolution.id for diagnostic in artifact.diagnostics for resolution in diagnostic.resolutions}
        assert loss_code in resolution_ids, f"{fixture['id']}: disclosed loss code {loss_code!r} is not surfaced"
        # The loss is disclosed as an explicit occurrence, not merely a generic failure.
        disclosed = any(
            loss_code in {resolution.id for resolution in diagnostic.resolutions} and diagnostic.blocking
            for diagnostic in artifact.diagnostics
        )
        assert disclosed, f"{fixture['id']}: disclosed loss is not a blocking, named occurrence"
    elif disposition == "unsupported_fail_closed":
        # An unrepresentable shape must be REFUSED, not silently normalised: the
        # page is not push-safe, the ownership proof is fatal, and the specific
        # shape diagnostic names it (it is not merely a generic failure).
        assert not equal, f"{fixture['id']}: an unsupported fixture must have a real storage difference"
        assert artifact.push_safe is False, f"{fixture['id']}: an unsupported fixture must fail closed"
        candidate = cfxmark.to_cfx_artifact(
            artifact.markdown,
            base_artifact=artifact,
            splice_source=storage,
            options=_EDITABLE,
        )
        proof = candidate.ownership_proof
        assert proof is not None and proof.fatal, f"{fixture['id']}: an unsupported fixture must have a fatal proof"
        diagnostic_code = str(fixture["diagnostic_code"])
        emitted_codes = {diagnostic.code for diagnostic in artifact.diagnostics}
        assert diagnostic_code in emitted_codes, (
            f"{fixture['id']}: expected the {diagnostic_code!r} diagnostic to name the unsupported shape"
        )
    else:  # pragma: no cover - guards against a mistyped disposition
        pytest.fail(f"{fixture['id']}: unknown disposition {disposition!r}")


def test_manifest_covers_the_track_a_and_track_b_witnesses() -> None:
    """The corpus must keep the control, both adversarial guards, and the Track-B loss."""
    by_id = {fixture["id"]: fixture for fixture in _FIXTURES}
    # Track A: control + three repaired depth fixtures are all lossless.
    for fixture_id in (
        "nol_d1_start1",
        "nol_d2_firstchild_start1",
        "nol_d2_pure_firstchild_start1",
        "nol_d3_firstchild_start1",
    ):
        assert by_id[fixture_id]["disposition"] == "lossless"
    # Adversarial guards stay lossless (the de-indent branch must not fire for them).
    assert by_id["list_first_child_text"]["disposition"] == "lossless"
    assert by_id["list_first_child_codeblock"]["disposition"] == "lossless"
    # Track B: the paragraph-shape non-1 nested list is an unrepresentable shape
    # that fails closed -- it is NOT a consentable disclosed loss.
    assert by_id["nol_start2_paragraph_shape"]["disposition"] == "unsupported_fail_closed"
    assert by_id["nol_start2_paragraph_shape"]["diagnostic_code"] == "nested-ordered-list-start-unrepresentable"
