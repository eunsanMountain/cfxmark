"""Explicit, value-safe manifests for intentional full-page replacement.

The normal source-bound ownership proof deliberately refuses a replacement it
cannot attribute.  This module does not weaken that proof.  It instead records
the macro identities which the *final* rendered candidate no longer carries so
an outer publisher can require an explicit, candidate-bound approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TypedDict

from cfxmark.artifacts import CfxArtifact, MacroIdentity, MarkdownArtifact, macro_identities
from cfxmark.exceptions import ConversionError
from cfxmark.parsers.cfx import parse_cfx_with_loss_locations

REPLACEMENT_MANIFEST_SCHEMA = "atls-replacement-manifest-v1"


class _PrivateDiscardedIdentity(TypedDict):
    """Hash-only detail, including the raw macro UUID that must not be returned."""

    kind: str
    semantic_path: list[str]
    ordinal: int
    macro_id: str
    schema_version: str | None


@dataclass(frozen=True)
class DiscardedIdentity:
    """The value-free public description of one discarded macro identity."""

    kind: str
    semantic_path: tuple[str, ...]
    ordinal: int
    count: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "semantic_path": list(self.semantic_path),
            "ordinal": self.ordinal,
            "count": self.count,
        }


@dataclass(frozen=True)
class ReplacementManifest:
    """A candidate-bound disclosure of identities an explicit replacement drops."""

    candidate_storage_sha256: str
    discarded_identities_sha256: str
    discarded_identities: tuple[DiscardedIdentity, ...]

    @property
    def schema(self) -> str:
        return REPLACEMENT_MANIFEST_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_storage_sha256": self.candidate_storage_sha256,
            "discarded_identities_sha256": self.discarded_identities_sha256,
            "discarded_identity_count": len(self.discarded_identities),
            "discarded_identities": [item.to_dict() for item in self.discarded_identities],
        }


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _identity_location(base: MarkdownArtifact, identity: MacroIdentity) -> tuple[str, ...]:
    for leaf in base.base_node_to_storage.get(identity.node_id, ()):
        if leaf.field == "attribute" and leaf.attribute_name == "ac:macro-id":
            return leaf.semantic_path
    raise ConversionError("replacement manifest cannot locate a source macro identity")


def _candidate_macro_ids(candidate: CfxArtifact) -> frozenset[str]:
    document, _warnings, _losses = parse_cfx_with_loss_locations(candidate.xhtml)
    return frozenset(identity.macro_id for identity in macro_identities(document) if identity.macro_id is not None)


def build_replacement_manifest(
    *,
    base_artifact: MarkdownArtifact,
    candidate: CfxArtifact,
) -> ReplacementManifest:
    """Describe identities absent from the final XHTML that will be written.

    ``candidate.xhtml`` is intentionally the only candidate input.  It is the
    post-render value after identity carry and remote-subtree preservation, so a
    caller cannot approve a pre-render estimate that differs from its PUT body.
    Raw macro UUIDs affect only ``discarded_identities_sha256`` and never appear
    in the returned public manifest.
    """

    candidate_ids = _candidate_macro_ids(candidate)
    public: list[DiscardedIdentity] = []
    private: list[_PrivateDiscardedIdentity] = []
    for ordinal, identity in enumerate(base_artifact.macro_identities):
        if identity.macro_id is None or identity.macro_id in candidate_ids:
            continue
        path = _identity_location(base_artifact, identity)
        public.append(
            DiscardedIdentity(
                kind=identity.kind,
                semantic_path=path,
                ordinal=ordinal,
            )
        )
        private.append(
            {
                "kind": identity.kind,
                "semantic_path": list(path),
                "ordinal": ordinal,
                "macro_id": identity.macro_id,
                "schema_version": identity.schema_version,
            }
        )

    public.sort(key=lambda item: (item.kind, item.semantic_path, item.ordinal))
    private.sort(
        key=lambda item: (
            str(item["kind"]),
            tuple(str(part) for part in item["semantic_path"]),
            int(item["ordinal"]),
            str(item["macro_id"]),
        )
    )
    candidate_sha256 = _sha256(candidate.xhtml.encode("utf-8"))
    discarded_sha256 = _sha256(
        json.dumps(private, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return ReplacementManifest(
        candidate_storage_sha256=candidate_sha256,
        discarded_identities_sha256=discarded_sha256,
        discarded_identities=tuple(public),
    )


__all__ = [
    "REPLACEMENT_MANIFEST_SCHEMA",
    "DiscardedIdentity",
    "ReplacementManifest",
    "build_replacement_manifest",
]
