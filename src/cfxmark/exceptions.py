"""Exception hierarchy for cfxmark."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cfxmark.change_ownership import LeafIdentity


class CfxmarkError(Exception):
    """Base class for all cfxmark exceptions."""


class ParseError(CfxmarkError):
    """Raised when input cannot be parsed.

    * Markdown parsing errors (CommonMark violations)
    * XHTML parsing errors (malformed XML, namespace issues)
    """


# Closed, public allowlist of value-free conversion reason codes that may cross
# the cfxmark ↔ consumer boundary on ``ConversionError.reason_code``. Each entry is
# a stable machine identifier only — never a message, leaf value, or source text —
# so a consumer can safely serialize it into a JSON envelope. Scope-minimized: only
# ``semantic-mapping-ambiguous`` is published this round.
PUBLIC_CONVERSION_REASON_CODES: frozenset[str] = frozenset({"semantic-mapping-ambiguous"})


class ConversionError(CfxmarkError):
    """Raised when a well-formed input cannot be converted.

    Typically means an AST was produced but it references something we
    cannot emit — for example, an inline unknown Confluence element that
    we cannot escalate to a block-level opaque fence.

    ``reason_code`` is an optional, value-free stable machine code drawn from
    :data:`PUBLIC_CONVERSION_REASON_CODES`. When a coded cfxmark failure is
    wrapped into a :class:`ConversionError`, the wrap site sets ``reason_code``
    to the wrapped error's code *only if* it is in the published allowlist, so
    the specific cause survives across the public error boundary without exposing
    any message string, leaf value, or source text. It is ``None`` when the cause
    carries no published code.
    """

    def __init__(self, *args: object, reason_code: str | None = None) -> None:
        super().__init__(*args)
        self.reason_code = reason_code


class MacroError(CfxmarkError):
    """Raised when a macro handler fails.

    * Missing required parameter
    * Forward or reverse function raised an unexpected error
    """


class AssetSecurityError(CfxmarkError):
    """Raised when ``resolve_assets`` rejects an unsafe attachment filename."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"unsafe attachment filename {name!r}: {reason}")


@dataclass(frozen=True)
class DiagnosticSummary:
    """Value-free projection of one conversion diagnostic: code and resolution only.

    Neither field carries a leaf value, fingerprint, or raw source; both are
    stable identifiers safe to serialize across a public error boundary.
    """

    code: str
    resolution: str
    # Zero-based table position for a table-presentation failure, else ``None``.
    # "which table" is the one thing a caller needs and could not previously get:
    # these failures are not leaf-based, so ``top_identities`` is empty for them.
    table_ordinal: int | None = None


@dataclass(frozen=True)
class OwnershipProofSummary:
    """Value-free summary of why a managed ownership proof is fatal.

    Carries only the fatal classification, per-category counts, canonical leaf
    identities (``(path, field, attribute_name, ordinal)`` — never a value
    fingerprint), and diagnostic ``code``/``resolution`` identifiers, so a caller
    can render safe operator diagnostics without touching storage content.
    """

    fatal_class: str
    unclassified_count: int
    multiple_owners_count: int
    overlap_count: int
    top_identities: tuple[LeafIdentity, ...] = ()
    diagnostics: tuple[DiagnosticSummary, ...] = ()


class OwnershipProofError(ConversionError):
    """Raised at the managed publish-preflight fatal-proof boundary.

    Subtype of :class:`ConversionError`, so existing ``except ConversionError``
    handlers keep working. Carries an immutable, value-free
    :class:`OwnershipProofSummary` so a caller can build safe operator context
    without inspecting leaf fingerprints or raw storage.
    """

    def __init__(self, message: str, *, summary: OwnershipProofSummary) -> None:
        super().__init__(message)
        self.summary = summary
