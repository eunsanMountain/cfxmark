# Changelog

All notable changes to **cfxmark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] — 2026-07-24

### Known limitations
- A non-1 `<ol start="N">` nested list that follows inline list-item text is an
  **unrepresentable shape**: Markdown cannot express it (CommonMark forbids a
  non-1 ordered list from interrupting a paragraph), so managed conversion
  **fails closed** and names it with the `nested-ordered-list-start-unrepresentable`
  diagnostic. It is **not** auto-migrated and cannot be resolved by a Markdown
  push. To publish the page, rewrite the list as a flat structure in the remote
  (Confluence web UI or an explicit storage-format workflow), then re-pull.
- A storage change that cannot be attributed to a user edit or a named
  migration occurrence fails closed before any write:
  `multiple-change-owners` when more than one occurrence could equally own it,
  `unclassified-storage-change` when none does.

### Added
- Collateral round-trip occurrence codes, each backed by a storage witness so
  the ownership proof can attribute the change: `element-attribute-omitted`,
  `element-attribute-normalized`, `table-colgroup-omitted`,
  `table-cell-paragraph-unwrapped`, `table-cell-paragraphs-collapsed`, and
  `body-only-table-header-synthesized`. All are consent-gated.
- Ordered-composition witness: composite migration effects are attributed to
  their contributing occurrences; a composition that does not fully explain a
  change grants no ownership.
- Body-less structured macros (e.g. `<ac:structured-macro ac:name="toc"/>`
  inside a heading) round-trip byte-exactly through the opaque payload
  transport, preserving macro identity and parameters.

### Fixed
- A bare, user-authored HTML comment (`<!-- ... -->`) in Markdown no longer
  **fatals** the managed Markdown→storage update. Previously the parser dropped
  the comment with a generic `"unmatched inline HTML span dropped"` warning that
  fell through to the `conversion-note` fallback and resolved to a **fatal**
  effect, blocking the whole page. The parser now recognises a genuine comment by
  its `<!--`/`-->` syntax and emits a distinct, value-free `"HTML comment dropped"`
  producer message, classified as the new `html-comment-dropped` code with the
  **`removed`** effect — a describable, consent-required loss (the push-side mirror
  of the pull-side `xml-comment-dropped`). Like every source loss it is a blocking
  content_loss, so `push_safe` is `False` and the page remains publishable through
  informed consent (not a silent drop); the comment is absent from the candidate
  storage.
  Processing instructions (`<? ?>`), CDATA (`<![CDATA[ ]]>`) and declarations
  (`<!DOCTYPE>`) share the same fall-through but are **not** comments, so they keep
  their existing `"unmatched inline HTML span dropped"` message and path. Content
  HTML (`<div>`, unknown `<span>` attributes) still classifies as
  `html-tag-stripped`, and the `cfxmark:img` functional comment is unaffected
  (consumed in pre-processing).

### Added
- `ConversionError` now carries an optional, value-free `reason_code` attribute
  and cfxmark exports a closed public allowlist
  `PUBLIC_CONVERSION_REASON_CODES` (currently `{"semantic-mapping-ambiguous"}`).
  When a coded cfxmark failure is wrapped into a `ConversionError`, the wrap site
  sets `reason_code` to the wrapped error's code only if it is in the allowlist,
  so a consumer can distinguish a semantic-mapping-ambiguous failure from any
  other conversion error via a stable machine code — never by parsing the
  message string. Any code outside the allowlist yields `reason_code is None`.

- Added managed Markdown artifacts with typed diagnostics, preservation
  signatures, bidirectional storage/source maps, edit-operation provenance,
  and fail-closed ambiguity reporting.
- Added fresh-remote subtree splice artifacts so unchanged or uniquely moved
  blocks retain remote structure and attributes while edited sibling blocks are
  rendered from Markdown.
- Added marker-free protected-region projections for common Confluence links,
  dates, task lists, cell lists, nested structures, and opaque macros, including
  typed Markdown ranges and status-only task change evidence.
