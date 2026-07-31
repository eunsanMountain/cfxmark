"""What Markdown can hold, and the guards that keep the answer honest.

The registry is the one place a wrong entry destroys data silently. Every other
mistake here produces a wrong number; a rule that calls a real loss `equivalent`
produces a green light, and the loss lands on a live page.

So these tests do two jobs. The first is to check the registry we ship. The
second, and the more important one, is to show the checks *can fail* -- a guard
that has never been seen to reject anything is not evidence the registry is
sound, only evidence that the guard ran.

The concrete failure this exists to prevent already happened. `ac:macro-id` is
uuid-shaped, unique per page and never hand-authored, so it was once registered
as harmless. Confluence uses it as an instance identity key, and reusing it is
what keeps a macro's comments, permissions and attachments attached.
"""

from __future__ import annotations

import pytest

import cfxmark
from cfxmark.compatibility import (
    ATTRIBUTE_RULES,
    EQUIVALENT,
    IDENTITY_BEARING,
    IDENTITY_CARRY,
    MARKDOWN_IDENTITY_BOUND,
    MARKDOWN_LOSSLESS,
    NAMED_LOSS,
    NAMED_LOSS_CONSENTABLE,
    STRUCTURE_RULES,
    UNKNOWN,
    UNKNOWN_BLOCKED,
    UNWRAP_RULES,
    AttributeRule,
    RegistryViolation,
    assess_candidate,
    assess_markdown_compatibility,
    validate_registry,
)

# Every atls path converts with this profile, and the profiles disagree about
# things that matter, so every assessment here uses the one the product uses.
EDITABLE = cfxmark.ConversionOptions(profile="editable")

MACRO = (
    '<ac:structured-macro ac:name="info" ac:schema-version="1"{extra}>'
    "<ac:rich-text-body><p>note body</p></ac:rich-text-body>"
    "</ac:structured-macro>"
)


def assess(storage: str):
    return assess_markdown_compatibility(storage, options=EDITABLE)


