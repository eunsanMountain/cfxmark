"""Adjacent ``strong`` / ``em`` runs as one semantic owner.

Confluence's editor splits a bold run into consecutive wrappers as a byproduct
of edit history: ``<strong>A</strong><strong>B</strong>`` renders exactly like
``<strong>AB</strong>``. Markdown has no spelling for the split form, so the
managed round trip has to pick one -- and until this canonicalization existed,
the ownership proof read the wrapper boundary as a content change and left the
page uneditable (``managed-base-roundtrip-changed``) even though the user had
changed nothing.

Two layers cooperate, and the split of responsibility is the point:

* :mod:`cfxmark.storage_normalize` defines the canonical *proof* shape, and is
  applied only by ``extract_storage_source()``. The parse tree itself stays
  verbatim so remote splice can still reuse untouched blocks byte-for-byte.
* the parser and renderer coalesce the *representation*, so one visible run
  becomes one Markdown run.

The two are allowed to disagree, and the direction of that disagreement is
what keeps this safe: the representation layer is looser (it does not inspect
attributes), so an ineligible shape still renders as one Markdown run but its
proof leaves stay distinct -- which fails closed instead of laundering the
difference. Every negative case below asserts that direction.
"""

from __future__ import annotations

import dataclasses

import pytest

import cfxmark
from cfxmark import ConversionOptions

EDITABLE = ConversionOptions(profile="editable")

TAIL = "<p>After</p>"


def _pull(storage: str) -> cfxmark.MarkdownArtifact:
    return cfxmark.to_md_artifact(storage, options=EDITABLE)


def _push(base: cfxmark.MarkdownArtifact, markdown: str, storage: str) -> cfxmark.CfxArtifact:
    return cfxmark.to_cfx_artifact(
        markdown,
        base_artifact=base,
        splice_source=storage,
        options=EDITABLE,
    )


# ---------------------------------------------------------------------------
# Positive: eligible runs are editable and render as one run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run", "expected_markdown"),
    [
        ("<p><strong>A</strong><strong>B</strong></p>", "**AB**"),
        ("<p><strong>A</strong><strong>B</strong><strong>C</strong></p>", "**ABC**"),
        ("<p><strong>A</strong><strong>B</strong><strong>C</strong><strong>D</strong></p>", "**ABCD**"),
        ("<p><em>A</em><em>B</em></p>", "*AB*"),
        ("<p><em>A</em><em>B</em><em>C</em></p>", "*ABC*"),
        ("<p><em>A</em><em>B</em><em>C</em><em>D</em></p>", "*ABCD*"),
        # Whitespace *inside* the run is part of one visible span. Only the
        # outer boundaries are disqualifying.
        ("<p><strong>A </strong><strong>B</strong></p>", "**A B**"),
        ("<p><em>A </em><em>B</em></p>", "*A B*"),
    ],
)
def test_eligible_run_is_ready_to_edit(run: str, expected_markdown: str) -> None:
    storage = run + TAIL
    base = _pull(storage)

    assert base.markdown.splitlines()[0] == expected_markdown
    assert base.status == "ready_to_edit"
    assert base.unmapped_storage_leaves == ()

    candidate = _push(base, base.markdown.replace("After", "Edited"), storage)
    assert candidate.push_safe is True
    # An edit elsewhere leaves the run's original split bytes untouched.
    assert candidate.xhtml == storage.replace("After", "Edited")


@pytest.mark.parametrize("tag", ["strong", "em"])
def test_eligible_run_inside_a_macro_body_is_also_editable(tag: str) -> None:
    storage = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        f"<p><{tag}>A</{tag}><{tag}>B</{tag}></p>"
        "</ac:rich-text-body></ac:structured-macro>" + TAIL
    )
    base = _pull(storage)
    candidate = _push(base, base.markdown.replace("After", "Edited"), storage)

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.xhtml == storage.replace("After", "Edited")


# ---------------------------------------------------------------------------
# Managed edit matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["strong", "em"])
@pytest.mark.parametrize(
    ("label", "replacement", "expected_text"),
    [
        ("first character", "XB", "XB"),
        ("internal boundary", "AXB", "AXB"),
        ("last character", "AX", "AX"),
        ("whole run", "ZZ", "ZZ"),
    ],
)
def test_editing_inside_the_run_emits_one_merged_wrapper(
    tag: str, label: str, replacement: str, expected_text: str
) -> None:
    delimiter = "**" if tag == "strong" else "*"
    storage = f"<p><{tag}>A</{tag}><{tag}>B</{tag}></p>" + TAIL
    base = _pull(storage)
    edited = base.markdown.replace(f"{delimiter}AB{delimiter}", f"{delimiter}{replacement}{delimiter}")

    candidate = _push(base, edited, storage)

    assert candidate.push_safe is True, label
    assert candidate.xhtml == f"<p><{tag}>{expected_text}</{tag}></p>" + TAIL
    proof = candidate.ownership_proof
    assert proof is not None
    assert proof.unclassified == ()
    assert proof.multiple_owners == ()
    assert proof.overlapping == ()
    assert proof.fatal_diagnostic_codes == ()
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