- Added deterministic image metadata comments using a validated, space-delimited
  `k=v` grammar and canonical `<br>` hard-break rendering.
- Added typed table-cell background presentation artifacts with explicit,
  server-readback-derived render capabilities and nullable support for valid
  but unverified values.
- Added stable table matching that preserves presentation through unambiguous
  table reordering and cell-text edits while rejecting duplicate identities.

### Fixed
- Bound managed candidates to the exact conversion options and table
  presentation authority used to render them, and replayed colored-cell source
  maps through both public validators against fresh source storage.
- Preserved inter-element whitespace tails for unchanged remote subtrees when
  an unrelated block is appended, avoiding storage churn outside the intended
  Markdown edit.
- Normalized invalid public text inputs to typed cfxmark errors instead of
  leaking raw Unicode or XML parser exceptions.
- Preserved code-macro identity, headerless-table topology, mixed alignment,
  exact remote table-cell presentation, and unsupported remote subtrees during
  unrelated Markdown edits while rejecting ambiguous correspondence.
- Preserved escaped commas and pipes in Jira image targets and alternative
  text without corrupting parser or renderer boundaries.
- Rejected oversized, amplification-prone, or overlapping table spans before
  logical-grid allocation, preserving the source table opaquely with a blocking
  diagnostic while keeping valid inherited-rowspan alignment fixed-point.
- Restricted leading-pipe Markdown table detection to structurally valid GFM
  tables so prose and inline-code pipes remain ordinary paragraphs.
- Kept endpoint whitespace, empty paragraphs, and list-item paragraph wrappers
  visible to preservation diagnostics instead of hiding collateral loss during
  canonicalization.
- Classified every unsupported block or inline Confluence construct as an
  occurrence-scoped managed-pull blocker instead of reporting the artifact as
  ready to edit.
- Preserved empty link labels without synthesizing visible text, and made
  managed artifacts fail closed whenever their generated Markdown would cause
  a storage change before any user edit.
- Added an occurrence-bound storage migration resolver so an approved emoticon
  conversion cannot authorize unrelated renderer normalization.
- Reported residual round-trip drift alongside approvable migration diagnostics
  during pull, including the first changed storage semantic path.
- Prevented root-level XML comments and processing instructions from entering
  a crashing standalone C14N path, while preserving unapproved comments,
  processing instructions, fragment text, and tails during exact migrations.
- Bound candidate storage bytes to their source-map provenance and separated
  direct AST edits from serializer-ancestor topology changes so callers can
  reject same-node collateral storage leaves before publishing.

### Removed
- Removed the proofless `cfxmark.confluence` REST writer and its namespace-only
  extra. cfxmark now enforces its converter-only boundary; transport callers
  must publish source-bound managed candidates after fresh-source validation.

## [0.4.1] — 2026-07-14

### Added
- Added explicit `legacy`, `editable`, and `readable` Markdown profiles with
  `push_safe` and `losses` diagnostics.
- Editable Markdown now carries compact semantic color categories, paragraph
  alignment, and image size, thumbnail, and alignment metadata.
- Added independent round-trip coverage for NBSP normalization, `colspan`, and
  `rowspan`, including spans in non-leading table columns.
- Added readable, round-trip-safe Markdown forms for inline Jira macros and
  paragraph-only table-of-contents macros.

### Fixed
- Flattened redundant strong markup so nested or adjacent `<strong>` elements
  no longer produce ambiguous `****` delimiters.
- Made boundary NBSP encoding and merged-table reconstruction fixed-point
  stable across repeated conversions.
- Kept header-cell row spans in a valid GFM table and preserved paragraph
  alignment when the paragraph contains hard breaks.
- Preserved simultaneous foreground and background colors. Unknown exact
  colors now round-trip with a diagnostic instead of being silently removed.
- Made unsupported-content markers fixed-point stable, include the macro name
  when available, and refuse unsafe Markdown-to-storage conversion.