def _rule(**overrides) -> AttributeRule:
    base = {
        "element": "ac:structured-macro",
        "attribute": "ac:schema-version",
        "verdict": EQUIVALENT,
        "context": "a macro regenerated without a remote source",
        "relation": 'present "1" -> absent',
        "rationale": "the server re-supplies its own default",
        "evidence": "live 2026-07-29",
        "verified_against": "cfxmark 0.5.1",
        "controlled_experiment": "bin/experiment.py server_defaults",
        "when_before": ("1",),
    }
    base.update(overrides)
    return AttributeRule(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The registry we ship
# --------------------------------------------------------------------------


def test_the_shipped_registry_is_valid() -> None:
    validate_registry(ATTRIBUTE_RULES, STRUCTURE_RULES)


def test_macro_id_is_not_in_the_registry() -> None:
    """Named explicitly rather than left to the schema check, because this is the
    entry that was wrong and the one most likely to be re-added by someone who
    notices it is uuid-shaped."""

    assert ("ac:structured-macro", "ac:macro-id") not in {(rule.element, rule.attribute) for rule in ATTRIBUTE_RULES}
    assert ("ac:structured-macro", "ac:macro-id") in IDENTITY_BEARING


def test_no_shipped_rule_is_equivalent_on_an_identity_field() -> None:
    for rule in ATTRIBUTE_RULES:
        if rule.verdict == EQUIVALENT:
            assert (rule.element, rule.attribute) not in IDENTITY_BEARING


# --------------------------------------------------------------------------
# Showing the guards can fail
# --------------------------------------------------------------------------


def test_an_identity_field_cannot_be_called_equivalent_without_an_experiment() -> None:
    """The exact mistake, replayed. Without this the guard is decoration."""

    forged = _rule(
        attribute="ac:macro-id",
        controlled_experiment=None,
        rationale="looks like a server-assigned id, so surely it is regenerated",
    )
    with pytest.raises(RegistryViolation, match="controlled experiment"):
        validate_registry((forged,))


def test_naming_a_controlled_experiment_is_what_unlocks_it() -> None:
    """Not a blanket ban -- a demand for evidence.

    `when_after` is supplied because an equivalent rule must now name what it writes in
    both directions, not only when `when_before` covers absence. Leaving it unset was how
    the shipped `present "1" -> absent` rule came to grade `"1" -> "2"` equivalent as well.
    """

    validate_registry((_rule(attribute="ac:macro-id", when_before=("abc-123",), when_after=(None,)),))


def test_an_equivalent_rule_must_say_which_values_it_covers() -> None:
    """Presence is not value.

    `schema-version` is the case that forced this. Dropping a stored "1" costs
    nothing because the server stamps "1" back; dropping a stored "2" downgrades
    the macro, because the server stamps "1" over it. One rule reading
    "present -> absent" licenses both, and only the second loses data."""

    with pytest.raises(RegistryViolation, match="when_before"):
        validate_registry((_rule(when_before=None),))


@pytest.mark.parametrize("name", ["context", "relation", "rationale", "evidence", "verified_against"])
def test_every_required_field_is_actually_required(name: str) -> None:
    """A rule missing any of these cannot be re-checked later, which makes it a
    claim rather than a fact."""

    with pytest.raises(RegistryViolation, match=name):
        validate_registry((_rule(**{name: "   "}),))


@pytest.mark.parametrize("attribute", ["style", "class", "data-highlight-colour", "ac:*", ""])
def test_family_keys_are_refused(attribute: str) -> None:
    """`style` and `class` cover harmless and harmful values alike, so a rule keyed
    on the attribute alone licenses whatever arrives next."""

    with pytest.raises(RegistryViolation):
        validate_registry((_rule(attribute=attribute),))


def test_a_relation_without_a_direction_is_refused() -> None:
    """A rule licensing a drop must not also license an invention."""

    with pytest.raises(RegistryViolation, match="direction"):
        validate_registry((_rule(relation="differs"),))


def test_a_rule_may_not_register_the_unknown_verdict() -> None:
    """`unknown` is what happens when no rule matches. Registering it would be an
    entry that looks like a decision and changes nothing."""

    with pytest.raises(RegistryViolation, match="unknown"):
        validate_registry((_rule(verdict=UNKNOWN),))


def test_validate_registry_checks_nothing_by_default() -> None:
    """Defaults are empty on purpose. If they defaulted to the live registry, a
    mutation test that forgot to pass its forged rule would pass while testing
    nothing at all."""

    validate_registry()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_a_page_with_nothing_to_lose_is_lossless() -> None:
    report = assess("<p>hello</p><ul><li>one</li><li>two</li></ul>")
    assert report.classification == MARKDOWN_LOSSLESS
    assert report.requires_identity_carry is False


def test_dropping_the_default_schema_version_costs_nothing() -> None:
    """Measured, not reasoned: Confluence stamps `schema-version="1"` onto any
    macro it saves without one, so a regeneration that omits the attribute stores
    the same value.

    This was once called a converter gap on the strength of "cfxmark reads it and
    does not re-emit it", which is true and beside the point -- the question is
    what ends up stored."""

    verdicts = {item.verdict for item in assess(MACRO.format(extra="")).judgements}
    assert verdicts <= {EQUIVALENT}


def test_a_macro_id_makes_the_page_identity_bound() -> None:
    """Not lossless: publishing without carrying the id detaches the macro's
    comments and attachments, and nothing visible changes to warn anyone."""

    report = assess(MACRO.format(extra=' ac:macro-id="7f3a-0001"'))
    assert report.classification == MARKDOWN_IDENTITY_BOUND
    assert report.requires_identity_carry is True
    assert IDENTITY_CARRY in report.counts


def test_identity_carry_does_not_claim_the_correspondence_is_unique() -> None:
    """Two byte-identical macros differing only in id classify exactly like one
    unique macro would. The comparison walks positionally and has no way to say
    which id belongs to which.

    Pinned so nobody reads `identity_bound` as "safe to carry". Classification
    answers *what is this difference*; whether a carry is safe is the carrying
    mechanism's question, and it must refuse an ambiguous one."""

    ambiguous = MACRO.format(extra=' ac:macro-id="aaaa-0001"') + MACRO.format(extra=' ac:macro-id="bbbb-0002"')
    unique = MACRO.format(extra=' ac:macro-id="aaaa-0001"')
    assert assess(ambiguous).classification == assess(unique).classification


def test_a_cell_background_is_a_named_consentable_loss() -> None:
    report = assess(
        "<table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000"><p>a</p></td></tr></tbody></table>'
    )
    assert report.classification == NAMED_LOSS_CONSENTABLE


def test_an_unregistered_attribute_is_unknown() -> None:
    """Default deny. This is the property the whole design rests on."""

    report = assess('<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">a</td></tr></tbody></table>')
    assert report.classification == UNKNOWN_BLOCKED


# --------------------------------------------------------------------------
# Unwrapped block wrappers -- one event, five leaves
# --------------------------------------------------------------------------


def test_a_paragraph_inside_a_table_cell_is_a_named_loss() -> None:
    """Markdown table cells hold inline content only, so a cell's paragraphs
    cannot survive as paragraphs. Real, visible, countable -- so the author can
    be shown it and asked."""

    report = assess(
        "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td><p>one</p><p>two</p></td></tr></tbody></table>"
    )
    assert report.classification == NAMED_LOSS_CONSENTABLE


def test_a_paragraph_inside_a_list_item_is_now_held_exactly() -> None:
    """The gap this rule was written for, now closed.

    Markdown always *could* hold a paragraph inside a list item -- that is a
    loose list -- so this was our limit rather than the format's, and the rule
    said so by calling it a converter gap rather than a named loss. The
    converter now spells tight and loose apart, so the shape survives and there
    is nothing left to report."""

    assert assess("<ul><li><p>alpha</p></li></ul>").classification == MARKDOWN_LOSSLESS
    assert assess("<ul><li>alpha</li></ul>").classification == MARKDOWN_LOSSLESS


def test_a_list_that_mixes_the_two_forms_now_round_trips() -> None:
    """This used to be the converter gap, and C3 closed it.

    There is no *CommonMark* spelling for "these two items are paragraphs and that one is
    not", which is why this was reported rather than handled: `List.loose` is a list-level
    property and storage carries the shape per item, so a mixed list flattened to tight and
    the wrapped items lost their `<p>`.

    But the two `cfxmark:list` markers were already invented spellings for shapes CommonMark
    cannot express, so a third naming *which* items were wrapped is the same device rather
    than a new concept. Both directions of the damage were live at some stage: promoting the
    whole list to loose added a paragraph to items nobody had touched, and leaving it tight
    dropped one. Neither happens now, and neither is silent.

    The registry rule stays. It is still the right answer for a shape no marker describes,
    and `test_the_unwrap_rules_name_both_tags` keeps it honest.
    """

    report = assess("<ul><li><p>alpha</p></li><li>bravo</li></ul>")
    assert report.classification == MARKDOWN_LOSSLESS
    assert not any(row.code == "list-item-paragraph-unwrapped" for row in report.findings())


def test_the_unwrap_rules_name_both_tags() -> None:
    """A rule keyed on "this parent's children changed" would also cover a cell
    whose macro was destroyed. Both ends are named so it cannot."""

    for rule in UNWRAP_RULES:
        assert rule.parent and rule.child


def test_a_macro_destroyed_inside_a_table_cell_must_stay_blocked() -> None:
    """The worst round trip found so far, and the one the default-deny design
    exists for.

    A macro inside a table cell does not survive the trip to Markdown: the cell
    becomes the literal text `cfx:info` and the macro's body is gone. The
    identical macro outside a table round-trips perfectly, so this is about the
    cell, not the macro.

    cfxmark's own migration report returns no diagnostics and
    `consent_required=False` for it -- the tool that is supposed to say what an
    author loses says nothing was lost while a macro's body is destroyed.

    MUST NEVER STOP BLOCKING. If a later relaxation of the cell rules lets this
    through, that relaxation destroys content."""

    storage = (
        "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>"
        + MACRO.format(extra="")
        + "</td></tr></tbody></table>"
    )
    report = assess(storage)
    assert report.classification == UNKNOWN_BLOCKED
    assert "note body" not in report.candidate_storage


def test_a_cell_that_lost_something_else_as_well_is_not_an_unwrap() -> None:
    """The unwrap rule requires that *every* child the parent lost is the wrapper
    tag. A cell that also lost a macro is a different event and keeps its
    unknowns."""

    storage = (
        "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td><p>one</p>"
        + MACRO.format(extra="")
        + "</td></tr></tbody></table>"
    )
    assert assess(storage).classification == UNKNOWN_BLOCKED


# --------------------------------------------------------------------------
# Severity, reporting, and the profile
# --------------------------------------------------------------------------


def test_one_unknown_among_many_explained_findings_still_blocks() -> None:
    """A page is only as publishable as its worst finding."""

    storage = (
        MACRO.format(extra=' ac:macro-id="7f3a-0001"')
        + '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">a</td></tr></tbody></table>'
    )
    report = assess(storage)
    assert IDENTITY_CARRY in report.counts
    assert report.classification == UNKNOWN_BLOCKED


def test_findings_are_grouped_with_a_count() -> None:
    """ "24 macro ids would be dropped" is a sentence a person can act on; 24
    separate rows is not."""

    storage = "".join(MACRO.format(extra=f' ac:macro-id="7f3a-{index:04d}"') for index in range(3))
    rows = assess(storage).findings()
    carried = [row for row in rows if row.verdict == IDENTITY_CARRY]
    assert len(carried) == 1
    assert carried[0].count == 3
    assert carried[0].code == "ac:structured-macro@ac:macro-id"


def test_the_conversion_profile_changes_the_answer() -> None:
    """Why `options` has no default.

    The same page is lossless under the default profile and blocked under
    `editable`, because only the second converts the macro instead of carrying it
    through opaquely. `editable` is what every atls path uses, so a default here
    would have quietly reported "lossless" for a page about to lose a macro
    body."""

    storage = (
        "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>"
        + MACRO.format(extra="")
        + "</td></tr></tbody></table>"
    )
    assert assess_markdown_compatibility(storage, options=EDITABLE).classification == UNKNOWN_BLOCKED
    default = assess_markdown_compatibility(storage, options=cfxmark.DEFAULT_OPTIONS)
    assert default.classification == MARKDOWN_LOSSLESS


def test_a_conversion_failure_classifies_as_blocked_rather_than_raising() -> None:
    """A page we cannot even regenerate is certainly not one to publish from
    Markdown. The caller needs a verdict, not an exception, because this runs on
    every pull."""

    report = assess_markdown_compatibility("<p>unclosed", options=EDITABLE)
    assert report.classification == UNKNOWN_BLOCKED
    assert report.conversion_error


def test_a_named_loss_outranks_an_identity_on_the_same_page() -> None:
    """Severity is "how much must a person do".

    A macro id needs a mechanism and nobody's attention; a dropped cell
    background needs the author to agree to lose it. Put both on one page and the
    page must report the one that needs a person.

    Built from two pages whose classifications are asserted separately, so this
    cannot pass by accident if either half stops working."""

    identity_only = MACRO.format(extra=' ac:macro-id="7f3a-0001"')
    loss_only = (
        "<table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
    )
    assert assess(identity_only).classification == MARKDOWN_IDENTITY_BOUND
    assert assess(loss_only).classification == NAMED_LOSS_CONSENTABLE
    assert assess(identity_only + loss_only).classification == NAMED_LOSS_CONSENTABLE


def test_an_unknown_outranks_a_named_loss_on_the_same_page() -> None:
    """An unknown needs somebody to go and find out what it is, which is strictly
    more than agreeing to a loss already named."""

    loss_only = (
        "<table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000">a</td></tr></tbody></table>'
    )
    unknown_only = '<table><thead><tr><th>h</th></tr></thead><tbody><tr><td colspan="2">a</td></tr></tbody></table>'
    assert assess(loss_only).classification == NAMED_LOSS_CONSENTABLE
    assert assess(unknown_only).classification == UNKNOWN_BLOCKED
    assert assess(loss_only + unknown_only).classification == UNKNOWN_BLOCKED


# --------------------------------------------------------------------------
# The candidate's cost, which is not the page's forecast
# --------------------------------------------------------------------------


def test_an_authors_edit_is_a_difference_like_any_other() -> None:
    """The limit that stops this being a measure of what an edit costs.

    An intentional change is a difference, and nothing here distinguishes "the
    converter dropped this" from "the author rewrote it". A first attempt at
    deriving approval from this reported every edited paragraph as an unexplained
    loss.

    Telling the two apart needs to know which edit claims which leaf, which is
    the ownership proof's question."""

    from cfxmark.compatibility import assess_candidate

    remote = "<p>alpha</p><p>bravo</p>"
    candidate = "<p>alpha edited</p><p>bravo</p>"
    assert assess_candidate(remote, candidate).classification == UNKNOWN_BLOCKED


def test_a_candidate_that_drops_something_still_reports_it() -> None:
    """Reduced, not disarmed. The same registry judges both."""

    from cfxmark.compatibility import assess_candidate

    remote = (
        "<table><thead><tr><th>h</th></tr></thead>"
        '<tbody><tr><td data-highlight-colour="#ff0000">cell</td></tr></tbody></table>'
    )
    candidate = remote.replace(' data-highlight-colour="#ff0000"', "")
    assert assess_candidate(remote, candidate).classification == NAMED_LOSS_CONSENTABLE


def test_an_identical_candidate_is_lossless() -> None:
    from cfxmark.compatibility import assess_candidate

    storage = MACRO.format(extra=' ac:macro-id="7f3a-0001"')
    assert assess_candidate(storage, storage).classification == MARKDOWN_LOSSLESS


# --------------------------------------------------------------------------
# The empty paragraph: canonicalization inside one narrow shape, named loss
# everywhere else
# --------------------------------------------------------------------------
#
# Basis, and it is not appearance: `<p/>` does not survive a no-edit save in
# Confluence's own editor -- it comes back `<p><br/></p>` at the next version. So
# regenerating it as `<p><br/></p>` does what the platform's editor does. Two
# rival justifications were measured and are false, which is why the scope is this
# tight:
#
#   "they look the same"   FALSE. body.view differs, and the rendered page is
#                          visibly taller with the <br>.
#   "REST converges too"   FALSE. Sending the stored bytes back through
#                          `page update` leaves `<p />` as `<p />`.
#
# The convergence is the editor's, observed on ordinary body blocks. Nowhere else
# has that observation, so nowhere else gets the verdict.

_EP_CANON = "platform_editor_canonicalization"


def test_an_empty_paragraph_between_body_blocks_is_editor_canonicalization() -> None:
    from cfxmark.compatibility import assess_candidate

    report = assess_candidate("<p>a</p><p /><p>b</p>", "<p>a</p><p><br /></p><p>b</p>")
    verdicts = {row.code: (row.verdict, row.count) for row in report.findings()}
    assert verdicts == {"empty-paragraph-normalised": (_EP_CANON, 1)}
    # Not a loss, so nothing to consent to and nothing blocked.
    assert report.classification == MARKDOWN_LOSSLESS


def test_the_canonicalization_rule_names_its_basis() -> None:
    """A verdict that rests on one observation must say which one.

    Without this the rule reads as "we decided it is fine", and the next reader
    cannot tell it from the appearance argument that was measured false.
    """

    from cfxmark.compatibility import STRUCTURE_RULES

    canon = [rule for rule in STRUCTURE_RULES if rule.verdict == _EP_CANON]
    assert canon, "no rule carries the canonicalization verdict"
    for rule in canon:
        assert rule.basis == "editor_save_convergence"
        assert rule.scope is not None


@pytest.mark.parametrize(
    ("shape", "remote", "candidate"),
    [
        # No editor-convergence observation exists inside these, and the server
        # stores `<p />` identically in all of them -- so "the server treats it
        # differently" is *not* the reason they are excluded. The reason is that
        # the one observation the verdict rests on was made between ordinary body
        # blocks and nowhere else.
        (
            "list item",
            "<ul><li><p>a</p><p /><p>b</p></li></ul>",
            "<ul><li><p>a</p><p><br /></p><p>b</p></li></ul>",
        ),
        (
            "table cell",
            "<table><tbody><tr><td><p>a</p><p /><p>b</p></td></tr></tbody></table>",
            "<table><tbody><tr><td><p>a</p><p><br /></p><p>b</p></td></tr></tbody></table>",
        ),
        (
            "blockquote",
            "<blockquote><p>a</p><p /><p>b</p></blockquote>",
            "<blockquote><p>a</p><p><br /></p><p>b</p></blockquote>",
        ),
        (
            "trailing",
            "<p>a</p><p />",
            "<p>a</p><p><br /></p>",
        ),
        (
            # `ac:rich-text-body` is not in the block-container list, so without the
            # macro-ancestor clause this one slips through on the *neighbour* test:
            # its siblings are two ordinary paragraphs. The mutation that removed
            # that clause was caught by nothing until this case existed.
            "inside a macro body",
            '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>a</p><p /><p>b</p>'
            "</ac:rich-text-body></ac:structured-macro>",
            '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>a</p><p><br /></p><p>b</p>'
            "</ac:rich-text-body></ac:structured-macro>",
        ),
        (
            # A paragraph of prose that comes back empty. The signature is identical
            # -- no element children before, a `br` after -- and the thing that tells
            # them apart is the source paragraph's text, which is why the predicate
            # reads the stored element instead of the path.
            "prose replaced by an empty paragraph",
            "<p>a</p><p>b</p>",
            "<p>a</p><p><br /></p><p>c</p>",
        ),
    ],
)
def test_outside_that_shape_it_stays_a_named_loss(shape: str, remote: str, candidate: str) -> None:
    from cfxmark.compatibility import assess_candidate

    report = assess_candidate(remote, candidate)
    verdicts = {row.verdict for row in report.findings() if row.code == "empty-paragraph-normalised"}
    assert verdicts == {NAMED_LOSS}, f"{shape}: {[(r.code, r.verdict) for r in report.findings()]}"


@pytest.mark.parametrize(
    ("shape", "remote"),
    [
        # An attribute and a whitespace payload are both PRESERVED by the server
        # (measured live), so neither paragraph is empty and neither is this rule's
        # business. The whitespace case is the dangerous one: the lost text leaf
        # sits on the paragraph's own path, so a rule matching the paragraph
        # swallows it and reports one tidy presentation change instead.
        ("attribute", '<p>a</p><p style="text-align: center;" /><p>b</p>'),
        ("whitespace text", "<p>a</p><p> </p><p>b</p>"),
    ],
)
def test_a_paragraph_that_is_not_empty_is_not_canonicalized(shape: str, remote: str) -> None:
    from cfxmark.compatibility import assess_candidate

    report = assess_candidate(remote, "<p>a</p><p><br /></p><p>b</p>")
    assert _EP_CANON not in {row.verdict for row in report.findings()}, shape
    # And what it lost is still reported rather than folded into a presentation note.
    assert report.classification in {UNKNOWN_BLOCKED, NAMED_LOSS_CONSENTABLE}, shape


def test_a_paragraph_the_candidate_invented_is_not_canonicalization() -> None:
    """Nothing was there to canonicalize.

    The candidate appends `<p><br /></p>` where the stored page ends, so the
    signature matches at a path the source has no element for. A scope that cannot
    be evaluated has not been satisfied: the scoped rule is skipped and the strict
    sibling answers instead. Getting this backwards would excuse an invented
    paragraph as something the platform did.
    """

    from cfxmark.compatibility import assess_candidate

    report = assess_candidate("<p>a</p>", "<p>a</p><p><br /></p>")
    empty = [row for row in report.findings() if row.code == "empty-paragraph-normalised"]
    assert [row.verdict for row in empty] == [NAMED_LOSS]
    assert report.classification == UNKNOWN_BLOCKED


# --------------------------------------------------------------------------
# The boundary the reclassification must not cross
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "markdown", "expected"),
    [
        ("one blank line", "alpha\n\nbravo\n", "<p>alpha</p><p>bravo</p>"),
        ("several blank lines", "alpha\n\n\n\nbravo\n", "<p>alpha</p><p>bravo</p>"),
        ("trailing blank lines", "alpha\n\n\n", "<p>alpha</p>"),
        # The one spelling that *does* ask for a break, so the rule above has
        # something to be about.
        ("an explicit break", "alpha\n\n<br />\n\nbravo\n", "<p>alpha</p><p><br/></p><p>bravo</p>"),
    ],
)
def test_an_author_blank_line_never_becomes_a_break(label: str, markdown: str, expected: str) -> None:
    """Paragraph separation is not content, and `<p><br/></p>` is.

    This is the failure mode the empty-paragraph reclassification could turn into:
    once `<p><br/></p>` is the form we happily publish, the tempting next step is
    to emit it wherever Markdown had vertical space. That would add a visible
    blank line to every paragraph boundary in every managed document -- an edit
    nobody asked for, on every page at once, and one the receipt would report as
    presentation canonicalization because that is what the node is called.

    The rule stays what it says: one storage node, one direction, storage side
    only.
    """

    assert cfxmark.to_cfx_artifact(markdown, options=EDITABLE).xhtml == expected, label


