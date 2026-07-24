from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import assume, given

import cfxmark

_plain_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        blacklist_characters=("\r", "\n"),
    ),
    min_size=1,
    max_size=24,
)


@given(first=_plain_text, before=_plain_text, after=_plain_text, last=_plain_text)
def test_one_block_edit_preserves_unaffected_stable_node_identity(
    first: str, before: str, after: str, last: str
) -> None:
    assume(len({first, before, after, last}) == 4)
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(
        f"<p>{first}</p><p>{before}</p><p>{last}</p>",
        options=options,
    )
    candidate = cfxmark.to_cfx_artifact(
        f"{first}\n\n{after}\n\n{last}\n",
        base_artifact=base,
        options=options,
    )
    unaffected_paragraph_ids = {
        node.node_id for node in base.semantic_nodes if node.node_type == "Paragraph" and node.ordinal in {0, 2}
    }
    unaffected_ids = unaffected_paragraph_ids | {
        node.node_id for node in base.semantic_nodes if node.parent_id in unaffected_paragraph_ids
    }

    assert candidate.push_safe is True
    assert unaffected_ids.isdisjoint(
        operation.base_node_id for operation in candidate.edit_operations if operation.base_node_id is not None
    )