- Preserved image metadata when attachment links are rewritten to sidecars.
- Treated Confluence's structural `content-wrapper` div in table cells as
  transparent while leaving styled divs and nested tables unsupported.

### Changed
- Editable and readable profiles omit raw opaque XML. Unsupported content is
  represented by a local marker and makes editable output unsafe to publish.
- Legacy profile behavior remains the public API default for compatibility.

## [0.4.0] — 2026-04-13

### Added
- **Table → Jira wiki rendering** — `to_jira_wiki()` now renders
  `Table` / `TableRow` / `TableCell` AST nodes as Jira wiki table
  syntax (`||header||` / `|data|`). `colspan` / `rowspan` emit a
  warning (Jira wiki does not support them). Empty cells are padded
  with a space to prevent `||` from being mis-parsed as a header
  delimiter on re-read.
- **BlockQuote → `{quote}` rendering** — `to_jira_wiki()` now renders
  `BlockQuote` nodes as `{quote}…{quote}` blocks. Nested blockquotes
  are flattened with a warning (Jira wiki does not support `{quote}`
  nesting).
- **HardBreak → `\\` rendering** — `to_jira_wiki()` now renders
  `HardBreak` nodes as `\\` (Jira wiki forced line break). Inside
  table cells the trailing newline is suppressed so the row stays on
  one line.
- **HardBreak parsing** — `from_jira_wiki()` now recognises `\\` as
  a `HardBreak` AST node instead of treating it as an escaped
  backslash. An optional newline immediately after `\\` is consumed.
- **`Subscript`**, **`Superscript`**, **`Underline`** AST nodes and
  full parser / renderer support. `from_jira_wiki()` now parses
  `~sub~`, `^sup^`, `+ins+` into dedicated AST nodes (previously
  the markers were dropped with a warning). The Jira wiki renderer
  emits the native markers; the Markdown renderer emits `<sub>`,
  `<sup>`, `<ins>` HTML tags; the Confluence renderer emits matching
  XHTML elements.
- **`ColorSpan`** AST node — `from_jira_wiki()` now preserves
  `{color:#hex}text{color}` as a `ColorSpan(color, children)` node
  instead of dropping the colour with a warning. The Jira wiki
  renderer restores `{color:value}…{color}`; the Markdown renderer
  emits `<span style="color:value">…</span>`.
- **`Citation`** AST node and `??text??` parsing —
  `from_jira_wiki()` now recognises `??cited text??` as a `Citation`
  node. The Jira wiki renderer emits `??…??`; the Markdown renderer
  emits `<cite>…</cite>`.
- **`code_language_map`** option on `to_jira_wiki()` and
  `render_jira_wiki()` — optional `dict[str, str]` that normalises
  code block language identifiers before rendering (e.g.
  `{"ts": "javascript", "kotlin": "java"}`). `None` (default)
  preserves the original language tag unchanged.

### Changed
- **`_EMPHASIS_NODE` mapping** in the Jira wiki parser now maps `~`,
  `+`, `^` to `Subscript`, `Underline`, `Superscript` respectively
  (previously `None`, which caused marker-drop with warning).
- **`InlineNode` union** expanded with `Subscript`, `Superscript`,
  `Underline`, `ColorSpan`, `Citation`.
- Drop-count constants `_DROP_TABLE`, `_DROP_BLOCKQUOTE`,
  `_DROP_HARD_BREAK` removed from the Jira wiki renderer — these
  constructs are now rendered instead of dropped.

### Fixed
- **Table cell empty-content ambiguity** — empty data cells now
  render as `| |` (with a space) so adjacent delimiters are not
  mis-parsed as header openers on re-read. Fixes round-trip
  instability for tables with sparse cells.
- **HardBreak in table cells** — `\\\n` inside a cell is collapsed
  to `\\` (no newline) so the table row stays on a single line.

## [0.3.0] — 2026-04-08