# --------------------------------------------------------------------------
# ac:schema-version: the addition direction, and only exactly "1"
# --------------------------------------------------------------------------
#
# Measured, and it corrects two descriptions of this that were in circulation.
#
#   source has "1", candidate omits it     56 occurrences on the 24 adoptable RLM
#                                          pages. Already `equivalent`: the server
#                                          re-stamps its default over the gap.
#   source lacks it, candidate adds "1"    `unknown`, so the page BLOCKS. The same
#                                          measurement covers it -- absent came back
#                                          "1" -- so the stored value is identical
#                                          either way and the block is over nothing.
#   source has "2", candidate sends "1"    `unknown`, and it stays that way. This one
#                                          downgrades the macro, and the server keeps
#                                          whatever we send rather than undoing it.
#
# The third is why the second cannot be waved through on the direction alone: a rule
# that covers "the source had no value" licenses adding *any* value, and only one of
# them is the server's own default.

# A macro carrying a body, so the ONLY difference the round trip produces is the
# attribute under test. The first attempt used a body-less macro and blocked on three
# unrelated unknowns -- the regeneration adds an empty `<ac:rich-text-body/>` -- which
# made the test about the converter's macro shape rather than about this rule.
MACRO_NO_SCHEMA = (
    '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>b</p></ac:rich-text-body></ac:structured-macro>'
)


