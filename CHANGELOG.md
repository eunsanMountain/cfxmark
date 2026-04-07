# Changelog

All notable changes to **cfxmark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] — 2026-04-07

### Changed

- `render_cfx` now returns a third element — a ``warnings`` list —
  alongside ``(xhtml, attachments)``. Any ``::: jira`` / ``::: toc``
  (or other parameter-only directive) whose body is silently dropped
  by its handler now surfaces a human-readable warning via
  ``ConversionResult.warnings`` so callers can correct their
  Markdown instead of discovering the drop on Confluence.

## [0.1.2] — 2026-04-07

### Fixed

- Attachment enumeration now strips CDATA sections before scanning
  for `<ri:attachment>`, so a Confluence ``code`` macro documenting
  storage XML no longer leaks phantom filenames into
  `result.attachments`. `resolve_assets(mode="sidecar")` applies the
  same CDATA strip on its opaque-block fallback.
- `to_cfx` now emits only the **basename** of a local-image path in
  `<ri:attachment ri:filename="...">` (Confluence stores attachments
  in a flat per-page namespace). `result.attachments` still reports
  the caller's original path — including any directory prefix — so
  the caller knows where to read the bytes from on disk.

## [0.1.1] — 2026-04-07

### Fixed

- `ConversionResult.attachments` now enumerates **every**
  `ri:attachment` reference in the output XHTML, including those
  trapped inside Grade III opaque blocks (e.g. `<ac:image>` inside
  `<pre><code>`). Previously only Grade I/II native `<ac:image>`
  references were reported, so callers silently missed attachments
  they needed to upload. `to_md` also populates `attachments` now
  (previously always empty).
- `resolve_assets(mode="sidecar")` downloads opaque-block attachments
  into `asset_dir` as a fallback, keeping the sidecar directory a
  complete asset set regardless of how the image was preserved.
- `to_cfx` no longer crashes with `IndexError` when user-typed
  Markdown contains a literal `` `CFXMARK_OPAQUE-N-CFXMARK` `` /
  `` `CFXMARK_DIRECTIVE-N-CFXMARK` `` token whose index has no
  matching capture — the region falls back to plain inline code.

### Docs

- README custom-macro example uses a valid `AdmonitionHandler` flavour
  (`info`/`note`/`warning`/`tip`); the previous `"danger"` example
  raised `ValueError`.
- README docs/SPEC/OPAQUE/LICENSE links rewritten as absolute GitHub
  URLs so they resolve correctly when rendered on PyPI.
- `docs/SPEC.md` no longer claims `<ac:layout>` wrappers become opaque
  blocks; cfxmark flattens them transparently.

### Packaging

- `pyproject.toml` switched to PEP 639 license metadata
  (`license = "MIT"` + `license-files = ["LICENSE"]`); the redundant
  `License :: OSI Approved :: MIT License` classifier was removed.
- Added `Documentation` and `Changelog` entries to `[project.urls]`
  for the PyPI sidebar.

## [0.1.0] — 2026-04-07

### Added

#### Conversion API

- `cfxmark.to_cfx(markdown)` — Markdown → Confluence Storage Format XHTML.
- `cfxmark.to_md(xhtml)` — Confluence Storage Format XHTML → Markdown.
- Both return a `ConversionResult` carrying `xhtml` / `markdown`,
  `attachments` (local file references for the caller to upload),
  `warnings`, and the intermediate AST.

#### Native (grade I) constructs — lossless round-trip

- ATX headings `h1`–`h6`.
- Paragraphs, hard breaks, soft breaks, HTML entities.
- Inline emphasis: `**bold**`, `*italic*`, `` `code` ``, `~~strike~~`,
  links, images.
- Lists: bullet, ordered, deeply nested, mixed paragraph + nested-list
  list items.