### Added
- **`cfxmark.from_jira_wiki()`** — Jira wiki markup → Markdown parser
  (experimental, lossy). Round-trips real Jira issue descriptions to
  canonical Markdown and reaches a fixed point after at most two
  `wiki → md → wiki` iterations. Supported constructs: `h1.`–`h6.`
  headings, bullet / ordered / dash lists with nested marker chains
  (`*`, `**`, `*#`, …), tables with multi-line cells and escaped
  pipes, paired macros (`{code}`, `{noformat}`, `{quote}`, `{info}`,
  `{note}`, `{warning}`, `{tip}`, `{panel}`), `bq.` blockquote,
  `----` horizontal rule, `*bold*`, `_italic_`, `-strike-`,
  `{{mono}}`, labelled and bare URL links, `[^file]` attachment
  links (image extensions map to `Image`, everything else maps to
  `Link` with an `attachment:` URL), `!file.ext!` images, and
  `{color:#hex}…{color}` inline color (content kept, emphasis
  dropped with a warning). Boundary-aware emphasis parsing handles
  real-world Korean text like `8월~9월`, `~9월`, and `2~3` as plain
  text per Jira's actual rendering rules.
- **`cfxmark.jira`** submodule as the stable import site for the
  Jira-flavoured converters: `from cfxmark.jira import from_jira_wiki,
  to_jira_wiki`. Keeps the experimental/lossy Jira contract
  visually separated from the lossless Confluence round-trip.
- **`ConversionOptions.passthrough_html_comment_prefixes`** (R1) —
  opt-in list of prefixes identifying caller-owned HTML comment
  blocks like `<!-- workflow:meta … -->`. Matching comments are
  captured as `PassthroughComment` AST nodes during `parse_md`,
  emitted verbatim by `render_md`, and silently dropped by
  `to_cfx` / `to_jira_wiki` so local metadata never leaks to
  Confluence or Jira. `cfxmark:` prefixes are filtered out so
  cfxmark's own sentinel comments cannot be hijacked.
- **`cfxmark.strip_passthrough_comments(source, prefixes)`** — helper
  for the wrapper's canonical-compare workflow. Removes matching
  passthrough comment blocks (including their owned trailing blank
  line) so two Markdown documents can be diffed modulo local
  metadata.
- **`to_jira_wiki(heading_promotion=...)`** — new keyword that
  selects between `"confluence"` (default, the historical H3 → `h2`
  collapse for pages whose title occupies the top slot), `"jira"`
  (1:1 identity mapping for Jira issue descriptions, whose titles
  live in a separate field), and `"none"` (alias for `"jira"`).
- **`to_cfx(options=...)`** — now accepts `ConversionOptions` so the
  `passthrough_html_comment_prefixes` opt-in can be plumbed through
  the Markdown → Confluence direction as well.
- **`ConfluenceClient.push_markdown(options=...)`** and
  **`ConfluenceClient.pull_markdown(options=...)`** — the high-level
  Confluence push / pull helpers now accept `ConversionOptions`. This
  is the only way a wrapper can actually *use* the R1 passthrough
  feature end-to-end; without it, the `push_markdown` entry point
  silently ignored any caller-owned metadata comment policy. On
  `pull_markdown` the options mainly toggle the Markdown bullet
  marker and code fence style (`passthrough_html_comment_prefixes`
  is a no-op on that direction because Confluence storage XHTML has
  no HTML comment syntax for cfxmark to preserve).
- **`PassthroughComment`** AST node in `cfxmark.ast` (Markdown-side
  only; never serialised to CFX or Jira wiki output).
- 6 Jira wiki fixtures in `tests/fixtures/jira_wiki/` covering the
  parser's full feature surface (basic prose, sectioned content with
  boundary-aware tildes, nested lists with dash / asterisk markers,
  multi-line table cells with escaped pipes, tables immediately
  adjacent to headings, inline images with italic + empty heading).
