"""Can Markdown hold this page, and if not, exactly what does it drop?

The ownership proof answers a different and much harder question -- *which
storage leaf does each Markdown block inherit from?* -- and it fails whenever the
correspondence is ambiguous, which is often. Measured across 55 real pages, every
single planted-loss scenario was refused by that proof rather than by any
judgement about content. Nothing was overwritten, and nothing was explained
either.

This module asks the question that always has an answer:

    S0  the storage the server holds now
    M0  to_md(S0)
    C0  to_cfx(M0)          no base artifact, no presentation, no splice source

    what is in S0 that is not in C0?

C0 is deliberately built with no help from the original. That is the whole point:
whatever survives has survived on the strength of the Markdown alone, so the
difference is precisely what Markdown cannot hold.

Design rules, all deliberate:

* **Default deny.** A difference is ``unknown`` unless a registered rule names
  it. New Confluence markup therefore closes the fast path rather than slipping
  through it, and widening the registry is an evidence-backed edit.
* **No wildcards.** Rules key on an exact element name and an exact attribute
  name. ``style`` is already split per CSS property upstream, so a rule can name
  ``style/background-color`` but never bare ``style``.
* **Values, not just presence.** A rule states which stored values it covers.
  ``present -> absent`` licenses dropping a harmless default and downgrading a
  real one in the same words, and only the second loses data.
* **Identity is not appearance.** Dropping ``ac:macro-id`` changes nothing a
  reader can see and detaches every comment on that macro. Fields like it get
  their own verdict and may never be called equivalent without a live
  experiment.

The registry starts almost empty on purpose. Every entry below cites what was
measured, where, and against which converter version, because a rule nobody can
re-check is a guess wearing evidence's clothes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

from lxml import etree as ET

from cfxmark.source_map import fingerprint_semantic_value
from cfxmark.storage_map import extract_storage_source, storage_leaf_identity
from cfxmark.xml_ns import AC_URI, ns_of, strip_ns

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from cfxmark.api import ConversionOptions
    from cfxmark.macros import MacroRegistry

# --------------------------------------------------------------------------
# Verdicts -- what one difference is
# --------------------------------------------------------------------------

#: Markdown carries this. Publishing the regeneration stores the same thing.
EQUIVALENT = "equivalent"
#: Markdown could carry it; this converter does not. Ours to fix, not the
#: author's to consent to -- they lose nothing once it is fixed.
CONVERTER_GAP = "converter_gap"
#: Markdown genuinely cannot hold it. The author loses something real, and can
#: be told exactly what and how much.
NAMED_LOSS = "named_loss"
#: A value that is identity rather than appearance. The original still holds it,
#: so something *could* put it back.
#:
#: Deliberately not a fast path, and deliberately not a claim that carrying is
#: safe here. It says one thing only: the information needed to restore this
#: still exists. It does not say the correspondence is unique -- two
#: byte-identical macros with different ids both land here, and nothing in a
#: positional walk can say which id belongs to which. Deciding whether a carry is
#: safe belongs to the carrying mechanism, which must refuse an ambiguous one.
IDENTITY_CARRY = "identity_carry"
#: The platform's own editor replaces this form on save, so regenerating the
#: replacement is what the editor would have done anyway.
#:
#: Deliberately its own verdict rather than `EQUIVALENT`, because it is *not*
#: equivalence and the difference matters to whoever reads the receipt: the stored
#: bytes differ, the rendered `body.view` differs, and the page's spacing visibly
#: differs until the platform converges it. What makes it not a loss is narrower
#: than "no visible change" -- it is that the state being replaced does not
#: survive the platform's own save, so no author can hold it.
#:
#: Every rule carrying this verdict must name the observation (`basis`) and the
#: shape it was observed in (`scope`). One observation licenses one shape; the
#: verdict is not a general permission to normalise whitespace.
PLATFORM_EDITOR_CANONICALIZATION = "platform_editor_canonicalization"
#: No rule speaks to this. The only honest answer, and the default.
UNKNOWN = "unknown"

# --------------------------------------------------------------------------
# Classifications -- what a whole page needs from a person
#
# Ordered by how much work the answer demands, most first, because a page is
# only as publishable as its worst finding.
# --------------------------------------------------------------------------

MARKDOWN_LOSSLESS = "markdown_lossless"
MARKDOWN_IDENTITY_BOUND = "markdown_identity_bound"
NAMED_LOSS_CONSENTABLE = "named_loss_consentable"
CONVERTER_GAP_BLOCKED = "converter_gap_blocked"
UNKNOWN_BLOCKED = "unknown_blocked"


class RegistryViolation(Exception):
    """A rule that must not be allowed to sit in the registry."""


# Fields that carry identity rather than appearance. Losing one does not change
# how a page looks, which is exactly why it is dangerous: nothing in a visual or
# textual comparison notices, and the damage surfaces later as comments detached
# from their macro or an attachment reference pointing nowhere.
#
# `ac:macro-id` is why this list exists. It looks exactly like a server-assigned
# identifier -- uuid-shaped, unique per page, never hand-authored -- and on that
# basis it was once declared harmless. cfxmark's own opaque-macro design says the
# opposite: Confluence treats a reused id as the same macro instance and keeps its
# comments, permissions and attachments.
#
# Measured 2026-07-29: the server preserves a macro's id only when the body is
# byte-identical. Editing one character of prose inside the macro re-assigns it.
# So an ordinary edit detaches every macro on the page unless the id is carried.
IDENTITY_BEARING: frozenset[tuple[str, str]] = frozenset(
    {
        ("ac:structured-macro", "ac:macro-id"),
        ("ac:structured-macro", "ac:name"),
        ("ac:structured-macro", "ac:local-id"),
        ("ri:attachment", "ri:filename"),
        ("ri:attachment", "ri:version-at-save"),
        ("ri:page", "ri:content-title"),
        ("ri:page", "ri:space-key"),
        ("ri:user", "ri:userkey"),
        ("ri:user", "ri:username"),
        ("ac:link", "ac:anchor"),
    }
)

# Losses cfxmark already names in its own migration report. Real information an
# author can lose; consentable, never automatic.
NAMED_LOSS_ATTRIBUTES: dict[tuple[str, str], str] = {
    ("td", "data-highlight-colour"): "table-cell-background",
    ("th", "data-highlight-colour"): "table-cell-background",
}


def _matches(allowed: tuple[str | None, ...] | None, value: str | None) -> bool:
    """Whether `value` is one of the values a rule names, `None` meaning unconstrained.

    Values arrive as fingerprints, so a rule's plain text is fingerprinted here rather
    than at the rule -- keeping the registry readable, which is most of what makes it
    re-checkable.
    """

    if allowed is None:
        return True
    for candidate in allowed:
        if candidate is None:
            if value is None:
                return True
        elif value is not None and value == fingerprint_semantic_value(candidate):
            return True
    return False


@dataclass(frozen=True)
class AttributeRule:
    """One registered, justified reason an attribute difference is not data loss.

    Every field is required because every field is something a later reader needs
    in order to re-check the rule instead of trusting it:

        element, attribute    exact names. Never a family, never a prefix.
        context               where this holds. A rule true inside a code macro
                              and false elsewhere must say so.
        relation              which direction is covered. A rule licensing a drop
                              must not also license an invention.
        rationale             why no information is lost.
        evidence              what was observed, where, when.
        verified_against      the converter version the observation was made on.
                              Behaviour changes; a rule verified two releases ago
                              is a claim, not a fact.
        controlled_experiment required for an EQUIVALENT verdict on an
                              identity-bearing field. Names the live experiment
                              that showed the server does not care.
        when_before           which stored values this covers, `None` inside the
                              tuple meaning absent. Required for EQUIVALENT.
        when_after            which regenerated values this covers, same spelling.
                              Required for an EQUIVALENT rule that covers an
                              addition, because there `when_before` constrains
                              nothing: "the source had no value" is true of writing
                              the server's own default and of writing anything else.
    """

    element: str
    attribute: str
    verdict: str
    context: str
    relation: str
    rationale: str
    evidence: str
    verified_against: str
    controlled_experiment: str | None = None
    when_before: tuple[str | None, ...] | None = None
    when_after: tuple[str | None, ...] | None = None

    def covers(self, before: str | None, after: str | None = None) -> bool:
        """Whether this rule speaks to a difference between these two values.

        Both sides must match when both are constrained. `after` defaults to `None`
        so an existing caller asking only about the stored value keeps working -- but
        a rule naming `when_after` then only matches a difference whose regenerated
        value is absent, which is the safe way round: a rule constrains more than the
        caller asked about rather than less.
        """

        return _matches(self.when_before, before) and _matches(self.when_after, after)


@dataclass(frozen=True)
class StructureRule:
    """A structural difference with an exact, named signature.

    Keyed on the full set of child tags gained and lost, never on the element
    alone: a `td` losing a `p` wrapper and a `td` losing the macro that was its
    only content are not the same event, and one rule must not stand in for the
    other. The second destroys content -- see the `macro_in_table_cell` case in
    the harness -- so a rule broad enough to cover both would license it.
    """

    code: str
    element: str
    removed_children: tuple[str, ...]
    added_children: tuple[str, ...]
    verdict: str
    context: str
    rationale: str
    evidence: str
    verified_against: str
    #: The single observation this rule's verdict rests on, when the verdict is
    #: something other than a plain reading of the two documents. Required for
    #: `PLATFORM_EDITOR_CANONICALIZATION`, because that verdict is a claim about
    #: what the platform does rather than about what Markdown can hold, and a claim
    #: with no named observation cannot be re-checked or retired.
    basis: str | None = None
    #: The name of a registered scope predicate, or `None` for "wherever the
    #: signature matches".
    #:
    #: The signature `p: () -> ("br",)` is the same event in a table cell, in a
    #: list item and between two paragraphs, and the difference between them is not
    #: in the signature at all. A rule whose justification was observed in one
    #: place must be able to say so, or the registry's "no wildcards" rule holds
    #: for element names and quietly fails for context.
    scope: str | None = None


@dataclass(frozen=True)
class UnwrapRule:
    """A block wrapper that disappears, its text moving up into its parent.

    This shape needs its own rule form because one event arrives as five separate
    leaves and no per-leaf rule can see the whole of it:

        <td><p>a</p></td>  ->  <td>a</td>

            p  element    removed
            p  text       removed
            p  topology   removed
            td text       added
            td topology   changed

    Judged leaf by leaf, every one of those is an unexplained structural change,
    and the page blocks on five unknowns that are really one thing a person could
    have been told about in a sentence.

    Both tags are named, and so is everything allowed to appear in the wrapper's
    place, because the same parent can lose a child for reasons that are not this.
    A `td` whose only child is a macro also ends up with text where an element
    used to be -- and that one destroyed the macro's body. A rule keyed on "the
    parent's children changed" would cover both.
    """

    #: The event's name, reported instead of the individual leaves. Five leaves
    #: for one unwrapped paragraph reads as five problems; the count that helps
    #: is "how many paragraphs", not "how many leaves moved".
    code: str
    parent: str
    child: str
    #: Tags that may appear in the wrapper's place. `("br",)` covers several
    #: paragraphs collapsing into one line with separators; `()` means nothing
    #: may appear at all.
    allowed_added: tuple[str, ...]
    verdict: str
    context: str
    rationale: str
    evidence: str
    verified_against: str


UNWRAP_RULES: tuple[UnwrapRule, ...] = (
    UnwrapRule(
        code="table-cell-paragraph-unwrapped",
        parent="td",
        child="p",
        allowed_added=("br",),
        verdict=NAMED_LOSS,
        context="a table cell regenerated from Markdown, which has no block structure inside cells",
        rationale=(
            "Markdown table cells hold inline content only, so a cell's paragraphs cannot "
            "survive as paragraphs -- several become one line joined by breaks. The author "
            "loses paragraph separation inside that cell, which is visible and countable, so "
            "it is a loss to show rather than one to excuse or to give up on naming."
        ),
        evidence=(
            "corpus 55 pages: 227 cells hold more than one paragraph, across 20 pages. Live "
            "2026-07-29: the server stores <td><p>one</p><p>two</p></td> exactly as sent, so "
            "the collapse is ours and not a server normalisation."
        ),
        verified_against="cfxmark 0.5.1",
    ),
    UnwrapRule(
        code="table-header-paragraph-unwrapped",
        parent="th",
        child="p",
        allowed_added=("br",),
        verdict=NAMED_LOSS,
        context="a table header cell, for the same reason as td",
        rationale="Markdown header cells hold inline content only, exactly as body cells do.",
        evidence="same measurement as the td rule; header cells share the code path",
        verified_against="cfxmark 0.5.1",
    ),
    UnwrapRule(
        code="list-item-paragraph-unwrapped",
        parent="li",
        child="p",
        allowed_added=(),
        verdict=CONVERTER_GAP,
        context="a list item regenerated from Markdown",
        rationale=(
            "Unlike a table cell, Markdown *can* hold a paragraph inside a list item -- that "
            "is what a loose list is. So this is not the format's limit but this converter's, "
            "and the author loses nothing once it is fixed. Calling it a named loss would ask "
            "someone to consent to a defect."
        ),
        evidence=(
            "corpus 55 pages: 22 occurrences across 2 pages. Live 2026-07-29: the server "
            "stores <li><p>alpha</p></li> exactly as sent, so the unwrap is ours."
        ),
        verified_against="cfxmark 0.5.1",
    ),
)

_UNWRAP_INDEX = {(rule.parent, rule.child): rule for rule in UNWRAP_RULES}


_SCHEMA_VERSION_EVIDENCE = (
    "live 2026-07-29: expand and code macros published with and without the attribute. "
    'Absent came back "1", "1" came back "1", "2" came back "2" -- the server supplies its '
    "default and does not touch a value we send. Corpus: 125 pages across two runs, 224 "
    'occurrences, every one "1".'
)

ATTRIBUTE_RULES: tuple[AttributeRule, ...] = (
    AttributeRule(
        element="ac:structured-macro",
        attribute="ac:schema-version",
        verdict=EQUIVALENT,
        when_before=("1",),
        # Absent, and only absent. Unset, this matched any regenerated value, so the rule
        # said `present "1" -> absent` and meant `present "1" -> whatever`.
        when_after=(None,),
        context=(
            "a structured macro regenerated from Markdown with no remote source, where the "
            "stored value is the server's own default"
        ),
        relation='present "1" -> absent',
        rationale=(
            'Confluence stamps ac:schema-version="1" onto any macro it saves without one, '
            "so publishing a regeneration that omits the attribute restores the same stored "
            "value. Nothing has to be carried because the server puts it back. Measured "
            "rather than reasoned from the attribute's appearance, which is the mistake "
            "ac:macro-id came from."
        ),
        evidence=_SCHEMA_VERSION_EVIDENCE,
        verified_against="cfxmark 0.5.1",
        controlled_experiment="atls_harness bin/experiment.py server_defaults",
    ),
    # The same fact read the other way, and it was blocking pages over nothing.
    #
    # A stored macro with no `ac:schema-version` regenerates with `"1"`, which arrived
    # as an *added* attribute and therefore as `unknown` -- so the page blocked. But
    # the measurement that licenses the drop licenses this too: absent came back "1",
    # so the value the server ends up holding is the same whether we send the
    # attribute or leave it out. Nothing is added that the server was not going to
    # add.
    #
    # `when_after` exists because of this rule. On the drop side `when_before` is what
    # keeps a harmless default and a real downgrade apart; on this side `when_before`
    # constrains nothing at all -- "the source had no value" is equally true of
    # writing the server's default and of writing "2" over it.
    AttributeRule(
        element="ac:structured-macro",
        attribute="ac:schema-version",
        verdict=EQUIVALENT,
        when_before=(None,),
        when_after=("1",),
        context=("a stored macro carrying no ac:schema-version, regenerated with the server's own default"),
        relation='absent -> present "1"',
        rationale=(
            'Confluence stamps ac:schema-version="1" onto any macro it saves without one, so '
            "the stored value after this publish is the value the page would have had anyway. "
            'Narrowed to exactly "1" rather than to the direction: a rule keyed on the source '
            'having no value would license writing any value, and writing "2" over a macro is '
            "not something this evidence says anything about."
        ),
        evidence=_SCHEMA_VERSION_EVIDENCE,
        verified_against="cfxmark 0.6.0",
        controlled_experiment="atls_harness bin/experiment.py server_defaults",
    ),
)

# Deliberately NOT registered, and the reason `when_before` exists:
#
#     stored "2" -> regenerated "1" or absent
#
# The source-free path renders every directive macro with "1", so a macro stored
# as "2" comes back downgraded, and the server preserves whatever we send rather
# than undoing it. No corpus page carries a non-"1" value today (0 of 125), so
# this is unobserved rather than impossible, and it stays `unknown`.

# --------------------------------------------------------------------------
# Scope predicates -- where a rule's justification was actually observed
# --------------------------------------------------------------------------
#
# A predicate reads the *source* element the difference was measured from, not the
# path string, so it can see attributes, text payloads and siblings. `elements`
# from the same extraction walk supplies it, which is why there is no second walk
# here: two numberings that drift apart would let a rule fire on a node nobody
# checked.

_BLOCK_CONTAINERS_WITHOUT_OBSERVATION = frozenset({"li", "td", "th", "blockquote"})


def _is_macro(element: ET._Element) -> bool:
    return (
        isinstance(element.tag, str)
        and ns_of(element.tag) == AC_URI
        and strip_ns(element.tag) in {"structured-macro", "macro", "adf-extension"}
    )


def _holds_a_macro(element: ET._Element) -> bool:
    return _is_macro(element) or any(_is_macro(node) for node in element.iterdescendants())


def _empty_paragraph_between_body_blocks(element: ET._Element) -> bool:
    """An exactly-empty `<p>` with an ordinary body block on each side.

    Every clause is one thing the live measurement showed, and dropping any of
    them widens the rule past its evidence:

    * **no attributes, no text.** The server preserves both -- `<p style="...">`
      comes back with its style and `<p> </p>` comes back with its space. So
      neither paragraph is empty, and neither is this rule's business. The
      whitespace case is the one that needed a guard rather than a comment: the
      text leaf it loses hangs off the paragraph's own path, so a rule that
      matched the paragraph would have swallowed it and reported one tidy
      presentation change in place of a dropped character.
    * **no `li`/`td`/`th`/`blockquote` ancestor, and no macro ancestor.** The
      server stores `<p />` *identically* in all of those, so "the platform treats
      them differently" is not the reason -- measured, and it is false. The reason
      is that the editor-save observation was made between ordinary body blocks
      and nowhere else.
    * **something follows it.** Leading and middle positions are both observed;
      trailing is not, and a paragraph with nothing after it is the one position where
      "the editor replaces it" has never been watched.

    A macro neighbour is *not* grounds for exclusion, and neither is the leading
    position. Both were excluded in the first version -- correctly, on the grounds that
    nobody had watched the editor there -- and on 2026-07-31 somebody did: two sandbox
    pages, one leading and one beside a macro, opened and saved with no edit, and `<p/>`
    became `<p><br /></p>` at v2 in both, exactly as a between-blocks control did. That
    shape is the large majority of the occurrences on the corpus this was measured against.
    """

    if not isinstance(element.tag, str) or ns_of(element.tag) is not None or strip_ns(element.tag) != "p":
        return False
    if len(element) or element.attrib or element.text:
        return False
    for ancestor in element.iterancestors():
        if _is_macro(ancestor):
            return False
        if isinstance(ancestor.tag, str) and strip_ns(ancestor.tag) in _BLOCK_CONTAINERS_WITHOUT_OBSERVATION:
            return False
    # Only "something follows". The trailing position stays out: it is the one place the
    # editor-save convergence has not been observed, and the rule may not reach further
    # than the observation.
    return element.getnext() is not None


STRUCTURE_SCOPES: dict[str, Callable[[ET._Element], bool]] = {
    "empty_paragraph_between_body_blocks": _empty_paragraph_between_body_blocks,
}


_EMPTY_PARAGRAPH_EVIDENCE = (
    "live 2026-07-29 and 07-31. The server folds two of the three spellings and keeps the "
    "third: <p/> and <p></p> both come back as <p />, while <p><br/></p> comes back "
    "unchanged, and the rendered body.view differs, so the distinction reaches the browser. "
    "Contexts: <p /> is stored identically between blocks, in a list item, in a table cell, "
    "before a macro and inside one, while <p style=...> keeps its attribute and <p> </p> "
    "keeps its space (experiment empty_paragraph_contexts). Corpus 70 pages: <p/> 6 "
    "occurrences, <p><br/></p> 131, never both on one page."
)

STRUCTURE_RULES: tuple[StructureRule, ...] = (
    StructureRule(
        code="empty-paragraph-normalised",
        element="p",
        removed_children=(),
        added_children=("br",),
        verdict=PLATFORM_EDITOR_CANONICALIZATION,
        basis="editor_save_convergence",
        scope="empty_paragraph_between_body_blocks",
        context=(
            "an exactly-empty paragraph at the top level of the body, in any position but "
            "last -- leading and middle are both observed, trailing is not"
        ),
        rationale=(
            "`<p/>` does not survive a save in Confluence's own editor. Opened and saved with "
            "no edit at all, the page comes back with `<p><br /></p>` at the next version, so "
            "the form being replaced is one no author can keep -- the platform takes it away "
            "at the first touch. Regenerating the replacement does what the editor does.\n\n"
            "Two easier justifications were measured and are FALSE, and the scope is this "
            "tight because of them. `They look the same`: no -- body.view differs and the "
            "rendered page is visibly taller with the break. `The storage layer converges "
            "too`: no -- sending the stored bytes back through the REST update leaves `<p />` "
            "as `<p />`. The convergence belongs to the editor alone.\n\n"
            "So this is not equivalence and is not called it. The receipt says the first "
            "publish changes presentation, and says how many places."
        ),
        evidence=(
            _EMPTY_PARAGRAPH_EVIDENCE + " The convergence itself was observed in the browser on "
            "a sandbox page: opened, saved with no edit, stored form became <p><br /></p> at v2. "
            "A REST no-op does not reproduce it on the same page (converged_via_rest: false), "
            "which is why the basis names the editor and not the server. Extended 2026-07-31 to "
            "the leading position and to a macro neighbour on the same instrument, each against "
            "a between-blocks control that converged the same way. Trailing is still unobserved "
            "and still excluded."
        ),
        verified_against="cfxmark 0.6.0",
    ),
    # Same signature, no scope: everywhere the observation above was NOT made. A
    # single rule covering both would be the appearance argument wearing the
    # convergence argument's clothes, since `<p />` is stored identically in a
    # table cell and a list item and nobody has watched the editor there.
    StructureRule(
        code="empty-paragraph-normalised",
        element="p",
        removed_children=(),
        added_children=("br",),
        verdict=NAMED_LOSS,
        context=(
            "an empty paragraph regenerated from Markdown, anywhere the editor-save "
            "convergence has not been observed: inside a list item, a table cell, a "
            "blockquote or a macro, leading or trailing, or beside a macro. 18 of the 19 "
            "occurrences on the 24 adoptable RLM pages are the leading one before the page's "
            "first macro, so this rule is the corpus's common case and not the fallback"
        ),
        rationale=(
            "Markdown has one spelling, `<br>`, for two things the server keeps apart and "
            "renders apart, so an author who had an empty paragraph gets a blank line "
            "instead. Nameable, countable and visible -- a loss to consent to, not one to "
            "excuse and not one to give up on classifying.\n\n"
            "Not a converter gap: there is nothing cfxmark could re-emit without a Markdown "
            "format change, because the format itself has only the one spelling."
        ),
        evidence=(
            _EMPTY_PARAGRAPH_EVIDENCE + " A page sent beginning with a macro comes back "
            "beginning with that macro, so the leading <p /> is the author's and not something "
            "the server inserted -- which is what makes losing it a loss."
        ),
        verified_against="cfxmark 0.6.0",
    ),
)


def _rule_index(
    rules: tuple[AttributeRule, ...],
) -> dict[tuple[str, str], tuple[AttributeRule, ...]]:
    index: dict[tuple[str, str], tuple[AttributeRule, ...]] = defaultdict(tuple)
    for rule in rules:
        index[(rule.element, rule.attribute)] += (rule,)
    return dict(index)


_ATTRIBUTE_INDEX = _rule_index(ATTRIBUTE_RULES)


def _structure_index(
    rules: tuple[StructureRule, ...],
) -> dict[tuple[str, tuple[str, ...], tuple[str, ...]], tuple[StructureRule, ...]]:
    """Signature -> the rules sharing it, most specific first.

    A tuple rather than one rule because the same signature is the same event in
    places whose justifications differ. Scoped rules sort ahead of unscoped ones so
    that the unscoped rule is what is left when no scope admits the node, never
    something that shadows a scope before it was asked.
    """

    index: dict[tuple[str, tuple[str, ...], tuple[str, ...]], tuple[StructureRule, ...]] = defaultdict(tuple)
    for rule in sorted(rules, key=lambda item: item.scope is None):
        index[(rule.element, rule.removed_children, rule.added_children)] += (rule,)
    return dict(index)


_STRUCTURE_INDEX = _structure_index(STRUCTURE_RULES)

# Keys that name a family rather than a value. Each covers a wide range, some of
# it harmless and some not, so a rule keyed here licenses whatever arrives next.
_FAMILY_KEYS = frozenset({"style", "class"})


def _is_family_key(name: str) -> bool:
    return name in _FAMILY_KEYS or "*" in name or "?" in name or name.startswith("data-")


def validate_registry(
    attribute_rules: tuple[AttributeRule, ...] = (),
    structure_rules: tuple[StructureRule, ...] = (),
    identity_bearing: frozenset[tuple[str, str]] = IDENTITY_BEARING,
) -> None:
    """Refuse a registry that cannot be re-checked, or that excuses identity loss.

    Defaults are empty so a caller must pass what it wants checked. Defaulting to
    the live registry would let a test that forgot to pass its forged rule pass
    while checking nothing at all.
    """

    for rule in attribute_rules:
        where = f"{rule.element}@{rule.attribute}"
        if rule.verdict not in {EQUIVALENT, CONVERTER_GAP, NAMED_LOSS}:
            raise RegistryViolation(f"{where}: a rule may not register the {rule.verdict!r} verdict")
        # Checked before the family test: an empty attribute is falsy and would
        # otherwise skip every check below it.
        if not (rule.attribute or "").strip():
            raise RegistryViolation(f"{rule.element}: attribute is required -- lookups key on an exact name")
        if _is_family_key(rule.element) or _is_family_key(rule.attribute):
            raise RegistryViolation(
                f"{where}: keys must be exact names, never families. `style` is already split "
                "per CSS property, so the key is style/<property>."
            )
        for name in ("context", "relation", "rationale", "evidence", "verified_against"):
            if not (getattr(rule, name) or "").strip():
                raise RegistryViolation(f"{where}: {name} is required -- a rule nobody can re-check is a guess")
        if "->" not in rule.relation:
            raise RegistryViolation(
                f"{where}: relation must name a direction (e.g. 'present -> absent'); a rule "
                "licensing a drop must not also license an invention"
            )
        if (
            rule.verdict == EQUIVALENT
            and (rule.element, rule.attribute) in identity_bearing
            and not (rule.controlled_experiment or "").strip()
        ):
            raise RegistryViolation(
                f"{where}: identity-bearing fields need a controlled experiment before they can "
                "be called equivalent. Shape is not use -- this is exactly how ac:macro-id was "
                "wrongly excused."
            )
        if rule.verdict == EQUIVALENT and rule.when_before is None:
            raise RegistryViolation(
                f"{where}: an equivalent rule must name the stored values it covers "
                "(when_before). Presence is not value -- 'present -> absent' licenses dropping "
                "a harmless default and downgrading a real one in the same words, and only the "
                "second loses data."
            )
        if rule.verdict == EQUIVALENT and rule.when_before is not None and None in rule.when_before:
            # Covering "the source had no value" leaves the regenerated value
            # unconstrained, so the same words license writing the server's own default
            # and writing anything else over the macro. The check above is this one's
            # mirror; only one of them existed.
            if rule.when_after is None or None in rule.when_after:
                raise RegistryViolation(
                    f"{where}: an equivalent rule covering an addition must name the values it "
                    "writes (when_after), and may not leave absent among them. `when_before` "
                    "constrains nothing here -- 'the source had no value' is true of adding the "
                    "server's default and of adding a value nothing was measured about."
                )
        if rule.verdict == EQUIVALENT and rule.when_after is None:
            # And the mirror of the mirror. The check above fires only when `when_before`
            # covers absence, so a rule naming a concrete source value left the written
            # value unconstrained -- and `_matches(None, ...)` is True, so the registered
            # `present "1" -> absent` graded `"1" -> "2"` and `"1" -> "99-EVIL"`
            # equivalent too. That is the failure the module header names: one rule
            # licensing a harmless drop and a real downgrade in the same words, with only
            # the second losing data. Both directions must say what they write.
            raise RegistryViolation(
                f"{where}: an equivalent rule must name the regenerated values it writes "
                "(when_after). Leaving it unset licenses 'present \"1\" -> absent' and "
                "'present \"1\" -> anything at all' in the same words, and the relation "
                "field is prose the validator cannot enforce."
            )

    for structure in structure_rules:
        where = f"{structure.element}#structure"
        if structure.verdict not in {EQUIVALENT, CONVERTER_GAP, NAMED_LOSS, PLATFORM_EDITOR_CANONICALIZATION}:
            raise RegistryViolation(f"{where}: a rule may not register the {structure.verdict!r} verdict")
        if _is_family_key(structure.element):
            raise RegistryViolation(f"{where}: keys must be exact element names")
        if not structure.removed_children and not structure.added_children:
            raise RegistryViolation(
                f"{where}: a structure rule that names neither a loss nor an addition matches "
                "every structural change on this element"
            )
        for name in ("context", "rationale", "evidence", "verified_against"):
            if not (getattr(structure, name) or "").strip():
                raise RegistryViolation(f"{where}: {name} is required")
        if structure.scope is not None and structure.scope not in STRUCTURE_SCOPES:
            raise RegistryViolation(
                f"{where}: scope {structure.scope!r} is not a registered predicate, so the rule "
                "would either match nothing or match everything depending on the lookup -- "
                "neither is a scope"
            )
        if structure.verdict == PLATFORM_EDITOR_CANONICALIZATION:
            # This verdict says "the platform does this to the page itself", which is a
            # claim about behaviour rather than a reading of two documents. Both fields
            # exist so it cannot be asserted in general: `basis` is the one observation
            # it rests on, `scope` is the one shape that observation was made in. Without
            # them the verdict becomes a licence to rewrite any whitespace and call it
            # canonicalization -- which is the appearance argument this release measured
            # false.
            for name in ("basis", "scope"):
                if not (getattr(structure, name) or "").strip():
                    raise RegistryViolation(
                        f"{where}: {name} is required for a {PLATFORM_EDITOR_CANONICALIZATION!r} "
                        "verdict. The claim is that the platform replaces this form on save, and "
                        "one observation licenses one shape -- not a general permission."
                    )


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


def _element_of(path: tuple[str, ...]) -> str:
    """The tag the last path segment names, without its positional index."""

    if not path:
        return ""
    return path[-1].split("[", 1)[0]


def _parent_of(path: tuple[str, ...]) -> tuple[str, ...]:
    return path[:-1]


@dataclass(frozen=True)
class Difference:
    """One leaf that S0 has and C0 does not, or holds differently."""

    semantic_path: tuple[str, ...]
    element: str
    field: str
    attribute_name: str | None
    direction: str  # "removed" | "added" | "changed"
    before: str | None  # value fingerprint, never the value itself
    after: str | None

    @property
    def key(self) -> str:
        return f"{self.element}@{self.attribute_name}" if self.attribute_name else f"{self.element}#{self.field}"


@dataclass(frozen=True)
class Finding:
    """One thing that would change, named once with a count.

    A dataclass rather than a dict because every consumer of this reads the same
    four fields, and a `dict[str, object]` makes each of them cast before it can
    use any of them.
    """

    verdict: str
    code: str
    count: int
    direction: str
    #: A few example locations, enough to find the thing without printing every
    #: occurrence of it.
    semantic_paths: tuple[str, ...]


@dataclass(frozen=True)
class Judgement:
    difference: Difference
    verdict: str
    rule: AttributeRule | StructureRule | UnwrapRule | None = None
    #: The element whose change this leaf is part of, when a grouped rule
    #: explained it. Carried rather than re-derived: an unwrap's leaves sit at
    #: two different depths, so deriving the owner from each leaf's own parent
    #: counts one event twice.
    group: tuple[str, ...] | None = None


@dataclass
class CompatibilityReport:
    """What Markdown can and cannot hold for one page."""

    judgements: tuple[Judgement, ...] = ()
    candidate_markdown: str = ""
    candidate_storage: str = ""
    #: Set when the conversion itself failed. A page we cannot even regenerate is
    #: not a page we may publish from Markdown, so this classifies as blocked
    #: rather than raising -- the caller needs a verdict, not an exception.
    conversion_error: str | None = None
    _counts: dict[str, int] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        counts: dict[str, int] = defaultdict(int)
        for item in self.judgements:
            counts[item.verdict] += 1
        self._counts = dict(counts)

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def classification(self) -> str:
        """The page's class -- always the class its worst finding demands.

        Severity here means "how much must a person do", which is why an unknown
        outranks a converter gap: an unknown needs somebody to go and find out
        what it is, while a gap already has an owner and a fix.
        """

        if self.conversion_error is not None:
            return UNKNOWN_BLOCKED
        if self._counts.get(UNKNOWN):
            return UNKNOWN_BLOCKED
        if self._counts.get(CONVERTER_GAP):
            return CONVERTER_GAP_BLOCKED
        if self._counts.get(NAMED_LOSS):
            return NAMED_LOSS_CONSENTABLE
        if self._counts.get(IDENTITY_CARRY):
            return MARKDOWN_IDENTITY_BOUND
        # `PLATFORM_EDITOR_CANONICALIZATION` deliberately does not appear above.
        # It asks nothing of a person, so it must not raise the classification --
        # otherwise the release's own converter output would grade every page it
        # touched as needing consent. It is still reported as a finding, because
        # the first publish does change how the page looks until the platform
        # catches up, and that is disclosed rather than absorbed.
        return MARKDOWN_LOSSLESS

    @property
    def requires_identity_carry(self) -> bool:
        """Whether publishing this page from Markdown must supply a base artifact.

        Regenerating without one drops every identity in `IDENTITY_BEARING`, and
        the server re-assigns rather than restoring them, so each macro's
        comments and attachments detach. There is no way to notice afterwards.
        """

        return bool(self._counts.get(IDENTITY_CARRY))

    def findings(self) -> tuple[Finding, ...]:
        """Findings grouped for display: one row per (verdict, key), with a count.

        Grouped because "24 macro ids would be dropped" is a sentence a person
        can act on and twenty-four separate rows is not.
        """

        grouped: dict[tuple[str, str], list[Judgement]] = defaultdict(list)
        for item in self.judgements:
            # A rule that explained a whole event reports the event. Only leaves
            # nobody has grouped fall back to naming themselves.
            code = getattr(item.rule, "code", None) or item.difference.key
            grouped[(item.verdict, code)].append(item)
        rows: list[Finding] = []
        for (verdict, key), items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            first = items[0].difference
            groups = {item.group for item in items if item.group is not None}
            # One event per element the rule matched, not one per leaf it moved.
            occurrences = len(groups) if groups else len(items)
            rows.append(
                Finding(
                    verdict=verdict,
                    code=key,
                    count=occurrences,
                    direction=first.direction,
                    semantic_paths=tuple("/".join(item.difference.semantic_path) for item in items[:8]),
                )
            )
        return tuple(rows)


def _child_lists(
    identities: Iterable[tuple[tuple[str, ...], str, str | None, int]],
) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Every parent's full child-tag list, taken from its element leaves.

    A structure rule keys on the *whole* list on both sides rather than on what
    moved, for the same reason the unwrap rules name both tags: an empty
    paragraph gaining a `br` and a paragraph of prose gaining one are not the
    same event, and "gained a br" alone cannot tell them apart.
    """

    children: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for path, leaf_field, _attribute, _ordinal in identities:
        if leaf_field != "element":
            continue
        children[_parent_of(path)].append(_element_of(path))
    return {parent: tuple(sorted(tags)) for parent, tags in children.items()}


