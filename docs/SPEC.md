# cfxmark conversion spec

This document is the authoritative reference for what cfxmark
guarantees about every construct it knows. Constructs are graded
**I**, **II**, or **III** (see [`REQUIREMENTS.md`](REQUIREMENTS.md));
this file enumerates each grade-I and grade-II construct and lists
the grade-III escape hatches.

## Conventions

* `<ac:>` is `xmlns:ac="http://atlassian.com/content"`.
* `<ri:>` is `xmlns:ri="http://atlassian.com/resource/identifier"`.
* "Lossless after canonicalization" means
  `cfxmark.canonicalize_cfx(original) == cfxmark.canonicalize_cfx(round_tripped)`
  — not byte-for-byte equality of the raw XML, since Confluence
  injects volatile attributes and editor cosmetics that have no
  semantic role.
* Volatile attributes stripped during canonicalization:
  `ac:macro-id`, `ac:local-id`, `ac:schema-version`, `ac:thumbnail`,
  `ac:border`, `ac:align`, `ri:version-at-save`, `data-uuid`,
  `data-highlight-colour`.

## Grade I — Native CommonMark / GFM

| Markdown | Confluence storage | Notes |
|---|---|---|
| `# Title` … `###### Title` | `<h1>...</h1>` … `<h6>...</h6>` | Six levels. Setext headings normalised to ATX. Headings containing `<br/>` split into heading + paragraph. |
| Plain paragraph | `<p>...</p>` | Empty paragraphs dropped on both sides. |
| `**bold**` | `<strong>...</strong>` | Falls back to inline `<strong>` HTML when CJK adjacency would break the CommonMark boundary rule. |
| `*italic*` | `<em>...</em>` | Same fallback as `**bold**`. |
| `` `code` `` | `<code>...</code>` | Backtick runs are doubled if the content contains a backtick. `\|` inside `<code>` collapses to `|` (GFM table-cell limitation). |
| `~~struck~~` | `<del>...</del>` | GFM extension. |
| `[text](url)` | `<a href="url">text</a>` | `title` is preserved and double-quotes inside it are backslash-escaped. |
| `![alt](url)` | `<ac:image><ri:url ri:value="url"/></ac:image>` | External URL. |
| `![alt](path)` | `<ac:image><ri:attachment ri:filename="path"/></ac:image>` | Local file. Path also surfaces in `result.attachments`. Asset marker emitted (see Asset markers). |
| `![alt](path#cfxmark:w=300,h=200)` | `<ac:image ac:width="300" ac:height="200">…</ac:image>` | Image dimensions encoded in the URL fragment for clean Markdown. |
| `- item` (nested OK) | `<ul><li>...</li></ul>` | Bullet marker normalised to `-`. Singleton-paragraph `<li><p>X</p></li>` flattens to `<li>X</li>`. |
| `1. item` | `<ol><li>...</li></ol>` | Ordered lists carry their `start` index. |
| `> quote` | `<blockquote><p>...</p></blockquote>` | Multi-line quotes supported. |
| `---` | `<hr/>` | |
| `\| a \| b \|` table | `<table><thead><tr>...</tr></thead><tbody>...</tbody></table>` | Simple GFM tables — see "Tables" below for `colspan` / `rowspan` and multi-paragraph cells. |
| ` ```lang\n…\n``` ` | `<ac:structured-macro ac:name="code">…<ac:plain-text-body>` CDATA `</ac:plain-text-body></ac:structured-macro>` | The HTML form `<pre><code>...</code></pre>` is canonicalized to the macro form. Cosmetic params (`linenumbers`, `theme`, …) are stripped. |
| HTML entities (`&amp;` `&lt;` `&gt;` `&quot;` `&nbsp;` …) | Same / Unicode | Entities outside the XML-safe set are decoded to Unicode before parsing. CDATA contents are exempted. |
| `<br/>` (HTML) | `<br/>` | Inside paragraphs and table cells. Lone `<br/>` inside an empty cell is canonicalized away. |

### Tables in detail

* **Simple GFM tables** — pipe syntax with `---` separator row.
* **`colspan`** — encoded in Markdown as a continuation cell with the
  literal `<` character (MultiMarkdown convention):
  ```markdown
  | a | b | c |
  | --- | --- | --- |
  | spans 2 columns | < | x |
  ```
* **`rowspan`** — same idea with `^`:
  ```markdown
  | a | b |
  | --- | --- |
  | spans down | x |
  | ^ | y |
  ```
* **Multi-paragraph cells** — Confluence's `<td><p>X</p><p>Y</p></td>`
  is flattened to inline content joined by `<br>` HTML tags. GFM
  passes `<br>` through verbatim, so the cell renders as two lines.
* **Cells with `<div>` wrappers** — Confluence's editor often wraps
  cell content in `<div class="content-wrapper">`. The wrapper is
  unwrapped transparently during conversion and canonicalization.
