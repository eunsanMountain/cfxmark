# cfxmark

**Bidirectional Markdown ↔ Confluence Storage XHTML converter** —
with explicit migration reports, managed Markdown projections, and
source-bound ownership proof for publishing edited remote content.

```python
import cfxmark

# Markdown → Confluence storage XHTML
result = cfxmark.to_cfx(markdown_text)
result.xhtml          # str    — ready for Confluence REST PUT
result.attachments    # tuple  — local file refs the caller should upload
result.warnings       # tuple  — human-readable conversion warnings

# Confluence storage XHTML → Markdown
result = cfxmark.to_md(xhtml_text)
result.markdown       # str    — canonical markdown
result.warnings       # tuple

# Markdown or Confluence XHTML → Jira wiki markup
result = cfxmark.to_jira_wiki(markdown_text)
result.jira_wiki      # str | None — Jira wiki markup

# Jira wiki markup → Markdown  (EXPERIMENTAL — v0.3+, enhanced v0.4)
from cfxmark.jira import from_jira_wiki
result = from_jira_wiki(jira_issue_description)
result.markdown       # str
result.attachments    # tuple  — filenames referenced via [^file] / !file!
result.warnings       # tuple  — unsupported user mentions, …
```

`ConversionResult` is the same dataclass for all directions —
`xhtml` is populated for `to_cfx`, `markdown` for `to_md` /
`from_jira_wiki`, and `jira_wiki` for `to_jira_wiki`. Confluence
conversions also expose `push_safe` and `losses` so callers can keep
read-only output separate from editable round-trip output.

### Markdown profiles

`ConversionOptions(profile=...)` selects the Confluence-to-Markdown
contract:

- `legacy` is the backward-compatible default and preserves unknown
  content through the existing opaque XML sidecar format.
- `editable` emits semantic Markdown, compact color categories, image
  metadata, and alignment markers. It sets `push_safe=False` and emits
  an unsupported-content marker when a construct cannot be represented
  without raw XML. Unsupported markers name the macro when available, remain
  stable across normalization, and are rejected by `to_cfx()`.
- `readable` removes round-trip-only visual metadata for concise reading.
  Its output is always `push_safe=False` and should not be published.

```python
editable = cfxmark.to_md(
    xhtml,
    options=cfxmark.ConversionOptions(profile="editable"),
)
if not editable.push_safe:
    raise ValueError("page contains unsupported content")
```

Known colors use compact names such as `red`, `green`, and `amber`. An exact
color outside the known palette remains in `data-cfx-color` and produces a
warning, so state-bearing color is never removed silently.

Known inline Jira macros stay readable and editable without an opaque XML
payload:

```markdown
Related issue: {{jira key="DOC-123"}}
Matching issues: {{jira jqlQuery="project = DOC" count="true"}}
```

A paragraph containing only a table-of-contents macro uses the existing block
directive form:

```markdown
::: toc maxLevel="3"
:::
```

Confluence's structural `content-wrapper` div around table-cell content is
transparent. Styled divs and nested tables remain unsupported in editable
Markdown, so callers can block a lossy publish through `push_safe=False`.

## Why another converter?

Two existing projects inspired this one — [`md2cf`][md2cf] and
[`md2conf`][md2conf] — but both are **one-directional** (md → cf) and
neither preserves unknown macros across a round trip. `cfxmark` fills
both gaps:

1. **Bidirectional.** `to_md(to_cfx(m))` is byte-identical to
   `canonicalize(m)` for every construct in the supported subset.
2. **Opaque preservation.** Confluence content cfxmark doesn't
   understand (custom plugins, drawio diagrams, exotic table cells)
   round-trips byte-for-byte, **including the `ac:macro-id` UUID**.
   Confluence treats the round-tripped macro as the same instance, so
   comments, attachments, and permissions stay attached.
3. **Pure text-in / text-out.** No Confluence API, no network, no
   attachment upload. The caller owns REST I/O. (See "Image assets"
   below for the helper function that lets the caller plug in
   network-bound logic without bloating cfxmark.)

[md2cf]: https://github.com/iamjackg/md2cf
[md2conf]: https://github.com/hunyadi/md2conf

## Install

cfxmark is a converter library. Network transport and credentials remain the
caller's responsibility:

```bash
# Core: Markdown ↔ Confluence XHTML converter + Jira wiki renderer
pip install cfxmark

# With uv (recommended)
uv add cfxmark
```