def test_the_server_supplying_its_own_default_is_not_a_finding() -> None:
    report = assess(MACRO_NO_SCHEMA)
    schema = [item for item in report.judgements if item.difference.attribute_name == "ac:schema-version"]
    assert schema, "expected the regeneration to supply the default"
    assert {item.verdict for item in schema} == {EQUIVALENT}
    assert report.classification in {MARKDOWN_LOSSLESS, MARKDOWN_IDENTITY_BOUND}


def test_a_rule_covering_an_addition_must_name_the_value_it_adds() -> None:
    """Presence is not value, in the direction the registry had not checked.

    `when_before` stops a rule licensing a harmless drop and a real downgrade in the
    same words. Nothing did the same on the other side, so a rule reading
    "the source had no value" would license writing *any* value -- including the "2"
    that the case below keeps as a finding.
    """

    with pytest.raises(RegistryViolation, match="when_after"):
        validate_registry((_rule(when_before=(None,), when_after=None, relation='absent -> present "1"'),))


def test_a_rule_covering_a_drop_must_also_name_what_it_writes() -> None:
    """The other half of the mirror, which was still missing.

    The guard above requires `when_after` when `when_before` covers absence. Nothing
    required it at all otherwise, so a rule reading `present "1" -> absent` -- with
    `when_after` simply unset -- matched *any* regenerated value, because
    `_matches(None, ...)` is True. The module header names this exact failure: the same
    words licensed dropping a harmless default and writing an arbitrary value over the
    macro, and only the second loses data.
    """

    with pytest.raises(RegistryViolation, match="when_after"):
        validate_registry((_rule(when_before=("1",), when_after=None),))


