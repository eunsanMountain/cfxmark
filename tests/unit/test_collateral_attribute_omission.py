"""Round-trip attribute/structure omissions are named, witnessed, and owned.

Regression class: a one-word edit of one plain paragraph failed closed with
document-sized unclassified-storage-change counts. The minimal triggers were
remote-only decoration and metadata that Markdown cannot carry:

  * ``<li data-uuid=...>`` / ``<ul style=...>`` / ``<h2 style=...>``
  * ``<table class=...>`` with a ``<colgroup>`` (column widths)
  * body-only header rows (synthesized ``<thead>``) and cell ``<p>`` wrappers

Each drop is now an exact occurrence with an atomic storage witness, so the
ownership proof attributes the change and downstream publish still requires
explicit consent (effect ``removed``) or reports the normalization. Genuinely
ambiguous composites (two migrations producing identical leaf moves) must keep
failing closed.
"""

from __future__ import annotations

from dataclasses import replace

import cfxmark
from cfxmark.api import _resolve_atomic_storage_migration
from cfxmark.artifacts import collateral_diagnostics

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def _proof_for_unedited_full_migration(storage: str) -> cfxmark.CfxArtifact:
    base = replace(cfxmark.to_md_artifact(storage, options=EDITABLE), protected_regions=(), remote_subtrees=())
    return cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )


def _report_codes(candidate: cfxmark.CfxArtifact) -> dict[str, str]:
    report = candidate.source_migration_report
    assert report is not None
    return {occurrence.code: occurrence.effect for occurrence in report.occurrences}


def test_list_item_data_uuid_drop_is_owned() -> None:
    storage = (
        '<ul><li data-uuid="00000000-0000-0000-0000-000000000001">'
        "<p>alpha bravo charlie delta echo</p></li></ul><p>closing paragraph text</p>"
    )
    candidate = _proof_for_unedited_full_migration(storage)
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is False and not proof.unclassified
    assert _report_codes(candidate)["element-attribute-omitted"] == "removed"


def test_list_and_heading_style_drops_are_owned() -> None:
    for storage in (
        '<ul style="list-style-type: square;"><li><p>alpha bravo charlie delta</p></li></ul>'
        "<p>closing paragraph text</p>",
        '<h2 style="text-align: center;">heading text</h2><p>closing paragraph text</p>',
    ):
        candidate = _proof_for_unedited_full_migration(storage)
        proof = candidate.ownership_proof
        assert proof is not None and proof.fatal is False and not proof.unclassified
        assert _report_codes(candidate)["element-attribute-omitted"] == "removed"


def test_attribute_witness_removes_exactly_one_attribute() -> None:
    storage = '<p data-a="1" data-b="2">Visible text here</p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(base.markdown, options=EDITABLE).xhtml
    diagnostics = collateral_diagnostics(storage, candidate)
    omitted = [d for d in diagnostics if d.code == "element-attribute-omitted"]
    assert {d.location.semantic_path[-1] for d in omitted} == {"@data-a[0]", "@data-b[0]"}
    resolved = _resolve_atomic_storage_migration(storage, diagnostics, omitted[0])
    assert resolved is not None
    kept = omitted[1].location.semantic_path[-1]
    assert kept[1:-3] in resolved and omitted[0].location.semantic_path[-1][1:-3] not in resolved


def test_colgroup_and_cell_wrapper_and_thead_are_named() -> None:
    storage = (
        '<table class="wrapped"><colgroup><col style="width: 100px;"/><col/></colgroup>'
        "<tbody><tr><th><p>one</p></th><th><p>two</p></th></tr>"
        "<tr><td><p>alpha</p></td><td><p>bravo</p></td></tr></tbody></table>"
        "<p>closing paragraph text</p>"
    )
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(base.markdown, options=EDITABLE).xhtml
    codes = {d.code for d in collateral_diagnostics(storage, candidate)}
    assert {
        "table-colgroup-omitted",
        "table-cell-paragraph-unwrapped",
        "body-only-table-header-synthesized",
        "element-attribute-omitted",
    } <= codes


def test_composite_table_reshape_is_owned_by_its_contributors() -> None:
    """Colgroup drop + thead synthesis + cell unwraps compose at shared leaves;
    the ordered composition witness reproduces the full after-state, so every
    leaf is attributed to a real contributing occurrence and each contributor
    lands on the consent surface (all effects here are consent-gated)."""
    storage = (
        '<table class="wrapped"><colgroup><col style="width: 100px;"/><col/></colgroup>'
        "<tbody><tr><th><p>one</p></th><th><p>two</p></th></tr>"
        "<tr><td><p>alpha</p></td><td><p>bravo</p></td></tr></tbody></table>"
        "<p>closing paragraph text</p>"
    )
    candidate = _proof_for_unedited_full_migration(storage)
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is False
    assert not proof.unclassified and not proof.multiple_owners
    codes = _report_codes(candidate)
    assert {
        "table-colgroup-omitted",
        "table-cell-paragraph-unwrapped",
        "body-only-table-header-synthesized",
        "element-attribute-omitted",
    } <= set(codes)
    # every migration owner is a real report occurrence
    report_ids = {o.occurrence_id for o in candidate.source_migration_report.occurrences}
    assert set(proof.migration_occurrence_ids) <= report_ids