def _structure_rule_for(
    signature: tuple[str, tuple[str, ...], tuple[str, ...]],
    element: ET._Element | None,
) -> StructureRule | None:
    """The rule that speaks to this node, not merely to its signature.

    A scoped rule is skipped unless its predicate admits the *source* element, and
    skipped outright when the element could not be located -- a scope that cannot
    be evaluated has not been satisfied. That direction is deliberate: the
    unscoped sibling rule is the stricter verdict, so failing to evaluate a scope
    costs a page a consent prompt rather than granting it a silent rewrite.
    """

    for rule in _STRUCTURE_INDEX.get(signature, ()):
        if rule.scope is None:
            return rule
        if element is None:
            continue
        predicate = STRUCTURE_SCOPES.get(rule.scope)
        if predicate is not None and predicate(element):
            return rule
    return None


def _grouped_matches(
    differences: list[Difference],
    children_before: dict[tuple[str, ...], tuple[str, ...]],
    children_after: dict[tuple[str, ...], tuple[str, ...]],
    source_elements: dict[tuple[str, ...], ET._Element] | None = None,
) -> dict[int, tuple[UnwrapRule | StructureRule, tuple[str, ...]]]:
    """Which differences belong to one event rather than standing alone.

    Two kinds of group live here, and both exist because a single structural
    change arrives as several leaves that no per-leaf rule can see the whole of.

    Returns a map from position in `differences` to the rule that explains it, so
    the five leaves one unwrap produces are judged together instead of five times
    over. A parent only qualifies when *every* child it lost is the wrapper tag
    and *every* child it gained is one the rule permits -- a cell that also lost
    something else is not this event and keeps its unknowns.
    """

    lost: dict[tuple[str, ...], list[str]] = defaultdict(list)
    gained: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for difference in differences:
        if difference.field != "element":
            continue
        parent = _parent_of(difference.semantic_path)
        if difference.direction == "removed":
            lost[parent].append(difference.element)
        elif difference.direction == "added":
            gained[parent].append(difference.element)

    matched: dict[tuple[str, ...], UnwrapRule | StructureRule] = {}
    # Structure rules first: they key on the whole child list on both sides, so
    # they are the more specific claim, and an unwrap rule must not shadow one.
    for parent in set(children_before) | set(children_after):
        structure = _structure_rule_for(
            (
                _element_of(parent),
                children_before.get(parent, ()),
                children_after.get(parent, ()),
            ),
            (source_elements or {}).get(parent),
        )
        if structure is not None:
            matched[parent] = structure

    for parent, children in lost.items():
        if parent in matched:
            continue
        distinct = set(children)
        if len(distinct) != 1:
            continue
        rule = _UNWRAP_INDEX.get((_element_of(parent), next(iter(distinct))))
        if rule is None:
            continue
        if any(tag not in rule.allowed_added for tag in gained.get(parent, ())):
            continue
        matched[parent] = rule

    covered: dict[int, tuple[UnwrapRule | StructureRule, tuple[str, ...]]] = {}
    for index, difference in enumerate(differences):
        own = difference.semantic_path
        parent_rule = matched.get(own)
        # The parent's own text and topology moved because its children did.
        if parent_rule is not None and difference.field in {"text", "topology"}:
            covered[index] = (parent_rule, own)
            continue
        owner_path = _parent_of(own)
        owner = matched.get(owner_path)
        if owner is None:
            continue
        if isinstance(owner, UnwrapRule):
            # Leaves of the wrapper itself, and of whatever replaced it.
            if difference.element in {owner.child, *owner.allowed_added}:
                covered[index] = (owner, owner_path)
        elif difference.element in {*owner.removed_children, *owner.added_children}:
            covered[index] = (owner, owner_path)
    return covered