def test_an_arbitrary_schema_version_written_over_a_default_is_not_equivalent() -> None:
    """What the missing guard let through, measured on the public comparator.

    The registered rule is evidence about `present "1" -> absent`: the server re-supplies
    its own default, so omitting it restores the stored value. It says nothing about
    writing "2" -- or "99-EVIL" -- over a macro, and grading those `equivalent` reports
    `markdown_lossless`, the verdict meaning there is nothing to disclose and nobody to
    ask.
    """

    macro = (
        '<ac:structured-macro ac:name="info"%s><ac:rich-text-body><p>x</p></ac:rich-text-body></ac:structured-macro>'
    )
    measured = assess_candidate(macro % ' ac:schema-version="1"', macro % "")
    assert {row.verdict for row in measured.findings()} == {EQUIVALENT}

    for written in ('"2"', '"99-EVIL"'):
        report = assess_candidate(macro % ' ac:schema-version="1"', macro % f" ac:schema-version={written}")
        assert {row.verdict for row in report.findings()} != {EQUIVALENT}, written


def test_a_downgraded_schema_version_is_still_a_finding() -> None:
    """The stored "2" case. The server keeps what we send, so this loses data."""

    storage = MACRO.format(extra="").replace('ac:schema-version="1"', 'ac:schema-version="2"')
    report = assess(storage)
    schema = [item for item in report.judgements if item.difference.attribute_name == "ac:schema-version"]
    assert [item.verdict for item in schema] == [UNKNOWN]
    assert report.classification == UNKNOWN_BLOCKED


