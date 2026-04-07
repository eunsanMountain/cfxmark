# Opaque preservation formats

When cfxmark encounters a Confluence construct it doesn't know how
to convert to native Markdown, it preserves the raw XML through one
of the **opaque** mechanisms documented here. The XML is captured
byte-for-byte — including the `ac:macro-id` UUID Confluence uses to
identify macro instances — and round-trips losslessly.

cfxmark uses three opaque formats. They share a common pattern: an
HTML-comment sentinel (or sentinels) wraps the payload, the
sentinel ID is a SHA-256 prefix derived from the payload itself, and
the payload survives round-trip conversion in both directions.

* **Block opaque** — block-level constructs (full macros, complex
  tables, layouts).
* **Inline opaque** — inline constructs that appear in the middle of
  a paragraph or table cell (user mentions, inline jira issues).
* **Asset markers** — local-attachment images that need a follow-up
  fetch step before they're viewable.

A fourth piece — the **header notice** — is not opaque content
itself but a documentation comment cfxmark injects at the top of any
file containing the markers above so AI agents and human readers
know not to delete them.

## Block opaque

### Wire format

````
<!-- cfxmark:opaque id="op-<8-hex-chars>" -->
```cfx-storage
<raw XML fragment>
```
<!-- /cfxmark:opaque -->
````

* The HTML-comment sentinels are the **authoritative** marker. The
  Markdown parser scans for them first and removes the entire region
  from the input before passing the document to mistletoe. The
  fenced code block inside is purely cosmetic — it makes the block
  display as a code block in any Markdown renderer (GitHub, GitLab,
  Obsidian, VS Code) so a human reader sees an unmistakable
  "don't touch this" pattern.
* The fence info string is `cfx-storage`. Any other info string
  produces an ordinary code block. A user cannot accidentally create
  a fake opaque block by typing ` ```cfx-storage ` because the
  surrounding HTML-comment sentinels would also have to be present
  **and** the SHA-256 of the body would have to match the declared
  ID — see "Identity" below.

### Identity (SHA-256 fingerprint)

The `id` attribute of the start sentinel is a deterministic short
hash derived from the raw XML body:

```
op-<first 8 hex characters of sha256(raw_xml)>
```

This guarantees:

* Two cfxmark runs over the same input produce identical output.
* An unchanged opaque block has a stable identifier across round
  trips.
* Two identical opaque fragments in the same document share an ID
  (the ID is a label, not a primary key).
* **A user typing the sentinel sequence by hand does not get
  re-interpreted as a real opaque block.** The verification fails
  (the recomputed hash will not match the user-typed `op-XXXX`) and
  the region falls back to plain Markdown text.
* **A user editing the body of an opaque block also breaks the
  hash**, again falling back to plain text. This is intended — the
  whole point of the marker is that the body is XML that the user
  should not be touching.

### `ac:macro-id` preservation

Confluence's storage format identifies each macro instance with an
`ac:macro-id` UUID. This UUID is the key to **macro identity** in
Confluence: comments, attachments, and even permissions are bound
to it. If we generated a new UUID every time we round-tripped a
macro through Markdown, Confluence would treat the result as a
brand-new macro on every push, throwing away any history.

Because the opaque block preserves the **byte-for-byte** XML
fragment (including `ac:macro-id`), the round trip is identity
preserving. After:

```python
md   = cfxmark.to_md(cfx).markdown
back = cfxmark.to_cfx(md).xhtml
```

`back` and `cfx` are equal modulo cfxmark's canonicalization, and
Confluence sees no change to the macro instances on a subsequent
push.

## Inline opaque

Inline elements that have no native Markdown form — Confluence user
mentions, inline jira issue macros, custom widget invocations — are
preserved through a different mechanism so the surrounding paragraph
text stays as native Markdown.

### Wire format

The visible reference is an ordinary Markdown link with a `cfx:`
URL scheme:

```markdown
Contact the purchaser ([@user-2c9402cc](cfx:op-4fab0f8d))
```

The XML payload lives in a `cfxmark:payloads` sidecar block at the
bottom of the same Markdown file, indexed by the same `op-XXXXXXXX`
ID:

```markdown
<!-- cfxmark:payloads -->
<!-- op-4fab0f8d
<ac:link><ri:user ri:userkey="2c9402cc83d4bcc40183d976ef730001"/></ac:link>
-->
<!-- /cfxmark:payloads -->
```

* The `[label]` part is auto-derived from the underlying element
  type:
  * `<ac:link><ri:user/>` → `@user-<first-8-of-userkey>`
  * `<ac:link><ri:page/>` → `→<page-title>`
  * `<ac:link><ri:attachment/>` → `📎<filename>`
  * `<ac:structured-macro ac:name="X">` → `cfx:X`, plus `:KEY` if
    the macro carries a `key` parameter (e.g. `jira:PROJ-1`)
  * Anything else → `cfx:<localname>`
* The `op-XXXXXXXX` ID is the same SHA-256 prefix scheme as block
  opaque, so the same anti-injection guarantees apply.
* The sidecar block sits at the very bottom of the file so the body
  of the document stays clean. Multiple inline references with the
  same payload share a single sidecar entry.

### Round-trip flow

```
cfx → md:
  <ac:link>...</ac:link>  →  [label](cfx:op-XXXX)
                              + sidecar entry op-XXXX → raw XML

