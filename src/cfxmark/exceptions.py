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


#: How many leaf identities an error message shows before it stops being readable.
#: A display budget, not a redaction: every identity is already value-free.
MAX_SUMMARY_IDENTITIES = 10


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
    #: Every fatal leaf identity, uncapped — ``top_identities`` is its first page.
    #:
    #: Both are the same value-free projection and neither carries a fingerprint, so
    #: the cap was never a safety boundary; it is a display budget for an error
    #: message. A caller diagnosing *why* a corpus fails needs the whole set, and
    #: reading the capped one silently turns "these documents share a cause" into
    #: "these documents share a cause among the first ten" — which is how a real
    #: grouping was reported wrong. Consumers that render to a user should keep
    #: using ``top_identities``.
    all_identities: tuple[LeafIdentity, ...] = ()

    def __post_init__(self) -> None:
        # `all_identities` defaults to whatever `top_identities` holds, and that default
        # is the load-bearing half of this invariant rather than a convenience.
        #
        # The check below used to be guarded by `if self.all_identities`, which let the one
        # state this field exists to prevent through: `top_identities` populated and
        # `all_identities` empty. That is what *every* construction predating this field
        # produces, including anything rebuilding a summary from a serialized older payload
        # — so a consumer following the docstring's own advice and reading the full set
        # would get `()` while ten or more fatal identities sat beside it. Empty is
        # indistinguishable from "no leaves", which is the reporting failure the field was
        # added to end, arrived at from the other direction.
        #
        # Filling it instead of raising keeps older construction working and makes the
        # misleading state unrepresentable. What a caller supplied is then all this summary
        # claims to know.
        if not self.all_identities and self.top_identities:
            object.__setattr__(self, "all_identities", self.top_identities)
        # The cap is a view of the full list, not a separate answer. Without this
        # they can drift and a reader has no way to tell which one is the truth.
        if self.top_identities != self.all_identities[:MAX_SUMMARY_IDENTITIES]:
            raise ValueError("top_identities must be the first page of all_identities")


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