@pytest.mark.parametrize(
    ("what", "storage"),
    [
        # Every neighbouring attribute an addition rule must not reach. Named
        # individually because "any other added attribute" is the kind of scope that
        # only holds while somebody remembers it.
        ("a different added attribute", '<ac:structured-macro ac:name="expand" ac:local-id="L1" />'),
        ("the macro name", '<ac:structured-macro ac:name="expand" />'),
        (
            "a macro parameter",
            '<ac:structured-macro ac:name="expand"><ac:parameter ac:name="title">t</ac:parameter></ac:structured-macro>',
        ),
    ],
)
def test_the_addition_rule_reaches_nothing_but_its_own_attribute(what: str, storage: str) -> None:
    report = assess(storage)
    excused = {
        item.difference.attribute_name
        for item in report.judgements
        if item.verdict == EQUIVALENT and item.difference.field == "attribute"
    }
    assert excused <= {"ac:schema-version"}, f"{what}: {excused}"


def test_the_addition_rule_declines_a_value_nothing_was_measured_about() -> None:
    """`when_after` asked directly, because the converter cannot produce the bad case.

    The source-free path renders every directive macro with `"1"`, so a candidate
    writing `"2"` over a macro that had no value cannot arise today. That is the same
    "unobserved rather than impossible" standing the stored-`"2"` downgrade has, and it
    is why the constraint is in the rule rather than left to the converter's current
    behaviour. Nothing else in the suite can reach it, so it is asked of `covers`.
    """

    from cfxmark.compatibility import ATTRIBUTE_RULES
    from cfxmark.source_map import fingerprint_semantic_value

    addition = next(
        rule for rule in ATTRIBUTE_RULES if rule.attribute == "ac:schema-version" and rule.when_before == (None,)
    )
    assert addition.covers(None, fingerprint_semantic_value("1")) is True
    assert addition.covers(None, fingerprint_semantic_value("2")) is False
    # And it does not reach back across the direction it is not about: a stored value
    # being replaced is the other rule's business.
    assert addition.covers(fingerprint_semantic_value("1"), fingerprint_semantic_value("1")) is False