def classify(
    difference: Difference,
    grouped: tuple[UnwrapRule | StructureRule, tuple[str, ...]] | None = None,
) -> Judgement:
    if grouped is not None:
        rule, owner = grouped
        return Judgement(difference, rule.verdict, rule, group=owner)
    if difference.field == "attribute" and difference.attribute_name is not None:
        key = (difference.element, difference.attribute_name)
        for attribute_rule in _ATTRIBUTE_INDEX.get(key, ()):
            # A rule that does not cover this stored value has not spoken about
            # this difference at all, so the search continues and, finding
            # nothing, ends at `unknown`. Falling back to "the rule for this
            # attribute" would be the presence-not-value mistake.
            #
            # Both sides are offered. A rule naming only `when_before` ignores the
            # second argument exactly as before; one naming `when_after` could not
            # decide anything without it.
            if attribute_rule.covers(difference.before, difference.after):
                return Judgement(difference, attribute_rule.verdict, attribute_rule)
        if key in NAMED_LOSS_ATTRIBUTES:
            return Judgement(difference, NAMED_LOSS)
        # Only a *dropped* identity is carryable: the original still holds the
        # value, so something can put it back. An invented or altered identity is
        # a different event -- nobody asked for a new id, and matching a changed
        # one would mean deciding which of two identities is correct.
        if key in IDENTITY_BEARING and difference.direction == "removed":
            return Judgement(difference, IDENTITY_CARRY)
        return Judgement(difference, UNKNOWN)

    return Judgement(difference, UNKNOWN)