@pytest.mark.parametrize("tag", ["strong", "em"])
def test_deleting_the_run_is_a_clean_single_owner_edit(tag: str) -> None:
    delimiter = "**" if tag == "strong" else "*"
    storage = f"<p><{tag}>A</{tag}><{tag}>B</{tag}></p>" + TAIL
    base = _pull(storage)

    candidate = _push(base, base.markdown.replace(f"{delimiter}AB{delimiter}", "plain"), storage)

    assert candidate.push_safe is True
    assert candidate.xhtml == "<p>plain</p>" + TAIL
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


@pytest.mark.parametrize("tag", ["strong", "em"])
def test_no_edit_at_all_keeps_the_page_publishable(tag: str) -> None:
    storage = f"<p><{tag}>A</{tag}><{tag}>B</{tag}></p>" + TAIL
    base = _pull(storage)

    candidate = _push(base, base.markdown, storage)

    assert base.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.xhtml == storage
    cfxmark.validate_managed_cfx_artifact(candidate, source_storage=storage, options=EDITABLE)


# ---------------------------------------------------------------------------
# Negative eligibility: every one of these must stay unequal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("split", "merged"),
    [
        # A separator -- even one space -- is a real boundary.
        ("<p><strong>A</strong> <strong>B</strong></p>", "<p><strong>AB</strong></p>"),
        ("<p><em>A</em> <em>B</em></p>", "<p><em>AB</em></p>"),
        # Attributes are their own proof leaves; merging would drop one.
        ('<p><strong class="x">A</strong><strong>B</strong></p>', "<p><strong>AB</strong></p>"),
        ('<p><em>A</em><em class="x">B</em></p>', "<p><em>AB</em></p>"),
        # Nested payloads and comments must survive.
        ("<p><strong>A<em>X</em></strong><strong>B</strong></p>", "<p><strong>AXB</strong></p>"),
        ("<p><strong>A</strong><!--x--><strong>B</strong></p>", "<p><strong>AB</strong></p>"),
        # Aliases and mixed tags are not proven equivalent in v1.
        ("<p><b>A</b><b>B</b></p>", "<p><strong>AB</strong></p>"),
        ("<p><b>A</b><strong>B</strong></p>", "<p><strong>AB</strong></p>"),
        ("<p><strong>A</strong><b>B</b></p>", "<p><strong>AB</strong></p>"),
        ("<p><em>A</em><i>B</i></p>", "<p><em>AB</em></p>"),
        ("<p><i>A</i><i>B</i></p>", "<p><em>AB</em></p>"),
        # An empty wrapper is its own leaf.
        ("<p><strong></strong><strong>B</strong></p>", "<p><strong>B</strong></p>"),
        # Outer boundary whitespace: the renderer hoists it out of the
        # delimiters, which would move it across the wrapper.
        ("<p><strong> A</strong><strong>B</strong></p>", "<p><strong> AB</strong></p>"),
        ("<p><strong>A</strong><strong>B </strong></p>", "<p><strong>AB </strong></p>"),
        # Other styles are out of scope for v1.
        ("<p><del>A</del><del>B</del></p>", "<p><del>AB</del></p>"),
        ("<p><sub>A</sub><sub>B</sub></p>", "<p><sub>AB</sub></p>"),
    ],
)
def test_ineligible_split_never_equals_merged(split: str, merged: str) -> None:
    assert cfxmark.storage_semantically_equal(split, merged) is False


# ---------------------------------------------------------------------------
# Namespaced local names are a different element, not an alias
# ---------------------------------------------------------------------------

_NS = 'xmlns:x="urn:x" xmlns:y="urn:y"'