* **Cells with embedded macros / images / lists** — emit through the
  inline opaque mechanism (see below) so the rest of the table stays
  native.

## Grade II — Directive macros

Each directive macro is rendered in Markdown as a pandoc-style
fenced div:

```
::: name key="value" key="value"
<body markdown>
:::
```

| Confluence macro | Directive | Notes |
|---|---|---|
| `info` | `::: info` | Optional `title` parameter. Rich-text body. |
| `note` | `::: note` | Same shape as `info`. |
| `warning` | `::: warning` | |
| `tip` | `::: tip` | |
| `jira` | `::: jira` | Empty body. Parameters carry `key`, `jqlQuery`, `server`, etc. |
| `expand` | `::: expand title="…"` | Collapsible panel with rich-text body. |
| `toc` | `::: toc maxLevel="2"` | Empty body. Renders as Confluence's table-of-contents macro. |

The default registry lives in `cfxmark.macros.builtins`. Custom
macros are added via `MacroRegistry.register(handler)`.

## Grade III — Opaque preservation

Anything not on the lists above is captured verbatim through one of
the opaque mechanisms documented in [`OPAQUE.md`](OPAQUE.md):

* **Block opaque** — sentinel-wrapped fenced code block. Used for
  `<ac:structured-macro>` invocations whose `ac:name` is not in the
  registry, `<table>` with non-cell-span structural problems,
  `<pre><code>` with embedded non-text elements.
* **Inline opaque** — short `[label](cfx:op-XXXXXXXX)` link with the
  XML payload stored in a `cfxmark:payloads` sidecar at the bottom
  of the document. Used for unknown elements that appear in inline
  position (user mentions, inline jira issue references, …) so the
  surrounding paragraph stays native Markdown.

Opaque blocks survive `cfx → md → cfx` byte-for-byte, **including**
the `ac:macro-id` UUID. Confluence therefore treats the round-tripped
macro as the same instance as the original — preserving comments,
attachments, and permissions attached to the macro.

## Asset markers

Local-attachment images are emitted with a metadata marker that the
caller can resolve later:

```markdown
![](image-3.png#cfxmark:w=700)<!-- cfxmark:asset src="image-3.png" -->
```

`cfxmark.resolve_assets(md, fetcher, mode="sidecar"|"inline")` reads
the markers, calls `fetcher` to download bytes, and either saves
them to a sidecar directory (with relative-path links) or embeds
them as `data:` URIs. The marker is preserved in both cases, so the
resolution is idempotent and a subsequent `to_cfx` always recovers
the original Confluence filename.

## Header notice

When a converted Markdown document contains any opaque or directive
construct, cfxmark prepends a single-line `<!-- cfxmark:notice ... -->`
HTML comment to the top of the document. The comment explains the
marker conventions to humans and AI agents and asks them not to
edit the markers. It is invisible in any Markdown viewer.

## Security hardening

* Inputs containing `<!DOCTYPE>` or `<!ENTITY>` declarations are
  refused before lxml parses them, blocking XXE and billion-laughs
  attacks.
* The lxml parser is configured with `no_network=True`,
  `load_dtd=False`, `huge_tree=False`.
* Opaque sentinels carry a SHA-256 fingerprint. A user typing the
  literal sentinel sequence in their own Markdown does **not** get
  silently re-interpreted as an opaque block; the verification fails
  and the region falls back to plain text.

## Known limitations (as of v0.2)

* **Macro handler API leaks lxml.** Custom `MacroHandler`
  implementations receive and return `lxml.etree._Element` objects
  directly. A thin `MacroBody` adapter is planned for v0.3.
* **HTML comments in Markdown** are dropped with a warning, with the
  exception of cfxmark's own opaque / asset / header markers.
  Confluence drops them on save anyway.
* **Image dimensions ride in the URL fragment.** If the original
  image URL already had a fragment, cfxmark appends with `&`. Do
  not use a `#cfxmark:` fragment for any other purpose.
* **`<th scope="...">`, `<td title="...">`** attributes are stripped
  during canonicalization since Markdown cannot preserve them.

## What does NOT round-trip

| Construct | Behaviour | Reason |
|---|---|---|
| HTML comments in Markdown (other than cfxmark markers) | Dropped with warning | Confluence strips them on save anyway. |
| Inline HTML other than `<strong>` `<em>` `<code>` `<del>` `<s>` `<br>` and the recognised cfxmark markers | Dropped with warning | No reliable Confluence equivalent. |
| Images inside `<pre><code>` | Whole `<pre>` becomes opaque | Cannot be represented in a Markdown code fence. |
| `<span style="color:#aaa">` (non-default colours) and other inline span styling | Style stripped, contents preserved | Markdown has no inline colour syntax. |
| `<ac:structured-macro>` macros not in the default registry (`status`, `drawio`, custom plugins, …) | Captured as opaque (block or inline) | Promote to grade II by registering a `MacroHandler`. |