md → cfx:
  Strip cfxmark:payloads sidecar
  For each [label](cfx:op-XXXX) link:
    Look up payload by ID
    Verify SHA-256 (else: treat as plain link)
    Materialize raw XML in place of the link
```

## Asset markers

Local-attachment images carry a metadata HTML comment immediately
following the link:

```markdown
![](image-3.png#cfxmark:w=700)<!-- cfxmark:asset src="image-3.png" -->
```

* `src` is the **original** Confluence attachment filename. It is
  preserved verbatim across round trips so `to_cfx` always emits
  `<ri:attachment ri:filename="image-3.png"/>` regardless of what
  the visible link target currently points at.
* The marker is stripped before mistletoe sees the document, so it
  has no effect on parsing.
* `cfxmark.resolve_assets(md, fetcher, mode=...)` reads the markers,
  calls `fetcher` to download bytes, and either saves them to a
  sidecar directory (`mode="sidecar"`) or embeds them as a `data:`
  URI (`mode="inline"`). The marker is preserved through both
  modes, so the operation is idempotent.

External-URL images do **not** carry a marker — they are already
resolvable by any Markdown viewer.

## Header notice

When cfxmark emits a Markdown document containing any opaque or
directive marker, it prepends a single-line HTML comment at the top:

```markdown
<!-- cfxmark:notice Converted from Confluence storage format. Inline
[label](cfx:op-XXXXXXXX) references preserve Confluence content that
has no native Markdown form; the raw XML for each lives in the
cfxmark:payloads sidecar at the bottom of this file. Do not edit
those references or the sidecar — tampering invalidates a SHA-256
fingerprint and the round trip falls back to plain text. -->
```

The notice is invisible in any Markdown viewer (it's an HTML
comment) but readable by any human or AI agent inspecting the raw
file. It serves as a stable contract: a tool that respects the
notice will not silently destroy the markers below it.

The notice is stripped on `to_md` parse, so it does not need to be
present in user-edited Markdown.

## Caller responsibilities

* **Never edit the contents of an opaque marker by hand.** The
  fenced code block, the inline `cfx:op-...` link, the asset
  marker's `src`, and the payload sidecar are all SHA-256 verified.
  Editing them invalidates the round trip and the affected region
  falls back to plain text.
* **Deleting an opaque marker is the intended way to remove the
  underlying Confluence construct.** A subsequent `to_cfx` will
  emit XHTML without the macro / image / inline reference, and a
  push to Confluence will remove it from the page.
* **Adding new content next to an opaque marker is fine** — just
  put it outside the sentinels.