def assess_markdown_compatibility(
    storage: str,
    *,
    options: ConversionOptions,
    macros: MacroRegistry | None = None,
) -> CompatibilityReport:
    """Measure what Markdown cannot hold for this stored page.

    Regenerates the page from its own Markdown with no help from the original --
    no base artifact, no presentation re-attachment, no splice source -- and
    reports every leaf the round trip did not reproduce.

    This is a *measurement*, not a permission. It says what would be lost;
    whether to publish anyway is the caller's decision and, for anything but
    `markdown_lossless`, the author's.

    `options` is required and has no default on purpose. The answer is only true
    for the profile the publish path will actually use, and the profiles do not
    agree about anything interesting. A macro inside a table cell survives the
    round trip under the default profile and is reduced to the text `cfx:info`
    under `editable` -- the profile every atls path uses. A default here would
    have silently reported "lossless" for a page whose macro was about to lose
    its body.
    """

    from cfxmark.api import to_cfx_artifact, to_md_artifact

    try:
        markdown = to_md_artifact(storage, options=options, macros=macros).markdown
        candidate = to_cfx_artifact(markdown, options=options, macros=macros).xhtml
    except Exception as error:  # noqa: BLE001 - any failure means "cannot publish from Markdown"
        return CompatibilityReport(conversion_error=f"{type(error).__name__}: {error}")

    report = assess_candidate(storage, candidate)
    return CompatibilityReport(
        judgements=report.judgements,
        candidate_markdown=markdown,
        candidate_storage=candidate,
    )


