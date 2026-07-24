"""W2 — bounded structured-macro server-envelope allowlist.

Two public comparators (`storage_readback_comparison`, `storage_noop_comparison`)
back a single private classifier. The allowlist is asymmetric and value-free.
"""

from __future__ import annotations

import dataclasses

import pytest

import cfxmark
from cfxmark.ownership_proof import StorageComparison, StorageComparisonSummary

_BODY = "<ac:plain-text-body><![CDATA[x]]></ac:plain-text-body>"
_NO_ENVELOPE = f'<ac:structured-macro ac:name="code">{_BODY}</ac:structured-macro>'
_SCHEMA_1_ONLY = f'<ac:structured-macro ac:name="code" ac:schema-version="1">{_BODY}</ac:structured-macro>'
_STAMPED_1 = (
    f'<ac:structured-macro ac:name="code" ac:schema-version="1" ac:macro-id="mid-1">{_BODY}</ac:structured-macro>'
)
_STAMPED_2 = (
    f'<ac:structured-macro ac:name="code" ac:schema-version="2" ac:macro-id="mid-1">{_BODY}</ac:structured-macro>'
)
_NAME_CHANGED = (
    f'<ac:structured-macro ac:name="info" ac:schema-version="1" ac:macro-id="mid-1">{_BODY}</ac:structured-macro>'
)
_BODY_CHANGED = (
    '<ac:structured-macro ac:name="code" ac:schema-version="1" ac:macro-id="mid-1">'
    "<ac:plain-text-body><![CDATA[y]]></ac:plain-text-body></ac:structured-macro>"
)


def test_server_added_schema_1_and_macro_id_are_equivalent_for_both_comparators() -> None:
    for observed in (_STAMPED_1, _SCHEMA_1_ONLY):
        readback = cfxmark.storage_readback_comparison(_NO_ENVELOPE, observed)
        noop = cfxmark.storage_noop_comparison(_NO_ENVELOPE, observed)
        assert readback.equivalent is True
        assert noop.equivalent is True
        assert readback.public_summary is None
        assert noop.public_summary is None
    # Compat boolean agrees; the strict proof comparator never applies the allowlist.
    assert cfxmark.storage_readback_equivalent(_NO_ENVELOPE, _STAMPED_1) is True
    assert cfxmark.storage_semantically_equal(_NO_ENVELOPE, _STAMPED_1) is False


def test_server_added_schema_2_rejected_with_value_free_summary() -> None:
    readback = cfxmark.storage_readback_comparison(_NO_ENVELOPE, _STAMPED_2)
    assert readback.equivalent is False
    summary = readback.public_summary
    assert isinstance(summary, StorageComparisonSummary)
    assert summary.code == "server_envelope_allowlist_miss"
    # Canonical attribute identity, never the observed schema value.
    assert summary.identity[1] == "attribute"
    assert summary.identity[2] == "ac:schema-version"
    assert summary.policy == "server-envelope-allowlist:v1:readback"
    assert summary.resolution == "keep-remote"
    serialized = repr(dataclasses.asdict(summary))
    assert '"2"' not in serialized and "'2'" not in serialized
    assert "sha256:" not in serialized

    noop = cfxmark.storage_noop_comparison(_NO_ENVELOPE, _STAMPED_2)
    assert noop.equivalent is False
    assert noop.public_summary is not None
    assert noop.public_summary.policy == "server-envelope-allowlist:v1:noop"


def test_candidate_carried_id_or_schema_change_is_rejected() -> None:
    # Candidate already carries schema="1"; server changes it to "2" -> value change.
    assert cfxmark.storage_readback_comparison(_SCHEMA_1_ONLY, _STAMPED_2).equivalent is False
    # Candidate carries a macro-id; server changes it -> instance rebind.
    other_id = (
        f'<ac:structured-macro ac:name="code" ac:schema-version="1" ac:macro-id="mid-2">{_BODY}</ac:structured-macro>'
    )
    assert cfxmark.storage_readback_comparison(_STAMPED_1, other_id).equivalent is False
    # Candidate carries a macro-id; server removes it -> instance lost.
    assert cfxmark.storage_readback_comparison(_STAMPED_1, _SCHEMA_1_ONLY).equivalent is False
    # Candidate carries ac:schema-version="1"; server drops it entirely -> rejected.
    # Symmetric with the macro-id-removed case: only a server-ADDED envelope
    # attribute is tolerated, never the removal of one the candidate carried.
    schema_removed = cfxmark.storage_readback_comparison(_SCHEMA_1_ONLY, _NO_ENVELOPE)
    assert schema_removed.equivalent is False
    assert schema_removed.public_summary is not None
    assert schema_removed.public_summary.identity[2] == "ac:schema-version"
    assert cfxmark.storage_noop_comparison(_SCHEMA_1_ONLY, _NO_ENVELOPE).equivalent is False


def test_non_envelope_differences_still_fail() -> None:
    # Macro name, body, and list start are semantic content, never transport metadata.
    assert cfxmark.storage_readback_comparison(_NO_ENVELOPE, _NAME_CHANGED).equivalent is False
    assert cfxmark.storage_readback_comparison(_STAMPED_1, _BODY_CHANGED).equivalent is False
    start_miss = cfxmark.storage_readback_comparison('<ol start="2"><li>a</li></ol>', "<ol><li>a</li></ol>")
    assert start_miss.equivalent is False
    assert start_miss.public_summary is not None
    assert start_miss.public_summary.code == "storage_leaf_mismatch"
    assert (
        cfxmark.storage_noop_comparison('<ol start="2"><li>a</li></ol>', '<ol start="3"><li>a</li></ol>').equivalent
        is False
    )


def test_public_surface_and_immutability() -> None:
    assert "StorageComparison" in cfxmark.__all__
    assert "storage_readback_comparison" in cfxmark.__all__
    assert "storage_noop_comparison" in cfxmark.__all__
    # The summary type and the private classifier stay off the public surface.
    assert "StorageComparisonSummary" not in cfxmark.__all__
    assert not hasattr(cfxmark, "compare_server_envelope")

    result = cfxmark.storage_readback_comparison(_NO_ENVELOPE, _STAMPED_1)
    assert isinstance(result, StorageComparison)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.equivalent = False  # type: ignore[misc]
