"""Storage-XHTML canonicalization shared by parsing and proof-leaf extraction.

Markdown cannot express two adjacent inline runs of the same kind. There is no
spelling for ``<code>a</code><code>b</code>`` (`` `a``b` `` re-parses as one
span) and none for ``<strong>A</strong><strong>B</strong>`` (``**A****B**`` is
not bold at all). Confluence's editor emits these split wrappers routinely, as
a byproduct of edit history rather than as intent.

The ownership proof defines a change as a difference between storage leaf
sets, so without this transform a managed round trip reports the boundary that
Markdown cannot carry as if the user had rewritten the paragraph -- the page
becomes uneditable (``managed-base-roundtrip-changed``) even though nothing
about its content changed.

This transform is applied by ``extract_storage_source()`` only -- it defines
the canonical *proof* shape. The parse tree stays verbatim, because remote
splice reuses subtrees of it as exact storage bytes for untouched content;
canonicalizing there would rewrite blocks the user never edited. AST-level
merging of the *representation* lives in the parser and renderer instead, and
is deliberately looser (the AST does not carry attributes). The two layers are
allowed to disagree in that direction only: a shape this transform rejects
still renders as one Markdown run, but its proof leaves stay distinct.

Eligibility is narrow on purpose. Only ``strong`` and ``em`` participate, and
only when the merge cannot hide anything:

* the elements are directly consecutive siblings with no separator between
  them, not even whitespace;
* every element carries no attributes (the extractor treats attributes as
  their own proof leaves, so merging an attributed wrapper would drop one);
* every element is text-only -- no nested elements, comments, or macros;
* every element has non-empty text (an empty wrapper is its own leaf, and
  dropping it is a real loss);
* the combined run's outer boundaries are not whitespace, because the renderer
  hoists boundary whitespace out of the delimiters and would move it across
  the wrapper.

Aliases (``b``, ``i``), mixed tags, ``del``, and ``s`` are excluded: they are
not proven equivalent, so they stay fail-closed. The renderer may still render
an alias pair as one Markdown run -- readable output is not proof that a
managed candidate is push-safe.
"""

from __future__ import annotations

from lxml import etree as ET

from cfxmark.xml_ns import strip_ns

# ``code`` keeps its original, looser contract: it predates this module and
# merging it has always been unconditional for attribute-free, childless
# spans. ``strong``/``em`` are held to the stricter contract below.
_LEGACY_TAGS = frozenset({"code"})
_STYLE_TAGS = frozenset({"strong", "em"})
_COALESCING_TAGS = _LEGACY_TAGS | _STYLE_TAGS


def _plain_run_tag(element: ET._Element) -> str | None:
    """Canonical tag name if ``element`` can start or continue a run.

    Returns ``None`` for comments, processing instructions, aliases, attributed
    wrappers, and anything with element children.

    ``strong``/``em`` must additionally be namespace-free. ``{urn:x}strong`` is
    not the HTML element -- it is some other vocabulary that happens to share a
    local name -- so treating it as equivalent would let a distinct element
    disappear into its sibling. ``code`` keeps matching on the local name,
    which is its pre-existing behavior.
    """

    if not isinstance(element.tag, str):
        return None
    tag = strip_ns(element.tag)
    if tag in _STYLE_TAGS and element.tag != tag:
        return None
    if tag not in _COALESCING_TAGS:
        return None
    if len(element) or element.attrib:
        return None
    return tag


def _is_eligible_style_run(run: list[ET._Element]) -> bool:
    texts = [element.text or "" for element in run]
    if any(not text for text in texts):
        return False
    return not (texts[0][:1].isspace() or texts[-1][-1:].isspace())


def _coalesce_children(parent: ET._Element) -> None:
    children = list(parent)
    index = 0
    while index < len(children):
        tag = _plain_run_tag(children[index])
        if tag is None:
            index += 1
            continue

        run = [children[index]]
        cursor = index
        # Extend while the previous element is *immediately* followed by
        # another plain element of the same tag. A tail -- including a single
        # space -- is a real separator and ends the run.
        while cursor + 1 < len(children) and not run[-1].tail:
            following = children[cursor + 1]
            if _plain_run_tag(following) != tag:
                break
            run.append(following)
            cursor += 1

        if len(run) > 1 and (tag in _LEGACY_TAGS or _is_eligible_style_run(run)):
            merged = run[0]
            merged.text = "".join(element.text or "" for element in run)
            merged.tail = run[-1].tail
            for element in run[1:]:
                parent.remove(element)

        index = cursor + 1


def coalesce_adjacent_inline_runs(root: ET._Element) -> ET._Element:
    """Merge eligible adjacent inline runs in place and return ``root``."""

    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        _coalesce_children(parent)
    return root


def _style_wrapper_tag(element: ET._Element) -> str | None:
    """Canonical style tag if ``element`` is a bare, attribute-free wrapper.

    Unlike :func:`_plain_run_tag` this permits element children: a nested
    ``<strong><strong>x</strong></strong>`` carries no information the flat
    form loses, so merging it is safe for the *representation*. It is still
    not proof-equivalent, which is why the two predicates differ.
    """

    if not isinstance(element.tag, str):
        return None
    tag = strip_ns(element.tag)
    if tag not in _STYLE_TAGS or element.tag != tag or element.attrib:
        return None
    return tag


def continues_plain_style_run(previous: ET._Element | None, current: ET._Element) -> bool:
    """Whether ``current`` extends a plain style run started by ``previous``.

    The AST does not carry attributes, so a parser that merges style nodes
    without asking this question cannot tell ``<strong>A</strong><strong>B</strong>``
    from ``<strong class="x">A</strong><strong>B</strong>`` — and merging the
    second one drops an attribute that no later layer can recover, which is
    exactly how an attributed wrapper became silently push-safe.

    Deliberately looser than the proof contract in two ways: nested children
    are allowed (see above), and boundary whitespace is not checked. Both only
    affect whether the two *storage* shapes are equivalent, and erring loose
    here is safe — the run still fails closed when the proof layer disagrees.
    """

    if previous is None or previous.tail:
        return False
    tag = _style_wrapper_tag(current)
    return tag is not None and _style_wrapper_tag(previous) == tag


__all__ = ["coalesce_adjacent_inline_runs", "continues_plain_style_run"]
