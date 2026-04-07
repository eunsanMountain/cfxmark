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
```

`ConversionResult` is the same dataclass for both directions —
`xhtml` is populated for `to_cfx`, `markdown` for `to_md`.

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

```bash
# With uv (recommended):
uv add cfxmark

# With pip:
pip install cfxmark
```

cfxmark depends on `lxml` and `mistletoe`. Python 3.10+.

## The contract

cfxmark grades every Confluence construct into one of three buckets:

| Grade | Description | Behaviour |
|---|---|---|
| **I — Native** | Standard CommonMark / GFM (headings, lists, tables, code fences, links, images, blockquote, hr, inline emphasis) | Lossless round-trip after canonicalization. |
| **II — Directive** | Confluence macros with a known Markdown directive mapping (`info`, `note`, `warning`, `tip`, `jira`, `expand`, `toc`) | Lossless after canonicalization. Pluggable via `MacroRegistry`. |
| **III — Opaque** | Everything else | Captured byte-for-byte through cfxmark's opaque-block / inline-opaque mechanism. **Never dropped, never rewritten.** |

See [`docs/SPEC.md`](docs/SPEC.md) for the full mapping table and
[`docs/OPAQUE.md`](docs/OPAQUE.md) for the opaque-block format.

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
my_registry.register(AdmonitionHandler("danger"))  # treat ac:name="danger" as info-style

result = cfxmark.to_md(xhtml, macros=my_registry)
```

Implementing a `MacroHandler` from scratch requires a small amount
of lxml knowledge — see `cfxmark/macros/builtins/admonition.py` for
a complete example. A higher-level handler API that hides lxml is
planned for v0.2.

### Canonicalization helpers

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

MIT. See [`LICENSE`](LICENSE).
