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


def test_a_tie_is_refused_by_the_proof_not_by_being_unreconstructable() -> None:
    """The validator repeats the generator's fallback instead of raising at it.

    The generator does not stop on a tie: it retries the mapping without the base
    and returns that candidate, carrying a blocking `semantic-mapping-ambiguous`
    diagnostic. This validator used to raise `ConversionError` at the same point,
    so it could not reconstruct the artifact the generator had just produced --
    a tie anywhere in a document turned a candidate whose proof is fatal into no
    candidate at all. Measured on a live page: `to_cfx_artifact` returned a
    16832-byte artifact and this call raised on the same input.

    Nothing about publishing changes. The refusal is still a refusal; it is now
    the *right* refusal, and the caller gets a candidate it can inspect.
    """

    candidate = _managed_tie_candidate()
    assert candidate.push_safe is False
    with pytest.raises(cfxmark.OwnershipProofError) as info:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=_TIE_STORAGE, options=EDITABLE)
    assert info.value.summary.fatal_class


def test_the_tie_still_names_itself_in_a_value_free_machine_code() -> None:
    """What the old contract protected, kept. The code used to travel as
    `ConversionError.reason_code`; it now travels in the ownership summary,
    alongside its resolution and the rest of the diagnosis -- which is strictly
    more than the bare code, and is what the publish preflight already projects.
    """

    candidate = _managed_tie_candidate()
    assert "semantic-mapping-ambiguous" in {item.code for item in candidate.diagnostics}
    with pytest.raises(cfxmark.OwnershipProofError) as info:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=_TIE_STORAGE, options=EDITABLE)
    assert "semantic-mapping-ambiguous" in {item.code for item in info.value.summary.diagnostics}


def test_a_tie_candidate_can_be_reconstructed_at_all() -> None:
    """The property the fix is really about: the validator reaches its verdict
    rather than failing to get there. Asserted as the absence of the old
    failure mode, because that is what regressed."""

    candidate = _managed_tie_candidate()
    try:
        cfxmark.validate_managed_cfx_artifact(candidate, source_storage=_TIE_STORAGE, options=EDITABLE)
    except cfxmark.OwnershipProofError:
        pass  # a verdict
    except cfxmark.ConversionError as error:  # pragma: no cover - the regression
        raise AssertionError(f"validator could not reconstruct the generator's candidate: {error}") from error


def test_conversion_error_reason_code_defaults_to_none() -> None:
    assert cfxmark.ConversionError("message only").reason_code is None


def test_reason_code_allowlist_is_closed_and_scoped() -> None:
    # Only the semantic tie is published this round. A different real coded cfxmark
    # error (SourceMapError, "source-map-invalid") is excluded, so applying the
    # allowlist rule to it yields reason_code is None — the raw code is never
    # passed through.
    assert SemanticMappingAmbiguous.code in cfxmark.PUBLIC_CONVERSION_REASON_CODES
    assert SourceMapError.code not in cfxmark.PUBLIC_CONVERSION_REASON_CODES