@pytest.mark.parametrize("tag", ["strong", "em"])
@pytest.mark.parametrize(
    ("split_template", "merged_template"),
    [
        # Same foreign namespace on both wrappers.
        ("<x:{tag}>A</x:{tag}><x:{tag}>B</x:{tag}>", "<x:{tag}>AB</x:{tag}>"),
        # Two different foreign namespaces.
        ("<x:{tag}>A</x:{tag}><y:{tag}>B</y:{tag}>", "<x:{tag}>AB</x:{tag}>"),
        # Foreign namespace next to the HTML element.
        ("<x:{tag}>A</x:{tag}><{tag}>B</{tag}>", "<{tag}>AB</{tag}>"),
        ("<{tag}>A</{tag}><x:{tag}>B</x:{tag}>", "<{tag}>AB</{tag}>"),
    ],
)
def test_namespaced_style_runs_are_never_equivalent(tag: str, split_template: str, merged_template: str) -> None:
    # ``{urn:x}strong`` merely shares a local name with HTML ``strong``; it is
    # some other vocabulary. Merging across it would let a distinct element
    # disappear into its sibling, so a local-name-only match is not enough.
    split = f"<p {_NS}>{split_template.format(tag=tag)}</p>"
    merged = f"<p {_NS}>{merged_template.format(tag=tag)}</p>"

    assert cfxmark.storage_semantically_equal(split, merged) is False
    assert cfxmark.storage_semantically_equal(merged, split) is False
    assert cfxmark.storage_readback_equivalent(merged, split) is False
    assert cfxmark.storage_readback_equivalent(split, merged) is False


@pytest.mark.parametrize("tag", ["strong", "em"])
def test_namespaced_style_run_keeps_both_leaves(tag: str) -> None:
    from cfxmark.storage_map import extract_storage_source

    split = f"<p {_NS}><x:{tag}>A</x:{tag}><y:{tag}>B</y:{tag}></p>"
    merged = f"<p {_NS}><x:{tag}>AB</x:{tag}></p>"

    assert len(extract_storage_source(split).leaves) > len(extract_storage_source(merged).leaves)


@pytest.mark.parametrize(
    ("split", "merged"),
    [
        ("<p><strong>A</strong><strong>B</strong></p>", "<p><strong>AB</strong></p>"),
        ("<p><strong>A</strong><strong>B</strong><strong>C</strong></p>", "<p><strong>ABC</strong></p>"),
        ("<p><em>A</em><em>B</em></p>", "<p><em>AB</em></p>"),
        ("<p><strong>A </strong><strong>B</strong></p>", "<p><strong>A B</strong></p>"),
        ("<p><code>a</code><code>b</code></p>", "<p><code>ab</code></p>"),
    ],
)
def test_eligible_split_equals_merged(split: str, merged: str) -> None:
    assert cfxmark.storage_semantically_equal(split, merged) is True


@pytest.mark.parametrize(
    ("storage", "payload"),
    [
        ('<p><strong class="x">A</strong><strong>B</strong></p>', 'class="x"'),
        ("<p><strong>A<em>X</em></strong><strong>B</strong></p>", "<em>X</em>"),
        ("<p><strong>A</strong><!--x--><strong>B</strong></p>", "<!--x-->"),
        ('<p><em>A</em><em class="x">B</em></p>', 'class="x"'),
    ],
)
def test_ineligible_shapes_never_lose_their_payload_silently(storage: str, payload: str) -> None:
    """An ineligible shape is never absorbed without saying so.

    atls strips ``protected_regions``/``remote_subtrees`` on every write path
    (``stateless_write``, ``managed_pull``, ``migration_preflight``), so the
    protected-region machinery cannot be the thing that preserves the payload.
    Assert with those stripped, which is the configuration that actually ships.

    The guarantee is stated as pull-time refusal rather than push-time
    preservation: cfxmark drops element attributes into a
    ``decision_required`` loss report in *all* positions -- a lone
    ``<strong class="x">`` behaves the same -- so "the attribute survives" is
    not a promise this layer can make. What it must never do is stay
    ``ready_to_edit`` with no diagnostic, which is what an unguarded parser
    merge produced.
    """

    full = storage + TAIL
    base = _pull(full)
    stripped = dataclasses.replace(base, protected_regions=(), remote_subtrees=())
    candidate = _push(stripped, base.markdown.replace("After", "Edited"), full)

    if payload in candidate.xhtml:
        return  # the payload survived the round trip
    assert base.status != "ready_to_edit"
    assert [item for item in base.diagnostics if item.blocking]


