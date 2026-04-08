# cfxmark

**Bidirectional Markdown ↔ Confluence Storage XHTML converter** —
with lossless opaque preservation for everything cfxmark doesn't
explicitly know how to convert.

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
```

`ConversionResult` is the same dataclass for all directions —
`xhtml` is populated for `to_cfx`, `markdown` for `to_md`,
`jira_wiki` for `to_jira_wiki`.

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

cfxmark ships in two modes:

```bash
# Core: Markdown ↔ Confluence XHTML converter + Jira wiki renderer
pip install cfxmark

# With uv (recommended):
uv add cfxmark

# With the optional Confluence REST client (zero additional deps —
# the extra is namespace-only and reserves a stable upgrade slot):
pip install 'cfxmark[confluence]'
```

The `confluence` extra declares zero third-party runtime dependencies —
`from cfxmark.confluence import ConfluenceClient` works even without it.
The extra exists to signal intent in requirements files and to reserve a
stable upgrade slot for future convenience helpers.

cfxmark depends on `lxml` and `mistletoe`. Python 3.10+.

## The contract

cfxmark grades every Confluence construct into one of three buckets:

| Grade | Description | Behaviour |
|---|---|---|
| **I — Native** | Standard CommonMark / GFM (headings, lists, tables, code fences, links, images, blockquote, hr, inline emphasis) | Lossless round-trip after canonicalization. |
| **II — Directive** | Confluence macros with a known Markdown directive mapping (`info`, `note`, `warning`, `tip`, `jira`, `expand`, `toc`) | Lossless after canonicalization. Pluggable via `MacroRegistry`. |
| **III — Opaque** | Everything else | Captured byte-for-byte through cfxmark's opaque-block / inline-opaque mechanism. **Never dropped, never rewritten.** |

See [`docs/SPEC.md`](https://github.com/eunsanMountain/cfxmark/blob/main/docs/SPEC.md)
for the full mapping table and
[`docs/OPAQUE.md`](https://github.com/eunsanMountain/cfxmark/blob/main/docs/OPAQUE.md)
for the opaque-block format.

## Usage

### Round-trip a Confluence page through Markdown

```python
import cfxmark

# Whatever fetched the page (REST API call, exported XML file, …)
xhtml = my_confluence_client.get_storage_format(page_id)

# Convert to Markdown
md_result = cfxmark.to_md(xhtml)
markdown = md_result.markdown

# … user edits the Markdown …

# Convert back to Confluence storage XHTML
cfx_result = cfxmark.to_cfx(markdown)
my_confluence_client.update_page(page_id, cfx_result.xhtml)

# Optionally upload any newly referenced local images
for filename in cfx_result.attachments:
    my_confluence_client.upload_attachment(page_id, filename)
```

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

Inline elements that have no native Markdown form — Confluence user
mentions, inline Jira issue macros, custom widget invocations, … —
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

`canonicalize_cfx` is the same function the test suite uses to
verify byte-identical round trips against real Confluence pages.
A good push pipeline calls it **before** the REST PUT so an unchanged
body is skipped entirely:

```python
remote = cfxmark.canonicalize_cfx(my_client.get_page(page_id))
local = cfxmark.canonicalize_cfx(cfxmark.to_cfx(local_md).xhtml)
if remote != local:
    my_client.update_page(page_id, ...)
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

`result.jira_wiki` is `None` when `section=` is specified but not
found in the document.

## Confluence client (optional extra)

Install with the `confluence` extra to signal intent — the client
itself is always importable because it is built on Python's standard
library:

```bash
pip install 'cfxmark[confluence]'
```

The extra declares **zero additional runtime dependencies**. It exists to:
1. Signal the dependency in your `requirements.txt` / `pyproject.toml`
   so readers see that you rely on the optional subsystem.
2. Reserve a stable upgrade slot — if future convenience helpers
   (credential stores, rich CLI) gain third-party deps, the extra is
   the place they'll land.

```python
from cfxmark.confluence import ConfluenceClient, BearerTokenFile

client = ConfluenceClient(
    host="https://confluence.example.com",
    auth=BearerTokenFile("~/.secrets/confluence_pat"),
    dialect="server",
)

# Canonical-aware push — skips the REST PUT entirely when the remote
# body is byte-equivalent to the rendered local Markdown.
result = client.push_markdown(
    page_id="12345",
    md_text=my_markdown,
    md_path="docs/my_page.md",
    on_conflict="abort",
)
if result.changed:
    print(f"Pushed. Uploaded {len(result.uploaded_attachments)} new attachments.")
    if result.has_partial_failure:
        for name, ex in result.failed_attachments:
            print(f"  ! attachment {name} failed: {ex!r}")
else:
    print("No-op; remote is already current.")

# Canonical-aware pull with resolved assets in a sidecar directory.
pull = client.pull_markdown(
    page_id="12345",
    md_path="docs/my_page.md",
    resolve_assets_mode="sidecar",
    asset_dir="docs/my_page-assets",
)
```

**Logging.** The client uses `logging.getLogger("cfxmark.confluence")`
exclusively — no direct writes to `sys.stdout` or `sys.stderr`.
Enable progress output with:

```python
import logging
logging.getLogger("cfxmark").setLevel(logging.INFO)
```

**Confluence dialect.** The default is `dialect="server"` because
Confluence Server / Data Center is the reference test target.
Confluence Cloud users should pass `dialect="cloud"` — the
`X-Atlassian-Token: no-check` XSRF bypass header (mandatory on
Server, unsupported on Cloud) is gated on this setting. Cloud support
is best-effort; if you hit a Cloud-only regression, please open an
issue.

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
`canonicalize_cfx`, `normalize_md`, `resolve_assets`,
`ConversionResult`, `ConversionOptions`, `DEFAULT_OPTIONS`,
`AssetFetcher`, `ResolveMode`, `CfxmarkError`, `ConversionError`,
`MacroError`, `ParseError`, `AssetSecurityError`, `MacroRegistry`,
`default_registry`.

**`cfxmark.confluence`** — `ConfluenceClient`, `PushResult`,
`PullResult`, `Auth`, `BearerToken`, `BearerTokenFile`, `BasicAuth`,
`EnvBearerToken`, `HTTPError`, `ConfluenceVersionConflict`.

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
