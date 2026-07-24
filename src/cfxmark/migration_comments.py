"""Control comments carried by managed Markdown.

Plan reference: `.omx/plans/atls-0.3.0-markdown-first-informed-migration-plan.md`
sections 6.1 to 6.5.

Managed Markdown mixes the user's prose with a few machine-readable comments.
Two constraints shape this module.

Stripping must be exact. The canonical content hash is defined over the document
with control comments removed, so a stripper that misses one, or that eats a
line the user actually wrote, silently changes the hash and breaks the chain
between what was approved and what is published. Marker-shaped text inside a
code fence is the user's content and stays.

The document must stay readable. A table with 220 styled cells is one decision;
emitting one comment per cell would bury the prose. Impacts are therefore
grouped for display while the atomic occurrences remain authoritative for
consent.

`cfxmark:img` is deliberately not control: it carries storage meaning that
survives into the published page, so it is content and is never stripped.
"""

from __future__ import annotations

import re

from cfxmark.migration_report import MigrationGroup, MigrationReport
from cfxmark.parsers.md import _lines_outside_fences

NOTICE_TEXT = (
    "Edit Markdown normally. Migration comments describe publish-time changes; "
    "run `atls confluence page push-md ... --dry-run` before publishing."
)

# Comments the control plane owns. Everything here is removed before hashing and
# before publishing. `cfxmark:img` is absent on purpose.
_CONTROL_PREFIXES = (
    "atls:managed",
    "atls:operation",
    "cfxmark:notice",
    "cfxmark:migrations",
    "cfxmark:migration",
    "cfxmark:asset",
)

_CONTROL_RE = re.compile(
    r"<!--\s*(?P<name>" + "|".join(re.escape(prefix) for prefix in _CONTROL_PREFIXES) + r")\b.*?-->",
    re.DOTALL,
)

_KEY_VALUE_RE = re.compile(r"^(?P<key>[a-z_][a-z0-9_]*)=(?P<value>\S+)$")

_EFFECT_ORDER = ("preserved", "converted", "normalized", "removed", "unsupported", "fatal")


def render_notice() -> str:
    return f"<!-- cfxmark:notice {NOTICE_TEXT} -->"


def render_migrations_summary(report: MigrationReport) -> str:
    """One line summarizing the whole report, anchored to its hash.

    Effects with no occurrences are omitted: a row of zeroes tells the reader
    nothing and competes with the counts that matter.
    """
    counts = report.counts()
    parts = ["v=1"]
    parts.extend(f"{effect}={counts[effect]}" for effect in _EFFECT_ORDER if counts.get(effect))
    parts.append(f"report={report.sha256()}")
    return f"<!-- cfxmark:migrations {' '.join(parts)} -->"


def _scope_token(group: MigrationGroup) -> str:
    scope = group.scope
    if scope is None:
        return "document"
    identity = scope.stable_id or scope.anchor or scope.name
    return f"{scope.kind}:{identity}" if identity else scope.kind


def render_migration_marker(group: MigrationGroup) -> str:
    """A block-local marker for one grouped impact.

    Carries identifiers and a count only. Page content never appears here: the
    comment is a pointer into the report, not a copy of what changed.
    """
    parts = [
        f"effect={group.effect}",
        f"code={group.code}",
        f"count={group.count}",
        f"scope={_scope_token(group)}",
    ]
    return f"<!-- cfxmark:migration {' '.join(parts)} -->"


def _parse_fields(line: str, *, marker: str) -> dict[str, str]:
    stripped = line.strip()
    prefix = f"<!-- {marker} "
    if not stripped.startswith(prefix) or not stripped.endswith("-->"):
        raise ValueError(f"not a {marker} comment: {line!r}")
    body = stripped.removeprefix(prefix).removesuffix("-->").strip()
    if not body:
        raise ValueError(f"{marker} comment carries no fields")
    fields: dict[str, str] = {}
    for token in body.split(" "):
        if not token:
            continue
        match = _KEY_VALUE_RE.match(token)
        if match is None:
            raise ValueError(f"{marker} field is not a valid k=v token: {token!r}")
        key = match.group("key")
        if key in fields:
            raise ValueError(f"{marker} repeats field {key!r}")
        fields[key] = match.group("value")
    return fields


def parse_migrations_summary(line: str) -> dict[str, str]:
    return _parse_fields(line, marker="cfxmark:migrations")


def parse_migration_marker(line: str) -> dict[str, str]:
    return _parse_fields(line, marker="cfxmark:migration")


def strip_control_comments(markdown: str, *, preserve_asset_markers: bool = False) -> str:
    """Remove control comments, leaving fenced and indented code untouched.

    Marker-shaped text inside a code block is documentation the user wrote, so
    rewriting it would corrupt their content and change the hash for a reason
    that has nothing to do with the control plane.
    """
    lines = markdown.split("\n")
    editable = _lines_outside_fences(lines)
    result: list[str] = []
    dropped_control_line = False
    for index, line in enumerate(lines):
        if not editable[index] or line.startswith("    ") or line.startswith("\t"):
            result.append(line)
            dropped_control_line = False
            continue
        cleaned = _CONTROL_RE.sub(
            lambda match: match.group(0) if preserve_asset_markers and match.group("name") == "cfxmark:asset" else "",
            line,
        )
        if cleaned.strip() == "" and line.strip() != "":
            # The line held nothing but control comments; drop it rather than
            # leaving blank lines that would shift the document's structure.
            dropped_control_line = True
            continue
        if dropped_control_line and cleaned.strip() == "":
            # The blank line that separated a control block from the content
            # below it. Markdown needs that blank so an HTML comment does not
            # absorb the following paragraph, but it belongs to the control
            # block, not the document -- keeping it would mean stripping the
            # header never quite returns the body it was wrapped around.
            # Only the first is consumed, so a blank line the user wrote
            # between two of their own blocks survives.
            dropped_control_line = False
            continue
        dropped_control_line = False
        result.append(cleaned)
    return "\n".join(result)


def canonical_content(markdown: str) -> str:
    """The exact text the content hash is defined over (plan 6.5)."""
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    stripped = strip_control_comments(normalized)
    return stripped.rstrip("\n") + "\n"


__all__ = [
    "NOTICE_TEXT",
    "canonical_content",
    "parse_migration_marker",
    "parse_migrations_summary",
    "render_migration_marker",
    "render_migrations_summary",
    "render_notice",
    "strip_control_comments",
]