@pytest.mark.parametrize(
    ("storage", "code"),
    [
        ('<p><strong class="x">A</strong><strong>B</strong></p>', "element-attribute-omitted"),
        ('<p><em>A</em><em class="x">B</em></p>', "element-attribute-omitted"),
        ("<p><strong>A</strong><!--x--><strong>B</strong></p>", "xml-comment-dropped"),
    ],
)
def test_absorbed_payload_is_owned_by_a_removing_occurrence(storage: str, code: str) -> None:
    """The dropped payload must be owned by a named occurrence, not merely reported.

    ``push_safe`` here means "the ownership proof holds", not "safe to publish
    unattended": the candidate is push_safe *and* its XHTML has lost the payload.
    What makes that publishable only with approval is downstream -- the consumer
    turns a ``removed`` occurrence into a consent gate. That only works if the
    occurrence is actually present and actually owned, so pin both:

    * the source migration report names it with ``effect="removed"``;
    * the ownership proof accepts that exact occurrence id.

    Without the second half an occurrence could be reported and yet owned by
    nothing, and a consumer keying on ownership would publish it silently.
    """

    full = storage + TAIL
    base = _pull(full)
    stripped = dataclasses.replace(base, protected_regions=(), remote_subtrees=())
    candidate = _push(stripped, base.markdown.replace("After", "Edited"), full)

    report = candidate.source_migration_report
    assert report is not None
    owning = [item for item in report.occurrences if item.code == code]
    assert owning, f"{code} is not in the source migration report"
    assert all(str(item.effect) == "removed" for item in owning)

    proof = candidate.ownership_proof
    assert proof is not None
    accepted = set(proof.accepted_migration_occurrence_ids)
    assert {item.occurrence_id for item in owning} <= accepted


# ---------------------------------------------------------------------------
# Layer contracts
# ---------------------------------------------------------------------------


def test_alias_pairs_still_render_as_one_markdown_run() -> None:
    # Readable Markdown for aliases is unchanged by this work; only their proof
    # equality stays false (asserted above).
    assert cfxmark.to_md("<p><b>A</b><b>B</b></p>").markdown.rstrip("\n") == "**AB**"
    assert cfxmark.to_md("<p><i>A</i><i>B</i></p>").markdown.rstrip("\n") == "*AB*"
    assert cfxmark.to_md("<p><b>A</b><strong>B</strong></p>").markdown.rstrip("\n") == "**AB**"


@pytest.mark.parametrize(
    ("storage", "node_type", "text"),
    [
        ("<p><strong>A</strong><strong>B</strong></p>", "Strong", "AB"),
        ("<p><em>A</em><em>B</em></p>", "Emphasis", "AB"),
        ("<p><strong>A</strong><strong>B</strong><strong>C</strong></p>", "Strong", "ABC"),
    ],
)
def test_eligible_run_parses_to_one_node_with_one_text_child(storage: str, node_type: str, text: str) -> None:
    # A run that collapsed to ``Strong(Text("A"), Text("B"))`` would render the
    # same but no longer correspond node-for-node to a Markdown parse of that
    # same document, and remote splice would rewrite untouched storage.
    from cfxmark.parsers.cfx import parse_cfx

    document = parse_cfx(storage)[0]
    (paragraph,) = document.children
    assert len(paragraph.children) == 1
    node = paragraph.children[0]
    assert type(node).__name__ == node_type
    assert len(node.children) == 1
    assert node.children[0].content == text


def test_parse_tree_is_not_canonicalized_so_splice_can_reuse_exact_bytes() -> None:
    # The canonical form belongs to the proof layer alone. If the parse tree
    # were canonicalized, an unrelated edit would rewrite this paragraph.
    from cfxmark.parsers.cfx import _parse_fragment_to_element

    root = _parse_fragment_to_element("<p><strong>A</strong><strong>B</strong></p>")
    paragraph = root[0]
    assert [child.text for child in paragraph] == ["A", "B"]


def test_normalization_module_does_not_create_an_import_cycle() -> None:
    import importlib

    for order in (
        ("cfxmark.storage_normalize", "cfxmark.parsers.cfx", "cfxmark.storage_map"),
        ("cfxmark.storage_map", "cfxmark.parsers.cfx", "cfxmark.storage_normalize"),
    ):
        for name in order:
            importlib.import_module(name)


# ---------------------------------------------------------------------------
# Read-back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["strong", "em"])
def test_readback_accepts_either_spelling_of_an_eligible_run(tag: str) -> None:
    split = f"<p><{tag}>A</{tag}><{tag}>B</{tag}></p>"
    merged = f"<p><{tag}>AB</{tag}></p>"

    assert cfxmark.storage_readback_equivalent(merged, split) is True
    assert cfxmark.storage_readback_equivalent(split, merged) is True


@pytest.mark.parametrize(
    "observed",
    [
        "<p><strong>A</strong> <strong>B</strong></p>",  # separator appeared
        '<p><strong class="x">A</strong><strong>B</strong></p>',  # attribute appeared
        "<p><b>A</b><b>B</b></p>",  # alias substituted
        "<p><strong>A<em>B</em></strong></p>",  # nested element appeared
        "<p><strong>AC</strong></p>",  # text changed
    ],
)
def test_readback_rejects_anything_beyond_the_wrapper_boundary(observed: str) -> None:
    expected = "<p><strong>AB</strong></p>"
    assert cfxmark.storage_readback_equivalent(expected, observed) is False