- Block quotes, horizontal rules.
- Code fences with language tags (mapped to Confluence's `code` macro).
- GFM tables with **`colspan` / `rowspan`** support via the
  MultiMarkdown `<` / `^` continuation cell convention. Multi-paragraph
  cells flatten to inline content joined by `<br>` tags.

#### Directive (grade II) macros

Pluggable `MacroRegistry`. Default registry covers:

- `info`, `note`, `warning`, `tip` admonition panels.
- `jira` issue references (single + JQL query forms).
- `expand` collapsible sections.
- `toc` table of contents.

Each is rendered as a pandoc-style fenced div in Markdown:

```
::: info
body
:::
```

#### Opaque (grade III) preservation

The signature feature: any Confluence construct cfxmark does not know
how to represent in Markdown is preserved **byte-for-byte**, including
the `ac:macro-id` UUID that gives Confluence its macro identity.

- **Block opaque**: HTML-comment sentinel + `cfx-storage` fenced code
  block. SHA-256 fingerprint in the sentinel ID prevents accidental
  collision with user-typed content.
- **Inline opaque**: short `[label](cfx:op-XXXXXXXX)` Markdown link
  with the XML payload stored in a `cfxmark:payloads` sidecar at the
  bottom of the document. The label is auto-derived from the
  underlying element type (`@user-…`, `jira:PROJ-1`, `cfx:status`, …).
- **Header notice**: a single-line `<!-- cfxmark:notice ... -->` HTML
  comment is injected at the top of any document containing opaque or
  directive constructs, telling humans and AI agents not to delete the
  markers.

#### Image asset workflow

- `to_md` automatically tags every local-attachment image with a
  `<!-- cfxmark:asset src="..." -->` metadata marker carrying the
  original Confluence filename.
- New `cfxmark.resolve_assets(md, fetcher, mode="sidecar"|"inline")`
  function reads the markers, calls a caller-provided `fetcher` to
  download the bytes, and either saves them to a sidecar directory
  (with relative path links) or embeds them as `data:` URIs.
- Markers are preserved across resolution so the round trip back to
  CFX always recovers the original Confluence filename — even after
  the visible link target has been rewritten to a sidecar path.
- Image dimensions encoded in the URL fragment as
  `#cfxmark:w=300,h=200` for round-trip preservation.

#### Canonicalization (`canonicalize_cfx`)

A deep XML normalization pass that lets two semantically equivalent
Confluence storage fragments compare equal:

- Strips volatile attributes (`ac:macro-id`, `ac:local-id`,
  `ri:version-at-save`, `ac:schema-version`, `ac:thumbnail`,
  `ac:border`, `ac:align`).
- Strips Confluence-editor data attributes
  (`data-uuid`, `data-highlight-colour`, …).
- Removes purely cosmetic CSS (default text colour, `text-align`,
  `width` / `height` on table family elements, `font-weight`,
  `padding`, `margin`, `list-style-type`, `vertical-align`, …).
- Removes Confluence-editor class names
  (`wrapped`, `fixed-width`, `auto-cursor-target`, `code-line`,
  `has-list-bullet`, `internal-link`, `confluenceTd`, …).
- Unwraps decorative `<span>` and structural `<div>` wrappers
  (including the `content-wrapper` div Confluence emits inside table
  cells).
- Promotes header rows to `<thead>`, splits `<h*>` containing `<br/>`,
  flattens singleton paragraphs inside `<li>`, drops empty paragraphs
  and trailing breaks, normalizes `<pre><code>` to the structured
  `code` macro form, removes cosmetic code parameters
  (`linenumbers`, `theme`, `firstline`, …).

#### Security hardening

- Rejects any input containing `<!DOCTYPE>` or `<!ENTITY>` to block
  XXE and billion-laughs attacks.
- lxml parser configured with `no_network=True`, `load_dtd=False`,
  `huge_tree=False`.
- Opaque sentinels carry a SHA-256 fingerprint of their body — a user
  who types the literal sentinel sequence in their Markdown is **not**
  silently turned into an opaque block; the verification fails and
  the region falls back to plain text.

#### Tooling

- `py.typed` marker for PEP 561 consumers.
- `pyproject.toml` configured for `uv` and `hatchling`.
- mypy clean (non-strict for v0.1; strict planned for v0.2).
- ruff clean.
- 65 tests:
  - 39 unit tests (per-construct + edge cases)
  - 7 image asset tests
  - 8 security regression tests
  - 1 corpus golden-file test (skipped if no private corpus available)
  - 1 Hypothesis property-based round-trip test (100 random documents)
- Verified against 9 production Confluence pages totalling ~290 KB
  of XHTML — all round-trip with byte-identical canonical form.

### Known limitations

- **HTML comments in Markdown** are dropped with a warning, with one
  exception: cfxmark's own opaque / asset / header markers are
  preserved. Confluence does not preserve HTML comments either, so
  this matches Confluence's own behaviour.
- **`drawio`, `plantuml`** and other rich diagram macros are
  passed through as opaque blocks (preserved losslessly but not
  rendered in Markdown).
- **`MacroHandler` protocol leaks lxml**. Custom macro handlers
  currently receive and return `lxml.etree._Element` objects. A thin
  adapter is planned for v0.2.
- **`<th scope="...">`, `<td title="...">`** attributes are stripped
  during canonicalization since Markdown cannot preserve them.
