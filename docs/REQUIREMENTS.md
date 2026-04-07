# cfxmark requirements

## Goal

A bidirectional, lossless converter between **CommonMark/GFM Markdown**
and **Confluence Storage Format XHTML**, suitable for use as the
text-conversion layer of any tool that synchronizes a Markdown file
with a Confluence page.

The library does **not** know about the Confluence REST API, does not
upload attachments, and does not authenticate. It is text-in /
text-out only. The caller is expected to wrap network I/O around it.
For image attachments, cfxmark provides a `resolve_assets` helper
that lets the caller plug in a fetcher callback without having to
parse Markdown themselves.

## Why a new library?

Two existing projects ([`md2cf`][md2cf] and [`md2conf`][md2conf])
inspired the design but neither suits the workflow:

* Both are **forward-only** (Markdown → Confluence).
* Neither preserves unknown Confluence macros — anything they don't
  understand is silently dropped.
* `md2conf` carries six runtime dependencies (`python-markdown`,
  `lxml`, `pymdownx.*`, …) and over 8000 lines of code.

cfxmark fills the bidirectional and preservation gaps with a leaner
runtime (`lxml` + `mistletoe`) and a clear contract for what does and
doesn't round-trip.

[md2cf]: https://github.com/iamjackg/md2cf
[md2conf]: https://github.com/hunyadi/md2conf

## Design principles

1. **Pure text-in / text-out.**
   Both `to_cfx(md)` and `to_md(cfx)` operate on strings. No file
   I/O, no HTTP, no global state.

2. **Round-trip stability inside a documented subset.**
   For every construct in the supported subset, the canonical form
   converges after exactly one normalization pass. The contract is
   captured in `docs/SPEC.md`.

3. **Opaque preservation outside the subset.**
   Confluence content cfxmark does not understand is captured
   verbatim into a marker block in Markdown and emitted byte-for-byte
   on the way back to Confluence. Crucially, `ac:macro-id`
   attributes are preserved, so Confluence treats the macro as the
   same instance after a round trip — comments, attachments, and
   permissions stay attached.

4. **No silent drops.**
   Anything that cannot be represented exactly is either preserved
   via the opaque mechanism or surfaced through the `warnings` list
   on `ConversionResult`.

5. **Caller owns attachments.**
   When converting Markdown that references local image files,
   `to_cfx` emits the image references as `<ri:attachment>` and
   returns the file paths in `result.attachments`. The caller is
   responsible for actually uploading them via the Confluence REST
   API.

## Three grades of construct

| Grade | Description | Round-trip behaviour |
|---|---|---|
| **I — Native** | Standard CommonMark / GFM (headings, lists, tables, code fences, links, images, blockquote, hr, inline emphasis) | Lossless after canonicalization. |
| **II — Directive** | Confluence macros for which cfxmark ships a Markdown directive mapping (`info`, `note`, `warning`, `tip`, `jira`, `expand`, `toc`) | Lossless after canonicalization. |
| **III — Opaque** | Anything else (`drawio`, `plantuml`, custom plugins, …) | Wrapped in an opaque sentinel block in Markdown and emitted as raw XML on the way back. Bytes preserved including `ac:macro-id`. |

The default macro registry covers Grade II out of the box. Callers
can extend it by registering custom :class:`MacroHandler`
implementations to promote a macro from Grade III to Grade II.

## Out of scope

* Confluence REST API integration (auth, page CRUD, attachment
  upload). Other libraries handle this.
* Image rendering (Mermaid → SVG, PlantUML → PNG, …). Mermaid is
  carried as a code fence; the rest are opaque-preserved.
* HTML comments inside Markdown — Confluence drops these on save, so
  we drop them on conversion (with a warning) rather than introduce
  a non-idempotent round trip.

## Acceptance criteria for v0.1

* `to_cfx` and `to_md` are public, fully typed, and documented.
* Real Confluence pages round-trip canonically with zero byte
  differences after cfxmark's `canonicalize_cfx` normalization.
* Property-based tests over a random subset of CommonMark documents
  pass at least 100 examples.
* The default macro registry covers `info`, `note`, `warning`,
  `tip`, `jira`, `expand`, `toc`.
* Opaque preservation survives `cfx → md → cfx` byte-for-byte
  (including the `ac:macro-id` UUID).
* Inline opaque references survive `cfx → md → cfx` via the
  `cfxmark:payloads` sidecar mechanism.
* Local-attachment images carry an `<!-- cfxmark:asset -->` marker
  that `resolve_assets` can use to fetch and embed bytes without
  losing the original Confluence filename.
