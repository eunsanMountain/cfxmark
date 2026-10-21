# Changelog

All notable changes to **cfxmark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Text after a `<div>` in a table cell was dropped.** `<td>before<div>inside</div>after</td>`
  converted to `| before<br>inside |`: when a cell's editor `<div>` wrapper was unwrapped, its
  tail was discarded, and so was its own leading text whenever it also held elements
  (`<div>a<strong>b</strong></div>` lost `a`), and text after an XML comment inside the div
  was lost without a loss being recorded. All of it is kept now. A div is treated as a block,
  as Confluence renders it: what comes before, inside and after it lands on separate lines
  (`before<br>inside<br>after`) (#1).

## [0.6.1] - 2026-08-02

Three changes to what the managed publish will and will not prove. An ordinary edit that
used to be refused now publishes, and nothing that was refused for a real reason became
permitted.

### Added

- **An explicit full-replacement contract.** Until now a deliberate rewrite of a page had
  no supported path: the ownership proof asks "which stored content does this replace",
  and a section-for-section rewrite cannot answer it. The proof is unchanged and still
  refuses. `build_replacement_manifest()` is a separate, opt-in route beside it, binding
  the final candidate's storage hash and a value-safe disclosure of the macro identities
  the replacement drops. Raw macro UUIDs affect only the hash; they never appear in the
  returned manifest, which reports kind, semantic path, ordinal and count.

### Fixed

- **A tight list item lost its edits.** Adding a bold, emphasised, struck or linked span
  inside a list item written without a `<p>` wrapper was refused as
  `unclassified-storage-change`. The item's topology leaf had no source-map entry at all,
  so nothing could own the change; the `<p>`-wrapped form worked because its paragraph
  carried one. The leaf is now attributed to the operation that actually changed the
  item's inline children, and only where the item corresponds uniquely, has not moved,
  and holds no macro, task state or preserved payload.

- **A page gained a table and lost its other tables' formatting.** Presentation was
  matched between the remote and candidate table sets as if they were the same set, so
  adding a table made every unchanged one ambiguous. Remote tables are now matched by
  their own identity; a new table inherits nothing, and a remote table that was
  rewritten, deleted or duplicated is still refused.

## [0.6.0] — 2026-08-01

Correspondence a tie did not touch, a table parser that reads what it wrote, a
preflight that finishes — and an empty paragraph that comes back the way it left.

> **Upgrading: pulled Markdown changes shape for one construct.** A paragraph
> stored as a bare `<p/>` now projects as `<!-- cfxmark:blank -->` instead of
> `<br>`. Any managed file pulled with 0.5.1 that contains an empty paragraph
> will not match a fresh pull, so its content hash moves and it needs re-pulling
> before its next publish. `<p><br/></p>` pages are unaffected — that form still
> projects as `<br>` and is still the default on the way back.

### Known limitations

- **A list marker on a *nested* list is not read back.** The renderer emits it as the
  nested list's first line, directly under its parent item, where CommonMark folds it
  into that paragraph as a lazy continuation; it is then dropped as an ordinary comment
  and the item's `<p>` wrapper is lost. A blank line before the marker would fix the
  emission.

  **Fail-closed, not silent**: dropping the comment reports `html-comment-dropped`,
  which is blocking, so `push_safe` is `False` and the managed path refuses rather than
  publishing the changed shape. Top-level and blockquote lists are unaffected.

### Added

- **Two empty-paragraph forms, two spellings.** Confluence stores `<p/>` and
  `<p><br/></p>` and renders them at different heights; Markdown had one token
  for both, so the round trip collapsed them and picked `<p><br/></p>` for
  either. The effect was not local: publishing an edit made anywhere on a page
  retuned the spacing of every untouched `<p/>` on it, not only the region the
  author edited. `<!-- cfxmark:blank -->` names the bare form, so both
  now round-trip to themselves with **no diagnostic and nothing to consent to**.

  `<p><br/></p>` stays the default deliberately: it is the form the platform's
  own editor converges to, and flipping the default would have moved the view on
  every browser-edited page instead. The marker is per occurrence, not one
  document-level list of positions, so inserting or deleting a blank line
  elsewhere cannot slide the restoration onto a different paragraph. It is
  control syntax only as its own block — inside prose it stays an ordinary HTML
  comment, and inside a fence it stays literal text.

- **`OwnershipProofSummary.all_identities`** — every fatal leaf identity,
  uncapped, with `top_identities` guaranteed to be its first page. Both are the
  same value-free `(path, field, attribute, ordinal)` projection, so the ten-item
  cap was never a safety boundary; it is a display budget for an error message.
  A caller diagnosing *why* a corpus fails needs the whole set, and reading the
  capped list silently turns "these documents share a cause" into "share it among
  the first ten" — which is how one real grouping was reported wrong.

- **A list may hold wrapped and bare items and survive saying so.** Storage
  distinguishes `<li>a</li>` from `<li><p>a</p></li>` and CommonMark cannot spell
  a list where the two are mixed, so the shape was lost and items nobody edited
  came back rewrapped. A `<!-- cfxmark:list loose=… -->` marker names which items
  storage had wrapped, and a three-way list — wrapped, bare, and multi-paragraph in
  one list — round-trips exactly as well. An earlier note in this release's evidence
  called that shape a remaining gap; measured against the shipped build it is
  `markdown_lossless` with no findings, so the note was stale.

### Removed

- **The supersession-witness machinery.** It existed for exactly one situation:
  dissolving the overlap between the `empty-paragraph-dropped` migration and a
  user deleting that paragraph, and only where that overlap landed on a topology
  leaf.

  The marker removes that migration for the **bare** form only. An empty paragraph
  carrying an attribute, or one inside a table cell, still produces an
  `empty-paragraph-dropped` occurrence, because neither is in the marker's scope.

  Migration/user overlaps also still arise from other codes — deleting three table
  rows at once produces four topology overlaps and refuses with
  `migration-user-overlap-ambiguous`. That is not a change: run against 0.5.1 and
  0.6.0 side by side, the occurrence codes, `push_safe`, blocking code, overlap
  identities and refusal message are identical. Those overlaps were never dissolved
  by a witness on either version; they were always fail-closed. The census that
  justifies this removal measured witness *production*, which is the narrower thing
  the removal depended on, and `empty-paragraph-dropped` was its only source.
  Deleting a styled empty paragraph, and deleting a leading bare one, also publish
  identically on both versions.

  Removed rather than left in place because a census over all eleven codes
  `_ATOMIC_COMPOSE_ORDER` composes — public API only, with the user edit found by
  mechanical search over every single-line and contiguous two-line deletion —
  found `empty-paragraph-dropped` was its **sole** producer. The gate was a
  hardcoded one-element allowlist, and its own comment said why it had to stay
  that way: a content-bearing code there would let a genuine content conflict be
  dissolved as a positional shift. So this is a mechanism whose cause is gone, not
  one whose tests stopped running.

  What replaces it is narrower and independent. `_verify_no_hidden_migration_owners`
  rejects user attribution for any leaf fresh storage proves migration-owned, and
  refuses outright when the independent proof is missing rather than treating an
  absent verifier result as permission. A future structural migration therefore
  fails closed until its ownership contract is deliberately redesigned.

### Fixed

- **The server's own default is not a finding in either direction.** A page whose
  source omitted `ac:schema-version` was reported as losing something when
  regeneration supplied the server's default — an addition, not a loss.

- **An `equivalent` rule must now name the values it writes, not only the ones it
  covers.** `AttributeRule.when_after` was enforced only when `when_before` covered
  absence, so the registered `present "1" -> absent` rule — which is evidence that
  Confluence re-supplies its own default — also graded `"1" -> "2"` and
  `"1" -> "99-EVIL"` as `equivalent`, and therefore the page as
  `markdown_lossless`: the verdict meaning there is nothing to disclose and nobody
  to ask. `relation` said `present "1" -> absent` all along, but that field is prose
  the validator never enforced. Both directions are now required to constrain the
  written value, and the shipped rule names `absent` explicitly. This matters most
  on the read-back path, where the "after" side is whatever the server returned.

- **`platform_editor_canonicalization` is now registered with no producer.** The
  verdict was added in this release for the empty paragraph, classified on
  observed editor-save convergence rather than on appearance. The marker then
  removed the divergence it was there to classify, so the rule and
  `canonicalization_sites()` remain and no finding reaches them. Kept rather than
  deleted: the scope predicate is the thing that identifies these nodes, and the
  next platform canonicalization will want it. Recorded here so a reader does not
  mistake silence for absence.

### Fixed

- **A dropped user mention is a loss, not a warning.** `[~alice] please review`
  converts to ` please review` — the mention is removed outright. It was
  reported through the warning channel, so a caller asking whether the body was
  complete was told yes while a person's name had gone from it. `losses` now
  carries it and the answer changes. Smart links and attachment references were
  measured at the same time and round-trip exactly, so they are not refused.

- **A code span now closes where CommonMark closes it.** Backslash escapes do
  not work inside code spans, but the table pipe-ambiguity check applied the
  text rule when looking for a span's closing delimiter. A span whose content
  ended in a backslash had its real closer skipped, and the scan ran on far
  enough to swallow the row's own cell delimiters and report them as an
  ambiguous pipe — a page that could not be published at all, with a message
  naming a pipe the Markdown did not contain.

- **An unmatched backtick no longer hides the span after it.** The scan gave up
  on the whole row, so a genuine later span was never examined.

- **A backslash run behind a backtick cannot open a span.** Parity alone does
  not decide it; mistletoe's opener also requires that the run not sit behind a
  backtick. Without this, a clean row was refused outright.

- **Macro identity survives a tie elsewhere in the document.** Identity carry ran
  inside the same guarded block as candidate generation, so an ambiguity
  anywhere raised before a single identity was applied and the retry rebuilt
  from a document that never received them. A duplicate paragraph pair was
  enough to strip the `macro-id` off every macro on the page.

- **A tie is confined to the pairs that actually differ**, not to the whole run
  of siblings it sits in. The exception carries the pairs identical across every
  minimum-cost alignment; the pairs that genuinely differ stay unclaimed.

- **The validator retry is narrowed to the call that can tie**, so the branch
  cannot be entered with variables the presentation rebuild never bound.

### Performance

- **Preflight on a large table-heavy page: 356s → 32s.** `operation_id` sorts
  and serialises an operation's tombstone arrays to hash, and the same operation
  was asked for its ID over and over — 85,070 calls across 53,629 operations on
  the page that prompted this. It is now computed at most once per operation and
  cached on the operation itself. Three `set[EditOperation]` dedups became
  ID-keyed maps for the same reason: hashing a dataclass carrying thousands of
  tombstones re-answers what the ID already answered.

  Nothing about the verdict moved: same candidate XHTML, same diagnostics, same
  fatal proof, same unclassified count. The independent validator is unchanged.


## [0.5.1] — 2026-07-28

Editable-Markdown repairs. Three classes of ordinary page become editable that
were not before, and one storage-comparison hole is closed.

**Managed files must be re-pulled.** The manifest binds the converter that
produced it, so an existing managed Markdown file fails with
`managed_converter_mismatch` until `pull-md` is run again, and pending consent
fingerprints are invalidated for the same reason. That is the designed effect of
a converter change, not a defect.

### Fixed

- **A table's identity is no longer computed from a storage-only field.** A page
  could not be edited at all when it held two tables of the same shape and at
  least one used Markdown column alignment. `_table_base_id()` hashed every cell
  field except `background`, which included `source_style` — the raw storage
  `style` attribute. A Markdown-parsed candidate can never carry that field, so
  the two sides of the boundary disagreed about a table that is in fact the same
  table, the identity match failed, the topology fallback found two equally good
  candidates, and the proof went fatal with `table-presentation-ambiguous`.
  Alignment is ordinary Markdown, so this reached ordinary documents, including
  for a no-op round trip.

  `align` — the same information in a form Markdown can express — stays in the
  identity, so tables that differ only by alignment are still told apart. Two
  tables identical apart from a *non-alignment* inline cell style now share an
  identity and are reported ambiguous; that direction refuses the edit and can
  never silently pick one.

- **Adjacent emphasis no longer injects literal asterisks.**
  `<em>A</em><em>B</em>` rendered as `*A**B*`, which reparsed to
  `<em>A**B</em>` — content corruption, not a formatting difference. Adjacent
  `Emphasis` runs are now merged the way adjacent `Strong` runs already were.

- **`**bold**` followed by a word character is no longer escaped into raw HTML.**
  The fallback guarding CommonMark's flanking rules tested only whether the
  *outside* character was alphanumeric, which is neither the rule nor what its
  own docstring described. `**bold**text` reparses correctly, so the fallback was
  unnecessary; Korean is affected on nearly every bold span because a particle
  follows the closing delimiter. The predicate now pairs each side with the
  adjacent inside character and classifies punctuation and whitespace by Unicode
  category, so Symbol boundaries (`★`, `°`, `€`, emoji) — which `isalnum()`
  misses — are handled correctly. Applies to `*` and `**` only; `~~` keeps its
  existing behavior.

- **An adjacent plain `<strong>` or `<em>` run is one semantic owner**, so a page
  carrying one is no longer stuck at `needs_migration`. Storage-source extraction
  canonicalizes an eligible run, and the parser coalesces a merged run's children
  down to one `Text` node so the tree corresponds node-for-node with a Markdown
  parse of the same document — without which remote splice does not recognize an
  untouched block and rewrites storage the user never edited. Eligibility is
  decided while the elements are still available, so an attributed or aliased
  wrapper is never absorbed after its tag information is gone.

- **Tags in different XML namespaces no longer compare equal.**
  `storage_semantically_equal()` matched on local name, so `{urn:x}strong` and
  `{urn:y}strong` collapsed into one run and an element could disappear while
  comparison still reported equality. Adjacent-run canonicalization now accepts
  namespace-free `strong`/`em` only. Inline `code` keeps its existing local-name
  matching.

### Added

- `DiagnosticSummary.table_ordinal` and `DiagnosticLocation.table_index` carry
  the position of the table a presentation failure concerns. These failures are
  not leaf-based, so `top_identities` is empty for them and the summary
  previously named no table at all. The value is a position in document order,
  never the table identity, which is derived from cell text and does not cross a
  public error boundary. Summary de-duplication includes it, so two tables
  failing the same way are reported as two.

### Changed

- An adjacent run carrying an attribute or an interleaved comment is now a named,
  consent-gated loss (`element-attribute-omitted`, `xml-comment-dropped`) instead
  of failing closed as `unclassified-storage-change`. Publishing it requires
  explicit consent, as with any reported loss. Nested children, mixed
  `<b>`/`<strong>` aliases, and separated wrappers still fail closed.


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