cfxmark depends on `lxml` and `mistletoe`. Python 3.10+.

## The contract

cfxmark grades every Confluence construct into one of three buckets:

| Grade | Description | Behaviour |
|---|---|---|
| **I — Native** | Standard CommonMark / GFM (headings, lists, tables, code fences, links, images, blockquote, hr, inline emphasis) | Lossless round-trip after canonicalization. |
| **II — Directive** | Confluence macros with a known Markdown directive mapping (`info`, `note`, `warning`, `tip`, `jira`, `expand`, `toc`) | Lossless after canonicalization. Pluggable via `MacroRegistry`. |
| **III — Opaque** | Everything else | The legacy profile carries verified XML payloads through opaque-block / inline-opaque markers. Editable projections report unsupported content instead of claiming a universally lossless publish. |

See [`docs/SPEC.md`](https://github.com/eunsanMountain/cfxmark/blob/main/docs/SPEC.md)
for the full mapping table and
[`docs/OPAQUE.md`](https://github.com/eunsanMountain/cfxmark/blob/main/docs/OPAQUE.md)
for the opaque-block format.

## Usage

### Edit a remote Confluence page with source-bound proof

```python
import cfxmark

# Use the same exact options for pull, candidate construction, and validation.
options = cfxmark.ConversionOptions(profile="editable")

# Whatever fetched the page (REST API call, exported XML file, ...)
xhtml_s0 = my_confluence_client.get_storage_format(page_id)

# Convert once and write the managed projection. `markdown` remains the body
# against which source ranges are measured; `managed_markdown` is the file.
base = cfxmark.to_md_artifact(xhtml_s0, options=options)
write_text("page.md", base.managed_markdown)

# … user edits the Markdown …
edited_m1 = read_text("page.md")

# Build C0 and C1 internally, then classify every S0 -> C1 leaf change.
candidate = cfxmark.to_cfx_artifact(
    edited_m1,
    presentation=base.presentation,
    base_artifact=base,
    splice_source=xhtml_s0,
    options=options,
)

# Re-fetch immediately before publication and reject stale or fatal proof.
fresh_storage = my_confluence_client.get_storage_format(page_id)
cfxmark.validate_managed_cfx_artifact(candidate, source_storage=fresh_storage, options=options)
if not candidate.push_safe:
    raise ValueError(candidate.migration_report)

my_confluence_client.update_page(page_id, candidate.xhtml)

# Optionally upload any newly referenced local images
for filename in candidate.attachments:
    my_confluence_client.upload_attachment(page_id, filename)
```

`to_md_artifact()` exposes a classified `migration_report`. Each atomic
occurrence has a stable occurrence ID tied to the source storage hash. The
managed projection places disclosure, summary, and occurrence comments near
the affected content without shifting the ranges stored against `markdown`.

When both `base_artifact` and `splice_source` are supplied,
`to_cfx_artifact()` returns a managed candidate with an immutable ownership
proof. It hashes S0/M0/C0/M1/C1, converter and profile; names the actual edit
operation IDs and migration occurrence IDs; and records unclassified,
multiply-owned, overlapping, or otherwise ambiguous changes. Every final
storage leaf must have exactly one owner. Missing source maps, duplicate
identity, ambiguous move correspondence, stale source, or fatal diagnostics
make publication unsafe. Publish preflight rebuilds the semantic matcher and
storage source-map claims from the candidate document, then verifies each
intended leaf against the independently derived edit operation. It rejects a
fatal proof regardless of the candidate's `push_safe` field. Merely appearing
in a stage diff is not ownership. If two producer diagnostics reuse one
occurrence ID for different source locations, both occurrences remain visible
and the candidate fails closed instead of silently deduplicating them.

### Image assets

When you convert a Confluence page that references uploaded
attachments, the resulting Markdown looks like this:

```markdown
![](image-3.png#cfxmark:w=700)<!-- cfxmark:asset src="image-3.png" -->
```

The image link still points at the original Confluence filename
(broken in any local Markdown viewer until you fetch the bytes), and
the `<!-- cfxmark:asset -->` HTML comment carries enough metadata for
a follow-up step to fetch and embed.

`cfxmark.resolve_assets` is that follow-up step. You provide a
fetcher callback that returns bytes for one filename at a time, and
choose between two output strategies:

```python
import cfxmark
from pathlib import Path

def fetcher(filename: str) -> bytes:
    # Whatever you use to download from Confluence:
    return my_confluence_client.download_attachment(page_id, filename)

# Strategy A — sidecar directory (recommended for git-tracked docs).
# Saves bytes to ./assets/ and rewrites links to relative paths.
md = cfxmark.resolve_assets(
    md_result.markdown,
    fetcher,
    mode="sidecar",
    asset_dir="docs/page-42/assets",
    md_path="docs/page-42.md",
)
Path("docs/page-42.md").write_text(md)
# docs/page-42/assets/image-3.png exists
# md link: ![](assets/image-3.png#cfxmark:w=700)<!-- cfxmark:asset src="image-3.png" -->

# Strategy B — inline data URIs (single self-contained file).
md = cfxmark.resolve_assets(md_result.markdown, fetcher, mode="inline")
# md link: ![](data:image/png;base64,iVBORw0K...)<!-- cfxmark:asset src="image-3.png" -->
```

The asset markers are **preserved** through both strategies, so
`resolve_assets` is idempotent and a subsequent `to_cfx` call always
recovers the original Confluence filename — even if the visible link
target has been rewritten to a sidecar path or a data URI.

### Mermaid diagrams

cfxmark maps Markdown's `` ```mermaid `` fenced code block to
Confluence's `code` macro with `language=mermaid`. If your Confluence
instance has a Mermaid plugin installed (e.g. *Mermaid Diagrams for
Confluence*) it will render the diagram automatically; otherwise the
content is shown as a syntax-highlighted code block.

```markdown
​```mermaid
graph LR
  A --> B --> C
​```
```

### Inline opaque references

Inline elements that have no native or directive Markdown form — Confluence
user mentions, unsupported inline macros, custom widget invocations, … —
become a short Markdown link with a `cfx:op-...` URL:

```markdown
Contact the purchaser ([@user-2c9402cc](cfx:op-4fab0f8d))
```

The `[label]` is auto-derived from the underlying element type
(`@user-…`, `jira:PROJ-1`, `cfx:status`, …) and the `op-XXXXXXXX` ID
is a SHA-256 prefix of the original XML payload. The full XML lives
in a `cfxmark:payloads` sidecar at the bottom of the same Markdown
file:

```markdown
<!-- cfxmark:payloads -->
<!-- op-4fab0f8d
<ac:link><ri:user ri:userkey="2c9402cc83d4bcc40183d976ef730001"/></ac:link>
-->
<!-- /cfxmark:payloads -->
```

The SHA-256 fingerprint means a user who **types** that exact link
syntax in their own Markdown is not silently re-interpreted as an
opaque payload — the verification fails and the region falls back to
ordinary text.

### Block opaque blocks

Block-level Confluence content cfxmark doesn't know how to convert
(e.g. drawio diagrams, plantuml, complex tables) is wrapped in a
fenced code block with sentinel comments:

````markdown
<!-- cfxmark:opaque id="op-1188e2b4" -->
```cfx-storage
<ac:structured-macro ac:name="drawio" ac:macro-id="...">
  <ac:parameter ac:name="diagramName">flow</ac:parameter>
  ...
</ac:structured-macro>
```
<!-- /cfxmark:opaque -->
````

Editors render this as a clearly visible code block — a "do not
touch" signal for human readers. The Markdown parser detects the
sentinels first and round-trips the contents byte-for-byte, including
the original `ac:macro-id` UUID that Confluence uses to identify
macro instances.

### Header notice

When a converted Markdown document contains any opaque or directive
markers, cfxmark prepends a single-line HTML comment explaining the
conventions to humans and AI agents:

```markdown
<!-- cfxmark:notice Converted from Confluence storage format. Inline
[label](cfx:op-XXXXXXXX) references preserve Confluence content that
has no native Markdown form; the raw XML for each lives in the
cfxmark:payloads sidecar at the bottom of this file. Do not edit
those references or the sidecar — tampering invalidates a SHA-256
fingerprint and the round trip falls back to plain text. -->
```

The comment is invisible in any Markdown viewer.

### Custom macros

Promote a Confluence macro from "opaque" to "directive" by registering
a custom handler:

```python
import cfxmark
from cfxmark.macros import MacroRegistry
from cfxmark.macros.builtins import AdmonitionHandler

# Start from the default registry and add your own.
my_registry = cfxmark.default_registry.copy()
# Built-in AdmonitionHandler accepts one of: "info", "note", "warning", "tip".
# To promote a previously-opaque macro, write a small MacroHandler subclass —
# see cfxmark/macros/builtins/admonition.py for a complete example.
my_registry.register(AdmonitionHandler("warning"))

result = cfxmark.to_md(xhtml, macros=my_registry)
```

Implementing a `MacroHandler` from scratch requires a small amount
of lxml knowledge — see `cfxmark/macros/builtins/admonition.py` for
a complete example. A higher-level handler API that hides lxml is
planned for v0.3.

### Canonicalization helpers

cfxmark ships two canonicalization helpers, one for each side of the
pipeline. Both are idempotent: `f(f(x)) == f(x)`.

#### `canonicalize_cfx(xhtml)` — compare two storage fragments

Two Confluence storage fragments are "the same" only after a deep
normalization pass that strips volatile attributes, editor noise,
and rendering hints. Use `canonicalize_cfx` to compare two snapshots:

```python
import cfxmark

c1 = cfxmark.canonicalize_cfx(original_xhtml)
c2 = cfxmark.canonicalize_cfx(round_tripped_xhtml)
assert c1 == c2  # passes for any document in the supported subset
```

`canonicalize_cfx` is useful for equality and fixed-point checks. It is not a
publish authorization boundary: remote edits must use a managed candidate and
`validate_managed_cfx_artifact` against freshly fetched storage before PUT.

```python
fresh_storage = my_client.get_storage_format(page_id)
cfxmark.validate_managed_cfx_artifact(candidate, source_storage=fresh_storage, options=options)
if candidate.push_safe:
    my_client.update_page(page_id, candidate.xhtml)
```

#### `normalize_md(markdown)` — converge hand-edited Markdown

`normalize_md` is the Markdown-side counterpart: it runs the document
through `parse_md → render_md` so the output is exactly the form
cfxmark would have produced. Applying it before push flattens any
drift introduced by hand edits, a different editor's Markdown
autoformatter, or a historical cfxmark version.

```python
import cfxmark

# Pre-push recipe: normalize hand-edited Markdown so the canonical
# XHTML body is stable across authors and editor plugins.
clean_md = cfxmark.normalize_md(local_md_from_disk)
xhtml = cfxmark.to_cfx(clean_md).xhtml
```

The key property: a document produced by `to_md` is already a fixed
point of `normalize_md`, so round-trippers pay nothing. Hand-edited
documents converge in a single pass, and that pass is enough to
eliminate the "local file drifted from the round-trip form" class of
bug (for example, stray ``**`` delimiters in positions where cfxmark
would have emitted raw `<strong>` HTML because of CommonMark's CJK
word-boundary rule).

If you only push `normalize_md(text)` rather than raw hand-edits,
the `canonicalize_cfx` diff above stays stable across collaborators.

### Jira wiki output

`to_jira_wiki` converts Markdown or Confluence storage XHTML to Jira
wiki markup. It accepts the same source formats as `to_cfx` / `to_md`
and auto-detects which format it received.

```python
import cfxmark

result = cfxmark.to_jira_wiki(markdown_text)
print(result.jira_wiki)   # h2. Heading\n\n*bold* text …
```

Two optional parameters cover common push-pipeline patterns:

```python
import re

# Only render the body of the first H2 section titled "Summary".
result = cfxmark.to_jira_wiki(markdown_text, section="Summary")

# Drop a leading cfxmark:notice comment before rendering
# (useful when pushing a round-tripped Confluence page to Jira).
result = cfxmark.to_jira_wiki(
    markdown_text,
    drop_leading_notice=(re.compile(r"cfxmark:notice"),),
)
```

Code block language identifiers can be normalised for Jira Server
compatibility:

```python
result = cfxmark.to_jira_wiki(
    markdown_text,
    code_language_map={"ts": "javascript", "kotlin": "java"},
)
```

`result.jira_wiki` is `None` when `section=` is specified but not
found in the document.

## Jira wiki (experimental, lossy)

The supported Confluence subset reaches a canonical fixed point. The legacy
profile can additionally carry verified opaque payloads while their control
markers remain intact. Editable and managed publishing instead exposes every
known migration and requires a source-bound proof; it does not make a blanket
lossless claim. The Jira wiki direction has no opaque-macro identity
preservation, and several constructs have no equivalent on the Markdown side.

Keep the two contracts at different import sites so the asymmetry is
visible:

```python
from cfxmark import to_cfx, to_md           # canonical Confluence conversion
from cfxmark.jira import from_jira_wiki, to_jira_wiki  # experimental
```

### Contract

The strongest guarantee the Jira pipeline offers is:

> `from_jira_wiki(jira_wiki)` produces a :class:`ConversionResult` whose
> Markdown representation reaches a **fixed point** after at most two
> `wiki → md → wiki` iterations. Pass 1 (`wiki → md`) is a one-way
> canonicalization; pass 2 (`md → wiki → md`) must be idempotent. A
> fixture corpus of 6 representative Jira issue descriptions is
> exercised in `tests/unit/test_jira_wiki_parser.py` to pin this
> contract.

Explicitly **allowed** canonicalization (not considered a diff):

- Heading spacing, list indent, trailing whitespace normalisation
- Soft-break inside a paragraph collapsed into a single line
- Jira `{panel}` macro mapped to `{note}` admonition
- `_italic_` canonicalised to Markdown `*italic*`
- `-strike-` canonicalised to Markdown `~~strike~~`
- `~sub~` → `<sub>`, `^sup^` → `<sup>`, `+ins+` → `<ins>` (v0.4+)
- `{color:#hex}text{color}` → `<span style="color:#hex">text</span>` (v0.4+)
- `??text??` → `<cite>text</cite>` (v0.4+)

Explicitly **forbidden** (would break the contract):

- Content loss inside headings, paragraphs, list items, code blocks
- Re-ordering of top-level blocks
- URL rewriting in links
- Renaming attachments (filenames in `[^file]` / `!file!` are preserved
  verbatim in `ConversionResult.attachments`)

### Lossy mapping table

| Jira wiki                    | Markdown                                | Note                                     |
|------------------------------|------------------------------------------|------------------------------------------|
| `h1.`…`h6.`                  | `#`…`######`                             | 1:1 identity (no promotion on parse)     |
| `*bold*`                     | `**bold**`                               | boundary-aware                           |
| `_italic_`                   | `*italic*`                               |                                          |
| `-strike-`                   | `~~strike~~`                             | boundary-aware                           |
| `{{mono}}`                   | `` `mono` ``                             |                                          |
| `~sub~`                      | `<sub>sub</sub>`                         | Subscript node (v0.4+)                   |
| `^sup^`                      | `<sup>sup</sup>`                         | Superscript node (v0.4+)                 |
| `+ins+`                      | `<ins>ins</ins>`                         | Underline node (v0.4+)                   |
| `{color:#hex}x{color}`       | `<span style="color:#hex">x</span>`     | ColorSpan node (v0.4+)                   |
| `??text??`                   | `<cite>text</cite>`                      | Citation node (v0.4+)                    |
| `[url]`                      | `[url](url)` (empty label form)          |                                          |
| `[label\|url]`               | `[label](url)`                           | label may contain nested `[...]`         |
| `[^file.png]`                | `![](file.png)`                          | when extension is image-like             |
| `[^file.msg]`                | `[file.msg](attachment:file.msg)`        | otherwise                                |
| `[~user]`                    | *(dropped)*                              | warning recorded                         |
| `{code:python}body{code}`    | ` ```python\nbody\n``` `                 |                                          |
| `{noformat}body{noformat}`   | ` ```\nbody\n``` `                       |                                          |
| `{quote}body{quote}`, `bq.`  | `> body`                                 |                                          |
| `{info}`/`{note}`/`{warning}`/`{tip}` | `> [!INFO]` callout             | GitHub / Obsidian style                  |
| `{panel:title=X}body{panel}` | `> [!NOTE] X` + warning                  | D4 mapping                               |
| `h3. *Title*`                | `### **Title**`                          | literal nested bold preserved            |
| Multi-line table cell        | GFM cell with `<br>` soft break          |                                          |
| `\|\|h1\|\|h2\|\|` header row | GFM header row                          |                                          |
| `----`                       | `---`                                    |                                          |

Warnings accumulate on `ConversionResult.warnings`; fixture tests in
`tests/unit/test_jira_wiki_parser.py` pin the behaviour of every
entry in this table.

### Heading promotion on the output side

`to_jira_wiki` has a `heading_promotion` keyword that controls the
heading level mapping:

- `"confluence"` *(default)* — Markdown H3 collapses to Jira `h2`
  (and H4 → `h3`, …). Use when the Jira wiki output will be pushed
  to a **Confluence page** whose title already occupies the top slot.
- `"jira"` — identity mapping. Use when the output is pushed to a
  **Jira issue description**, because the issue title lives in a
  separate field and the body can start at `h1`.
- `"none"` — alias for `"jira"`.

```python
from cfxmark import to_jira_wiki

# Confluence push — historical default
to_jira_wiki(md)

# Jira issue description push
to_jira_wiki(md, heading_promotion="jira")
```

### HTML comment passthrough (wrapper metadata)

If your wrapper embeds caller-owned metadata as HTML comments in the
local Markdown file — for example a workflow manifest:

```markdown
# My feature

<!-- workflow:meta
  key: TASK-42
  type: Story
  last_synced_version: 15
-->

body...
```

— opt in via `ConversionOptions.passthrough_html_comment_prefixes`.
Matching comments are preserved verbatim across `parse_md` /
`render_md`, and silently dropped by `to_cfx` / `to_jira_wiki` so
they never leak to Confluence or Jira:

```python
from cfxmark import ConversionOptions, to_cfx, to_md
from cfxmark.normalize import strip_passthrough_comments

opts = ConversionOptions(
    passthrough_html_comment_prefixes=("workflow:",)
)

# Push: comment is dropped on the way to Confluence
result = to_cfx(local_md, options=opts)

# Canonical compare: strip comments on both sides before diffing
left = strip_passthrough_comments(local_md, ("workflow:",))
right = strip_passthrough_comments(pulled_md, ("workflow:",))
assert left == right
```

`cfxmark:` prefixes are filtered out so cfxmark's own sentinel
comments (`cfxmark:opaque`, `cfxmark:notice`) cannot be hijacked.

## Security

cfxmark hardens its XML parser against XXE and billion-laughs attacks:

- Inputs containing `<!DOCTYPE>` or `<!ENTITY>` declarations are
  rejected before lxml ever sees them.
- The lxml parser is configured with `no_network=True`,
  `load_dtd=False`, and `huge_tree=False`.
- Opaque-block sentinels are SHA-256 verified — accidental sentinel
  syntax in user-typed Markdown does **not** become a real opaque
  block.

If you find a security issue, please open a GitHub issue.

## Stability contract

The following names are covered by semantic versioning and will not be
removed or incompatibly changed without a major version bump:

**`cfxmark` package** — `to_cfx`, `to_md`, `to_jira_wiki`,
`from_jira_wiki`, `canonicalize_cfx`, `normalize_md`,
`strip_passthrough_comments`, `resolve_assets`, `ConversionResult`,
`ConversionOptions`, `DEFAULT_OPTIONS`, `AssetFetcher`, `ResolveMode`,
`CfxmarkError`, `ConversionError`, `MacroError`, `ParseError`,
`AssetSecurityError`, `MacroRegistry`, `default_registry`.

**`cfxmark.jira`** — `to_jira_wiki`, `from_jira_wiki`.
**Experimental in v0.3**: the Jira parser contract is "converges
after at most three `wiki → md → wiki` iterations", not
byte-identical. The top-level `to_jira_wiki`, `from_jira_wiki`, and
`ConversionResult.jira_wiki` field ARE stable — only the quality
of the round-trip itself is experimental. See the "Jira wiki
(experimental, lossy)" section above for the full contract.

Guarantees:
- Breaking changes bump the minor version for 0.x.y releases.
- `canonicalize_cfx` normalization rules are cumulative — each release
  is a strict superset of the previous release's canonicalization.
- Deprecations are announced one minor version before removal.

Not covered: underscore-prefixed symbols, `parsers.*` / `renderers.*` /
`ast.*` internals, logging message wording, `ConversionResult.document`
AST shape, warning message wording.

Note: 0.x.y versioning is looser than 1.x.y — minor version bumps may
carry breaking changes as noted above.

## Development

```bash
git clone https://github.com/eunsanMountain/cfxmark
cd cfxmark
uv sync --all-extras

# Run all tests
uv run pytest

# Type-check
uv run mypy src/

# Lint
uv run ruff check .

# Build
uv build
```

The corpus tests look for `.cfx` files in `tests/corpus/` (gitignored
to keep your own private samples out of version control). Drop your
own Confluence storage XHTML there and they will be exercised by
`pytest tests/test_corpus.py`.

## License

MIT. See [`LICENSE`](https://github.com/eunsanMountain/cfxmark/blob/main/LICENSE).