- 81 new unit tests in `tests/unit/test_jira_wiki_parser.py` pinning
  every supported construct, every fixture's fixed-point
  convergence, and every known adversarial case (nested link
  labels, multi-line cells, heading directly after a table,
  boundary-aware emphasis with non-ASCII and version-range text).

### Changed
- **`jira_wiki` renderer now escapes `~`, `+`, `^`, and `]`** in
  addition to the previous set. `~` / `+` / `^` are escaped so that
  plain text with version ranges (`v1.0~v2.0`), ASCII-art (`x^2`),
  or non-ASCII runs does not round-trip into an accidental subscript
  / underline / superscript when re-parsed by a boundary-aware Jira
  wiki reader. `]` is escaped so that a Markdown link label whose
  text contains a literal `]` (e.g. `[prefix [nested] rest](url)`)
  survives the Jira link recogniser's bracket balancing — without
  this the trailing `]` would prematurely close the emitted Jira
  link and break the round-trip. Nested-bracket labels now converge
  in two passes instead of three as a direct consequence.

### Fixed
- `parsers/md.py` now handles CommonMark HTML comment blocks via a
  dedicated pre-processing pass instead of relying on mistletoe,
  which does not emit `HtmlBlock` tokens for multi-line HTML
  comments. Previously a multi-line `<!-- … -->` ended up in the
  inline HTML fallback path and was dropped with a warning even
  when the caller had opted into `passthrough_html_comment_prefixes`.