def canonicalization_sites(source_storage: str) -> frozenset[tuple[str, ...]]:
    """The stored nodes a `PLATFORM_EDITOR_CANONICALIZATION` rule accounts for.

    Exported because the classification and the *consent policy* live in different
    products, and the shape must not be defined twice. cfxmark knows which nodes
    the platform's own editor would rewrite; whether that means a person has to be
    asked is a question about a workflow, and belongs to the caller. What the
    caller must not do is re-derive the shape -- the corpus's common case is a
    leading empty paragraph before the first macro, which is a real loss, and a
    second implementation drifting onto it would silently stop asking about 18 of
    19 occurrences.

    Answers about the *stored* page. A caller pairs each path with the reported
    change at that path; a path alone does not say what happened there.
    """

    sites: set[tuple[str, ...]] = set()
    for path, element in extract_storage_source(source_storage).elements:
        for rules in _STRUCTURE_INDEX.values():
            for rule in rules:
                if rule.verdict != PLATFORM_EDITOR_CANONICALIZATION or rule.scope is None:
                    continue
                if _element_of(path) != rule.element:
                    continue
                predicate = STRUCTURE_SCOPES.get(rule.scope)
                if predicate is not None and predicate(element):
                    sites.add(path)
    return frozenset(sites)


def assess_candidate(remote_storage: str, candidate_storage: str) -> CompatibilityReport:
    """Classify every difference between two storage documents.

    Used where the two are *supposed* to be the same document -- comparing what
    was sent against what came back, or a regeneration against its original.
    There, any difference is a finding.

    **Not a measure of what an edit costs.** An author's intentional change is a
    difference too, and nothing here distinguishes "the converter dropped this"
    from "the author deleted it". Telling those apart needs to know which edit
    claims which leaf, which is the ownership proof's question and not this one.
    A first attempt at using this for approval reported every edited paragraph as
    an unexplained loss.
    """

    remote = extract_storage_source(remote_storage)
    before = {storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in remote.leaves}
    after = {
        storage_leaf_identity(leaf): leaf.value_fingerprint for leaf in extract_storage_source(candidate_storage).leaves
    }
    # The remote side, because a scope predicate asks what the *stored* page looks
    # like -- what the candidate did with it is the thing being judged, not the
    # thing that licenses the judgement.
    source_elements = dict(remote.elements)

    differences: list[Difference] = []
    for identity, value in before.items():
        path, leaf_field, attribute_name, _ordinal = identity
        if identity not in after:
            direction, other = "removed", None
        elif after[identity] != value:
            direction, other = "changed", after[identity]
        else:
            continue
        differences.append(
            Difference(
                semantic_path=path,
                element=_element_of(path),
                field=leaf_field,
                attribute_name=attribute_name,
                direction=direction,
                before=value,
                after=other,
            )
        )
    for identity, value in after.items():
        if identity in before:
            continue
        path, leaf_field, attribute_name, _ordinal = identity
        differences.append(
            Difference(
                semantic_path=path,
                element=_element_of(path),
                field=leaf_field,
                attribute_name=attribute_name,
                direction="added",
                before=None,
                after=value,
            )
        )

    grouped = _grouped_matches(differences, _child_lists(before), _child_lists(after), source_elements)
    return CompatibilityReport(
        judgements=tuple(classify(item, grouped.get(index)) for index, item in enumerate(differences)),
        candidate_storage=candidate_storage,
    )


__all__ = [
    "ATTRIBUTE_RULES",
    "CONVERTER_GAP",
    "CONVERTER_GAP_BLOCKED",
    "EQUIVALENT",
    "IDENTITY_BEARING",
    "IDENTITY_CARRY",
    "MARKDOWN_IDENTITY_BOUND",
    "MARKDOWN_LOSSLESS",
    "NAMED_LOSS",
    "NAMED_LOSS_ATTRIBUTES",
    "NAMED_LOSS_CONSENTABLE",
    "PLATFORM_EDITOR_CANONICALIZATION",
    "STRUCTURE_RULES",
    "STRUCTURE_SCOPES",
    "UNKNOWN",
    "UNKNOWN_BLOCKED",
    "AttributeRule",
    "CompatibilityReport",
    "Difference",
    "Finding",
    "Judgement",
    "RegistryViolation",
    "StructureRule",
    "UNWRAP_RULES",
    "UnwrapRule",
    "assess_candidate",
    "assess_markdown_compatibility",
    "canonicalization_sites",
    "validate_registry",
]
