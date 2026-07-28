"""Typed, deterministic conversion diagnostics.

Human-readable warning strings remain for compatibility, but write safety is
driven by this typed model so callers never infer severity from wording.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

DiagnosticCategory = Literal[
    "informational",
    "readable_simplification",
    "canonicalization",
    "presentation_loss",
    "content_loss",
    "structural_loss",
    "manual_replacement",
]
DiagnosticSeverity = Literal["informational", "decision_required", "manual_replacement", "unsupported", "blocking"]


@dataclass(frozen=True)
class DiagnosticLocation:
    """Content-free stable location suitable for diagnostics and state."""

    semantic_path: tuple[str, ...]
    heading_path: tuple[str, ...] = field(default_factory=tuple)
    table_row: int | None = None
    table_column: int | None = None
    # Which table, in document order. An integer position cannot carry content, so
    # it stays safe to surface across a public error boundary — unlike the table id,
    # which is derived from cell text.
    table_index: int | None = None


@dataclass(frozen=True)
class DiagnosticResolution:
    id: str
    label: str
    effect: str


@dataclass(frozen=True)
class ConversionDiagnostic:
    code: str
    category: DiagnosticCategory
    severity: DiagnosticSeverity
    occurrence_id: str
    location: DiagnosticLocation
    resolutions: tuple[DiagnosticResolution, ...]
    message: str

    @property
    def blocking(self) -> bool:
        return self.severity in {"blocking", "decision_required", "manual_replacement", "unsupported"}


_MANUAL_REPLACEMENT = DiagnosticResolution(
    id="manual-replacement",
    label="Replace with supported Markdown",
    effect="The unsupported source construct must be replaced explicitly before publishing",
)
_KEEP_REMOTE = DiagnosticResolution(
    id="keep-remote",
    label="Keep the remote representation",
    effect="Use storage-aware patching or leave this occurrence unchanged",
)
_UNICODE = DiagnosticResolution(
    id="unicode",
    label="Convert to Unicode text",
    effect="The Confluence macro identity is removed",
)


def make_diagnostic(
    *,
    code: str,
    category: DiagnosticCategory,
    severity: DiagnosticSeverity,
    message: str,
    source_fingerprint: str,
    ordinal: int = 0,
    location: DiagnosticLocation | None = None,
    resolutions: tuple[DiagnosticResolution, ...] | None = None,
) -> ConversionDiagnostic:
    resolved_location = location or DiagnosticLocation(semantic_path=(f"source[{ordinal}]",))
    location_key = "\x1f".join(resolved_location.semantic_path)
    if resolved_location.heading_path:
        location_key += "\x1dheading:" + "\x1f".join(resolved_location.heading_path)
    if resolved_location.table_row is not None or resolved_location.table_column is not None:
        location_key += f"\x1dcell:{resolved_location.table_row}:{resolved_location.table_column}"
    identity = hashlib.sha256(
        f"cfxmark-occ-v2\0{code}\0{ordinal}\0{location_key}\0{source_fingerprint}".encode()
    ).hexdigest()[:20]
    if resolutions is None:
        if code == "emoticon-to-unicode":
            resolutions = (_UNICODE, _MANUAL_REPLACEMENT)
        elif category in {"content_loss", "structural_loss", "presentation_loss", "manual_replacement"}:
            resolutions = (_KEEP_REMOTE, _MANUAL_REPLACEMENT)
        else:
            resolutions = (_KEEP_REMOTE,)
    return ConversionDiagnostic(
        code=code,
        category=category,
        severity=severity,
        occurrence_id=f"occ-{identity}",
        location=resolved_location,
        resolutions=resolutions,
        message=message,
    )


_EXACT_MESSAGE_CODES: dict[str, tuple[str, DiagnosticCategory, DiagnosticSeverity]] = {
    "XML comments and processing instructions are not represented in Markdown": (
        "xml-comment-dropped",
        "content_loss",
        "blocking",
    ),
    # The push-side sibling of xml-comment-dropped: a user-authored HTML comment
    # dropped when Markdown is converted to Confluence. Mirrors xml-comment-dropped
    # exactly — same content_loss category, same blocking severity, same "removed"
    # effect (see migration_report._PRODUCER_EFFECTS) — so it is a classified,
    # consentable loss, not the conversion-note fallback. blocking is required, not
    # cosmetic: the managed push contract (atls _validate_source_diagnostics) admits
    # a source loss only when it is content_loss + blocking, and consent is driven
    # by the "removed" effect, so the loss flips push_safe to False and is then
    # resolved through informed consent — the same path as every other source loss.
    "HTML comment dropped": (
        "html-comment-dropped",
        "content_loss",
        "blocking",
    ),
    "Confluence layout structure is flattened in Markdown": ("layout-flattened", "structural_loss", "blocking"),
    "body-only tables require a synthetic Markdown header row": (
        "body-only-table-header-synthesized",
        "structural_loss",
        "blocking",
    ),
    "table column contains mixed cell alignments that Markdown cannot preserve": (
        "table-column-alignment-mixed",
        "structural_loss",
        "blocking",
    ),
    "table cell span exceeds the safe editable limit": (
        "table-cell-span-limit-exceeded",
        "structural_loss",
        "blocking",
    ),
    "table cell spans overlap in the logical grid": (
        "table-cell-span-overlap",
        "structural_loss",
        "blocking",
    ),
    "Confluence emoticon macros are rendered as Unicode text": (
        "emoticon-to-unicode",
        "readable_simplification",
        "decision_required",
    ),
    "table cell background is not represented by the string-only editable API": (
        "table-cell-background-untracked",
        "presentation_loss",
        "blocking",
    ),
    "table cell background has no verified Confluence renderer": (
        "table-cell-background-unrenderable",
        "presentation_loss",
        "blocking",
    ),
    "table cell background value is invalid": ("table-cell-background-invalid", "presentation_loss", "blocking"),
    "table cell background sources conflict": ("table-cell-background-conflict", "presentation_loss", "blocking"),
    "semantic mapping is ambiguous": ("semantic-mapping-ambiguous", "structural_loss", "blocking"),
    "unsupported Confluence content has no editable Markdown representation": (
        "unsupported-confluence-content",
        "manual_replacement",
        "unsupported",
    ),
}


def diagnostics_from_messages(
    *,
    warnings: tuple[str, ...] | list[str],
    losses: tuple[str, ...] | list[str],
    source: str,
    loss_locations: tuple[DiagnosticLocation, ...] | list[DiagnosticLocation] = (),
) -> tuple[ConversionDiagnostic, ...]:
    """Translate known producer messages through an explicit stable registry."""

    if loss_locations and len(loss_locations) != len(losses):
        raise ValueError("loss_locations must align one-to-one with losses")
    fingerprint = hashlib.sha256(source.encode()).hexdigest()
    diagnostics: list[ConversionDiagnostic] = []
    # Diagnostics are occurrence-scoped authorization records.  Repeated
    # producer messages therefore must remain repeated: deduplicating here
    # would let one approval cover multiple source constructs.
    for ordinal, message in enumerate((*losses, *warnings)):
        location = loss_locations[ordinal] if ordinal < len(loss_locations) else None
        spec = _EXACT_MESSAGE_CODES.get(message)
        if (
            spec is None
            and message.startswith("inline HTML tag <")
            and (
                message.endswith(" dropped from Markdown") or message.endswith(" dropped — no equivalent in Confluence")
            )
        ):
            spec = ("html-tag-stripped", "content_loss", "blocking")
        elif spec is None and message == "inline <span> with unknown attributes stripped":
            spec = ("html-tag-stripped", "structural_loss", "blocking")
        if spec is None:
            diagnostics.append(
                make_diagnostic(
                    code="conversion-note",
                    category="informational",
                    severity="informational",
                    message=message,
                    source_fingerprint=fingerprint,
                    ordinal=ordinal,
                    location=location,
                )
            )
            continue
        code, category, severity = spec
        diagnostics.append(
            make_diagnostic(
                code=code,
                category=category,
                severity=severity,
                message=message,
                source_fingerprint=fingerprint,
                ordinal=ordinal,
                location=location,
            )
        )
    return tuple(diagnostics)


__all__ = [
    "ConversionDiagnostic",
    "DiagnosticCategory",
    "DiagnosticLocation",
    "DiagnosticResolution",
    "DiagnosticSeverity",
    "diagnostics_from_messages",
    "make_diagnostic",
]