def test_unknown_element_drop_stays_fatal() -> None:
    """Only *attributes* of corresponding same-tag elements are named; an
    unknown dropped element is still an unclassified fatal change."""
    storage = '<p><foo data-secret="x">Visible</foo> words after it</p>'
    candidate = _proof_for_unedited_full_migration(storage)
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is True


def test_color_style_value_normalization_is_owned() -> None:
    """A color style the round-trip re-renders canonically (palette-token
    coarsening, spacing, semicolon) is a named normalized occurrence whose
    witness applies the same shared normalizer — no target value needed."""
    storage = (
        '<p>text <span style="color:var(--ds-text-subtlest,#626F86);">colored words here</span>'
        " tail</p><p>closing paragraph text</p>"
    )
    candidate = _proof_for_unedited_full_migration(storage)
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is False and not proof.unclassified
    assert _report_codes(candidate)["element-attribute-normalized"] == "normalized"


def test_dropped_color_style_on_block_element_is_owned_as_an_omission() -> None:
    """A *dropped* attribute is an omission even when its value is normalizable.

    Regression: classifying the drop as ``element-attribute-normalized`` left the
    leaf unowned, because that witness can only *set* the canonical value on the
    node and can never reproduce a removal. Colored headings/paragraphs/lists —
    already covered by ``element-attribute-omitted`` — failed closed again with a
    spurious ``unclassified-storage-change``.
    """
    for storage in (
        '<h2 style="color: red;">Heading Text</h2><p>closing paragraph text</p>',
        '<p style="color:red">colored paragraph text here</p><p>second paragraph text</p>',
        '<ul style="color:red"><li><p>alpha item text</p></li></ul><p>closing paragraph text</p>',
    ):
        candidate = _proof_for_unedited_full_migration(storage)
        proof = candidate.ownership_proof
        assert proof is not None and proof.fatal is False and not proof.unclassified, storage
        assert _report_codes(candidate)["element-attribute-omitted"] == "removed", storage


def test_relocated_colored_cell_keeps_its_normalization_guess() -> None:
    """An attribute on an element the round-trip *moves* is unobservable at its
    old path, so the shape-based normalization guess must stand there.

    Regression: collapsing every "no candidate value" into an omission also
    caught the move case. The omission witness *removes* the attribute, which
    cannot reproduce a moved-and-re-rendered leaf, so the composite lost an
    owner and the whole table reshape failed closed, regressing a page from
    consent to fatal. Only an observed drop (the element corresponds, the
    attribute is gone) is an omission.
    """
    storage = (
        '<table class="wrapped"><colgroup><col style="width: 100px;"/><col/></colgroup>'
        '<tbody><tr><th style="color: red;"><p>one</p></th><th><p>two</p></th></tr>'
        "<tr><td><p>alpha</p></td><td><p>bravo</p></td></tr></tbody></table>"
        "<p>closing paragraph text</p>"
    )
    candidate = _proof_for_unedited_full_migration(storage)
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is False
    assert not proof.unclassified and not proof.multiple_owners


def test_unknown_attribute_value_change_stays_fail_closed() -> None:
    from cfxmark.artifacts import normalized_attribute_value

    assert (
        normalized_attribute_value("style", "color:var(--ds-text-subtlest,#626F86);")
        == "color:var(--ds-text-subtle, #626f86)"
    )
    # no known canonical form -> None -> the proof cannot name it and fails closed
    assert normalized_attribute_value("style", "font-family: Wingdings;") is None
    assert normalized_attribute_value("data-x", "anything") is None


def test_multi_paragraph_cell_collapse_is_owned() -> None:
    storage = (
        "<table><tbody><tr><th><p>h</p></th></tr>"
        "<tr><td><p>para one text</p><p>para two text</p></td></tr></tbody></table>"
        "<p>closing paragraph text</p>"
    )
    candidate = _proof_for_unedited_full_migration(storage)
    proof = candidate.ownership_proof
    assert proof is not None and proof.fatal is False and not proof.unclassified
    assert _report_codes(candidate)["table-cell-paragraphs-collapsed"] == "removed"