@pytest.mark.parametrize(
    ("shape", "remote", "candidate"),
    [
        # Observed 2026-07-31, in the browser, on two sandbox pages: each was opened and
        # saved with no edit, and `<p/>` became
        # `<p><br /></p>` at v2 -- exactly as a between-blocks control did. So the leading
        # position and a macro neighbour are no longer grounds for exclusion; they were
        # excluded only because nobody had watched the editor there, and now somebody has.
        #
        # 18 of the 19 occurrences on the 24 adoptable RLM pages are this shape.
        (
            "leading, before a bare macro",
            '<p /><ac:structured-macro ac:name="toc" /><p>a</p>',
            '<p><br /></p><ac:structured-macro ac:name="toc" /><p>a</p>',
        ),
        (
            "leading, before a paragraph holding a macro",
            '<p /><p><ac:structured-macro ac:name="toc" /></p><p>a</p>',
            '<p><br /></p><p><ac:structured-macro ac:name="toc" /></p><p>a</p>',
        ),
        (
            "between blocks, next to a macro",
            '<p>a</p><p /><ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p>'
            "</ac:rich-text-body></ac:structured-macro>",
            '<p>a</p><p><br /></p><ac:structured-macro ac:name="info"><ac:rich-text-body><p>x</p>'
            "</ac:rich-text-body></ac:structured-macro>",
        ),
    ],
)
def test_the_shapes_the_browser_save_now_covers(shape: str, remote: str, candidate: str) -> None:
    from cfxmark.compatibility import assess_candidate

    report = assess_candidate(remote, candidate)
    verdicts = {row.verdict for row in report.findings() if row.code == "empty-paragraph-normalised"}
    assert verdicts == {_EP_CANON}, f"{shape}: {[(r.code, r.verdict) for r in report.findings()]}"