- `parsers/jira_wiki.py` now recognises a bare list marker (``-``,
  ``*``, ``#`` with no trailing content) as an empty list item.
  Previously the parser treated a single ``-`` at line start as
  plain text, which diverged from mistletoe's CommonMark
  interpretation on the Markdown side and caused a document whose
  last line is a bare ``-`` placeholder (a common "TODO" marker in
  real issue bodies) to oscillate between ``-`` (mistletoe empty
  list) and ``\\*`` (escaped plain text) on every round trip.
  Aligning the two parsers closes the oscillation and keeps the
  "at most two passes" convergence guarantee for the whole corpus.

### Docs
- New "Jira wiki (experimental, lossy)" section in `README.md` with
  the full contract, lossy mapping table, heading-promotion guide,
  and HTML comment passthrough usage example.

## [0.2.0] — 2026-05

### Breaking
- `resolve_assets` now validates attachment filenames by default
  (`strict_filenames=True`). Unsafe names (parent traversal, absolute
  paths, Windows path separators, null bytes, symlink escapes) raise
  `AssetSecurityError`. Pass `strict_filenames=False` to preserve the
  exact 0.1.x behavior.

### Security
- Path traversal defense baked into `resolve_assets` by default. The
  validation fires BEFORE the caller's `fetcher` callback is invoked,
  so a malicious filename never triggers a network request.
- `ConfluenceClient.push_markdown` now validates every attachment
  filename in `cfx_result.attachments` before any HTTP traffic is
  issued. A malicious markdown like `![](../../../etc/passwd)` is
  rejected with `AssetSecurityError` *before* `get_page` runs, so a
  hostile markdown source cannot exfiltrate arbitrary host files
  through the upload loop. Pass `strict_filenames=False` to opt out.
- `ConfluenceClient.attachment_fetcher` now refuses off-host download
  URLs. If a remote `_links.download` is absolute and its netloc does
  not match `self._host`, the fetcher logs a warning and returns
  `None` instead of forwarding the caller's bearer/basic credential
  to an attacker-controlled host. Defense in depth against
  compromised proxies / SSRF.
- `ConfluenceClient.push_markdown` is now content-aware: a same-named
  remote attachment is only treated as up-to-date when its
  `extensions.fileSize` matches the local file. Size mismatch (or a
  missing remote fileSize) forces a re-upload so Confluence creates a
  new version. Closes a silent-staleness bug where edited local images
  were skipped because their basename already existed on the page.
  Future enhancement: hash-based dedup (plan §7).
- `ConfluenceClient.attachment_fetcher` now catches raw `OSError`
  (Python 3.11+ `TimeoutError` / `RemoteDisconnected` from `urlopen`),
  matching the `_request` helper. A flaky post-handshake disconnect
  no longer aborts the entire `pull_markdown` call.

### Added
- `cfxmark.AssetSecurityError` exception class.
- `cfxmark.to_jira_wiki()` — new Jira wiki markup renderer alongside
  Markdown and Confluence XHTML. Supports section slicing and
  caller-controlled leading-notice drop patterns.
- `ConversionResult.jira_wiki` field (appended — positional-arg compat preserved).
- `cfxmark.confluence` module — optional urllib-based Confluence REST
  client. Stdlib-only, zero runtime deps. Provides `ConfluenceClient`,
  `PushResult`, `PullResult`, `HTTPError`, `ConfluenceVersionConflict`,
  `BearerToken`, `BearerTokenFile`, `BasicAuth`, `EnvBearerToken`.
- `cfxmark[confluence]` optional-dependency extra (namespace-only).

### Changed
- `Development Status` classifier bumped from Alpha to Beta.

### Migration from 0.1.x

- `resolve_assets` now validates filenames by default. If you relied on
  no-validation (e.g. sidecar writes outside `asset_dir`), pass
  `strict_filenames=False` and fix your caller.
- `ConversionResult` gained a `jira_wiki` field. It is appended to the
  dataclass, so positional-arg construction of `ConversionResult`
  continues to work. Attribute access on `xhtml` / `markdown` /
  `attachments` / `warnings` / `document` is unaffected.
- cfxmark is now silent by default. Progress output, upload traces,
  and security rejections log to `logging.getLogger("cfxmark")` —
  enable with `logging.getLogger("cfxmark").setLevel("INFO")`.
- No other runtime behavior changed. Core API (`to_cfx`, `to_md`,
  `canonicalize_cfx`, `normalize_md`) is identical.

## [0.1.4] — 2026-04-08

### Tests

- Added explicit CJK boundary regression tests covering the Strong /
  Emphasis / Strikethrough HTML fallback path. When a delimiter run is
  flanked by word characters on both sides (for example Korean glyphs
  adjacent to `**bold**`), CommonMark's flanking-delimiter rule
  prevents the asterisks from re-parsing as emphasis, so cfxmark emits
  raw `<strong>...</strong>` / `<em>...</em>` / `<del>...</del>` HTML
  to preserve the round trip. The policy was already correct in the
  renderer — these tests pin the behavior so future refactors cannot
  silently flip it.
- Added renderer determinism guardrails: repeated `to_md` calls on the
  same input, and the `to_md → to_cfx → to_md` convergence invariant,
  are now asserted explicitly for CJK-heavy inputs.
- Extended the Hypothesis property test (`test_round_trip.py`) with a
  CJK-inclusive corpus (Hangul Syllables + CJK Unified Ideographs)
  and inline emphasis runs (`**bold**`, `*italic*`, `~~strike~~`).
  The original v0.1.0 strategy was ASCII-only and never exercised the
  CJK word-boundary branch of the renderer. The new strategy produces
  100 randomized documents per run and still converges in a single
  pipeline pass.

### Docs

- `README.md` now documents `cfxmark.normalize_md(source)` as the
  pre-push canonicalization recipe. The function was already exported
  from `cfxmark` in v0.1.0 but was not discoverable from the README.
  Applying it to hand-edited Markdown before calling `to_cfx` converges
  any drift to the parse→render fixpoint and avoids the "local file
  drifted from the round-trip form" failure mode.
- The canonicalization section now presents `canonicalize_cfx` and
  `normalize_md` as a matched pair with worked examples.

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
- Verified against a corpus of 9 substantial Confluence pages totalling
  ~290 KB of XHTML — all round-trip with byte-identical canonical form.

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
