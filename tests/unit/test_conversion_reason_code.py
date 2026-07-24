"""Public contract for ``ConversionError.reason_code`` and the reason-code allowlist.

The reason code is a value-free, closed-allowlist machine identifier: a consumer
can distinguish a semantic-mapping-ambiguous failure from any other conversion
error via a stable code, without parsing a message string, leaf value, or source
text. A code outside :data:`cfxmark.PUBLIC_CONVERSION_REASON_CODES` is never
published; it yields ``reason_code is None``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import cfxmark
from cfxmark.source_map import SemanticMappingAmbiguous, SourceMapError

EDITABLE = cfxmark.ConversionOptions(profile="editable")

# A base with duplicate sibling paragraphs; deleting one is a minimum-cost tie the
# semantic matcher refuses to resolve by first match, so managed validation of the
# candidate re-derivation fails closed with semantic-mapping-ambiguous.
_TIE_STORAGE = "<p>A</p><p>X</p><p>X</p><p>B</p>"
_TIE_EDITED = "A\n\nX\n\nB\n"


def _managed_tie_candidate() -> cfxmark.CfxArtifact:
    base = cfxmark.to_md_artifact(_TIE_STORAGE, options=EDITABLE)
    migration_base = replace(base, protected_regions=(), remote_subtrees=())
    return cfxmark.to_cfx_artifact(
        _TIE_EDITED,
        presentation=migration_base.presentation,
        base_artifact=migration_base,
        splice_source=_TIE_STORAGE,
        options=EDITABLE,
    )


def test_semantic_mapping_tie_surfaces_conversion_error_reason_code() -> None:
    candidate = _managed_tie_candidate()
    assert candidate.push_safe is False
    with pytest.raises(cfxmark.ConversionError) as info:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=_TIE_STORAGE, options=EDITABLE)
    # The specific cause survives as a bare, value-free machine code.
    assert info.value.reason_code == "semantic-mapping-ambiguous"


def test_conversion_error_reason_code_defaults_to_none() -> None:
    assert cfxmark.ConversionError("message only").reason_code is None


def test_reason_code_allowlist_is_closed_and_scoped() -> None:
    # Only the semantic tie is published this round. A different real coded cfxmark
    # error (SourceMapError, "source-map-invalid") is excluded, so applying the
    # allowlist rule to it yields reason_code is None — the raw code is never
    # passed through.
    assert SemanticMappingAmbiguous.code in cfxmark.PUBLIC_CONVERSION_REASON_CODES
    assert SourceMapError.code not in cfxmark.PUBLIC_CONVERSION_REASON_CODES
