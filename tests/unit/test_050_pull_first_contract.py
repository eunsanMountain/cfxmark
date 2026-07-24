from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

import cfxmark
import cfxmark.artifacts as artifacts_module
from cfxmark.macros import default_registry
from cfxmark.macros.builtins import AdmonitionHandler
from cfxmark.source_map import operation_id as source_operation_id

EDITABLE = cfxmark.ConversionOptions(profile="editable")


def _public(name: str) -> Any:
    value = getattr(cfxmark, name, None)
    assert value is not None, f"cfxmark 0.5.0 public contract is missing: {name}"
    return value


def _diagnostic(result: Any, code: str) -> Any:
    diagnostics = getattr(result, "diagnostics", ())
    matches = [item for item in diagnostics if item.code == code]
    assert matches, f"missing typed diagnostic {code!r}; got {diagnostics!r}"
    diagnostic = matches[0]
    assert diagnostic.occurrence_id.startswith("occ-")
    assert diagnostic.location is not None
    assert diagnostic.resolutions
    return diagnostic


def _first_image(result: Any) -> Any:
    document = result.document
    assert document is not None
    image = document.children[0].children[0]
    assert type(image).__name__ == "Image"
    return image


@pytest.mark.parametrize(
    ("storage", "preserved_fragment"),
    [
        ("<p>Keep  </p><p>X</p>", "<p>Keep  </p>"),
        ("<p>Keep</p><p></p><p>X</p>", "<p>Keep</p><p/>"),
    ],
)
def test_r1_localized_cosmetic_storage_is_preserved_during_unrelated_edit(
    storage: str,
    preserved_fragment: str,
) -> None:
    to_md_artifact: Callable[..., Any] = _public("to_md_artifact")
    to_cfx_artifact: Callable[..., Any] = _public("to_cfx_artifact")

    artifact = to_md_artifact(storage, options=EDITABLE)
    candidate = to_cfx_artifact(
        artifact.markdown.replace("X", "Y"),
        presentation=artifact.presentation,
        base_artifact=artifact,
        splice_source=storage,
        options=EDITABLE,
    )

    assert artifact.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert preserved_fragment in candidate.xhtml
    assert "<p>Y</p>" in candidate.xhtml


def test_unchanged_list_item_paragraph_is_remote_spliced_instead_of_blocking() -> None:
    storage = "<ul><li><p>Keep</p></li></ul><p>X</p>"

    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert artifact.status == "ready_to_edit"
    assert not [item for item in artifact.diagnostics if item.blocking]


@pytest.mark.parametrize(
    ("convert", "source", "code"),
    [
        (cfxmark.to_md, "<p>before<!--x-->after</p>", "xml-comment-dropped"),
        (cfxmark.to_cfx, "before <custom>after</custom>\n", "html-tag-stripped"),
    ],
)
def test_r3_dropped_content_has_a_blocking_typed_diagnostic(
    convert: Callable[..., Any], source: str, code: str
) -> None:
    result = convert(source, options=EDITABLE)

    assert result.push_safe is False
    diagnostic = _diagnostic(result, code)
    assert diagnostic.category == "content_loss"
    assert diagnostic.severity == "blocking"


@pytest.mark.parametrize(
    "storage",
    [
        "<!-- synthetic --><p>x</p>",
        "<p>x</p><!-- synthetic -->",
        "<p>x</p><!-- synthetic --><p>y</p>",
        "<?synthetic value?><p>x</p>",
    ],
)
def test_root_comment_and_processing_instruction_fail_closed_without_crashing(storage: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; import cfxmark; "
                "artifact = cfxmark.to_md_artifact(sys.argv[1], "
                "options=cfxmark.ConversionOptions(profile='editable')); "
                "print(json.dumps({'status': artifact.status, 'push_safe': artifact.push_safe, "
                "'codes': [item.code for item in artifact.diagnostics if item.blocking]}))"
            ),
            storage,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "needs_migration"
    assert result["push_safe"] is False
    assert "xml-comment-dropped" in result["codes"]


def test_candidate_artifact_binds_source_map_provenance_to_exact_storage_bytes() -> None:
    artifact = cfxmark.to_cfx_artifact("Before **after**\n", options=EDITABLE)

    assert artifact.candidate_storage_sha256 == hashlib.sha256(artifact.xhtml.encode()).hexdigest()


def test_candidate_artifact_reparses_final_xhtml_and_blocks_unreported_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_render = artifacts_module.render_cfx_with_source_map

    def inject_collateral(*args: Any, **kwargs: Any) -> Any:
        xhtml, attachments, warnings, extraction = real_render(*args, **kwargs)
        return xhtml.replace("<p>", '<p data-collateral="silent-loss">', 1), attachments, warnings, extraction

    monkeypatch.setattr(artifacts_module, "render_cfx_with_source_map", inject_collateral)

    artifact = cfxmark.to_cfx_artifact("Edited\n", options=EDITABLE)

    assert artifact.push_safe is False
    assert any(diagnostic.code == "semantic-source-map-incomplete" for diagnostic in artifact.diagnostics)
    assert any(
        leaf.field == "attribute" and leaf.attribute_name == "data-collateral"
        for leaf in artifact.unmapped_storage_leaves
    )


def test_public_candidate_verifier_rejects_matching_digest_with_missing_inverse_leaf() -> None:
    artifact = cfxmark.to_cfx_artifact("Edited\n", options=EDITABLE)
    forged_xhtml = artifact.xhtml.replace("<p>", '<p data-collateral="silent-loss">', 1)
    forged = replace(
        artifact,
        xhtml=forged_xhtml,
        candidate_storage_sha256=hashlib.sha256(forged_xhtml.encode()).hexdigest(),
    )

    with pytest.raises(cfxmark.ConversionError, match="exactly one owner"):
        cfxmark.validate_cfx_artifact(forged)


def test_edit_operations_exclude_unchanged_ancestor_subtrees() -> None:
    base = cfxmark.to_md_artifact("<p>A</p><p>B</p><p>C</p>", options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "A\n\nEdited\n\nC\n",
        presentation=base.presentation,
        base_artifact=base,
        options=EDITABLE,
    )
    base_types = {node.node_id: node.node_type for node in base.semantic_nodes}
    updated_types = {
        base_types[operation.base_node_id]
        for operation in candidate.edit_operations
        if operation.kind == "update" and operation.base_node_id is not None
    }

    assert updated_types == {"Text"}


def test_insert_operation_owns_every_storage_leaf_in_its_new_subtree() -> None:
    base = cfxmark.to_md_artifact("<p>Before</p>", options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        "Before\n\nInserted **bold** text\n",
        base_artifact=base,
        options=EDITABLE,
    )
    insertion = next(operation for operation in candidate.edit_operations if operation.kind == "insert")
    owner = source_operation_id(insertion)
    inserted_leaves = [
        (leaf, operation_id)
        for leaf, operation_id in candidate.candidate_storage_to_operation.items()
        if leaf.semantic_path[:1] == ("p[1]",)
    ]

    assert inserted_leaves
    assert {operation_id for _leaf, operation_id in inserted_leaves} == {owner}


def test_candidate_semantic_nodes_can_rebase_local_attachment_paths() -> None:
    storage = '<p><ac:image><ri:attachment ri:filename="diagram.png"/></ac:image></p>'
    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    options = cfxmark.ConversionOptions(
        profile="editable",
        attachment_filename_map=(("assets/diagram.png", "diagram.png"), ("assets/new.png", "new.png")),
    )
    local_baseline = cfxmark.to_cfx_artifact(
        "![](assets/diagram.png)\n",
        base_artifact=base,
        splice_source=storage,
        options=options,
    )
    rebased = replace(base, semantic_nodes=local_baseline.semantic_nodes)
    edited = cfxmark.to_cfx_artifact(
        "![](assets/diagram.png)\n\n![new](assets/new.png)\n",
        base_artifact=rebased,
        splice_source=storage,
        options=options,
    )

    cfxmark.validate_cfx_artifact(edited, source_storage=storage)
    assert {node.node_id for node in local_baseline.semantic_nodes} == {node.node_id for node in base.semantic_nodes}
    assert [operation.kind for operation in edited.edit_operations] == ["update", "insert"]
    assert edited.edit_operations[0].topology_only is True
    assert not [item for item in edited.diagnostics if item.blocking]


def test_escaped_literal_text_keeps_one_storage_owner() -> None:
    artifact = cfxmark.to_md_artifact("<p>[literal]</p>", options=EDITABLE)

    assert artifact.status == "ready_to_edit"
    assert artifact.push_safe is True
    assert not [item for item in artifact.diagnostics if item.blocking]


def test_escaped_text_round_trip_has_complete_candidate_source_map() -> None:
    artifact = cfxmark.to_cfx_artifact("\\[literal\\]\n", options=EDITABLE)

    cfxmark.validate_cfx_artifact(artifact)
    assert not [item for item in artifact.diagnostics if item.blocking]


@pytest.mark.parametrize(
    ("storage", "expected_path"),
    [
        (
            '<ac:structured-macro ac:name="synthetic-widget">'
            '<ac:parameter ac:name="mode">full</ac:parameter>'
            "</ac:structured-macro>",
            ("ac:structured-macro[0]",),
        ),
        (
            # A body-carrying macro: parameter-only macros now round-trip through
            # the managed opaque payload transport, so the protected-region
            # contract is pinned with a shape that stays non-round-trippable.
            '<p>Before <ac:structured-macro ac:name="synthetic-widget">'
            '<ac:parameter ac:name="mode">full</ac:parameter>'
            "<ac:rich-text-body><p>inner</p></ac:rich-text-body>"
            "</ac:structured-macro> after</p>",
            ("p[0]", "ac:structured-macro[0]"),
        ),
        (
            '<blockquote><ac:structured-macro ac:name="synthetic-widget">'
            '<ac:parameter ac:name="mode">full</ac:parameter>'
            "</ac:structured-macro></blockquote>",
            ("blockquote[0]",),
        ),
    ],
)
def test_opaque_managed_artifact_is_an_occurrence_scoped_protected_region(
    storage: str,
    expected_path: tuple[str, ...],
) -> None:
    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert artifact.status == "ready_to_edit"
    assert artifact.push_safe is True
    assert len(artifact.protected_regions) == 1
    region = artifact.protected_regions[0]
    assert region.remote_node_path == expected_path
    assert region.editable is False
    assert "cfxmark:unsupported" not in artifact.markdown
    assert "cfxmark:payloads" not in artifact.markdown
    assert 'ac:name="synthetic-widget"' not in artifact.markdown
    assert 'ac:name="mode"' not in artifact.markdown
    assert ">full<" not in artifact.markdown
    assert region.base_markdown_range.end > region.base_markdown_range.start


def test_unrelated_eof_append_preserves_inter_element_whitespace_of_unchanged_remote_subtrees() -> None:
    storage = (
        '<ac:structured-macro ac:name="synthetic-widget">'
        "<ac:plain-text-body><![CDATA[x]]></ac:plain-text-body>"
        "</ac:structured-macro>\n"
        "<h2>B</h2>\n"
        "<p>C</p>"
    )
    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    candidate = cfxmark.to_cfx_artifact(
        f"{artifact.markdown}\n\ntest\n",
        presentation=artifact.presentation,
        base_artifact=artifact,
        splice_source=storage,
        options=EDITABLE,
    )

    assert artifact.status == "ready_to_edit"
    assert candidate.push_safe is True
    assert candidate.xhtml == f"{storage}<p>test</p>"


def test_xml_comment_does_not_claim_surrounding_text_as_its_migration_scope() -> None:
    artifact = cfxmark.to_md_artifact("<p>before<!-- synthetic -->after</p>", options=EDITABLE)

    assert {diagnostic.code for diagnostic in artifact.diagnostics if diagnostic.blocking} == {"xml-comment-dropped"}
    comment = next(diagnostic for diagnostic in artifact.diagnostics if diagnostic.code == "xml-comment-dropped")
    migration = next(
        entry
        for entry in artifact.source_map_entries
        if entry.source_kind == "migration" and entry.node_or_occurrence_id == comment.occurrence_id
    )
    assert comment.location.semantic_path == ("p[0]", "#comment[0]")
    assert all(leaf.semantic_path[:2] == comment.location.semantic_path for leaf in migration.leaves)
    assert artifact.unmapped_storage_leaves == ()


def test_emoticon_has_one_occurrence_owned_migration_without_empty_paragraph_noise() -> None:
    storage = '<p><ac:emoticon ac:name="smile"/></p>'
    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert artifact.status == "ready_to_edit"
    assert not any(diagnostic.blocking for diagnostic in artifact.diagnostics)
    diagnostic = next(diagnostic for diagnostic in artifact.diagnostics if diagnostic.code == "emoticon-to-unicode")
    assert diagnostic.severity == "informational"
    assert diagnostic.location.semantic_path == ("p[0]", "ac:emoticon[0]")
    migration = next(
        entry
        for entry in artifact.source_map_entries
        if entry.source_kind == "migration" and entry.node_or_occurrence_id == diagnostic.occurrence_id
    )
    assert all(leaf.semantic_path[:2] == diagnostic.location.semantic_path for leaf in migration.leaves)
    candidate = cfxmark.to_cfx_artifact(
        artifact.markdown,
        base_artifact=artifact,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.push_safe is True
    assert candidate.xhtml == storage


def test_repeated_emoticons_keep_distinct_occurrences_with_joint_text_ownership() -> None:
    artifact = cfxmark.to_md_artifact(
        '<p><ac:emoticon ac:name="smile"/><ac:emoticon ac:name="smile"/></p>',
        options=EDITABLE,
    )

    emoticons = [diagnostic for diagnostic in artifact.diagnostics if diagnostic.code == "emoticon-to-unicode"]
    assert len(emoticons) == 2
    assert len({diagnostic.occurrence_id for diagnostic in emoticons}) == 2
    assert not any(diagnostic.code == "semantic-source-map-incomplete" for diagnostic in artifact.diagnostics)
    assert artifact.unmapped_storage_leaves == ()
    assert artifact.status == "ready_to_edit"
    assert all(diagnostic.severity == "informational" for diagnostic in emoticons)


def test_nested_and_double_digit_migration_occurrences_are_bound_by_producer_path() -> None:
    paragraphs = "".join('<p><ac:emoticon ac:name="smile"/></p>' for _ in range(12))
    storage = (
        paragraphs
        + '<blockquote><p><ac:emoticon ac:name="sad"/></p></blockquote>'
        + '<p><ac:emoticon ac:name="information"/></p>'
    )

    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    emoticons = [diagnostic for diagnostic in artifact.diagnostics if diagnostic.code == "emoticon-to-unicode"]
    expected_paths = {(f"p[{index}]", "ac:emoticon[0]") for index in range(13)} | {
        ("blockquote[0]", "p[0]", "ac:emoticon[0]")
    }
    assert {diagnostic.location.semantic_path for diagnostic in emoticons} == expected_paths
    assert len({diagnostic.occurrence_id for diagnostic in emoticons}) == len(expected_paths)
    migration_by_occurrence = {
        entry.node_or_occurrence_id: entry for entry in artifact.source_map_entries if entry.source_kind == "migration"
    }
    for diagnostic in emoticons:
        migration = migration_by_occurrence[diagnostic.occurrence_id]
        path = diagnostic.location.semantic_path
        assert all(leaf.semantic_path[: len(path)] == path for leaf in migration.leaves)
    assert artifact.unmapped_storage_leaves == ()


@pytest.mark.parametrize(
    ("markdown", "expected_alt", "expected_target"),
    [
        ("![a,b](f.png)\n", "a,b", "f.png"),
        ("![A](https://e.test/x.png?q=a|b)\n", "A", "https://e.test/x.png?q=a|b"),
    ],
)
def test_r4_jira_image_delimiters_preserve_identity_and_reach_a_fixed_point(
    markdown: str, expected_alt: str, expected_target: str
) -> None:
    wiki_1 = cfxmark.to_jira_wiki(markdown, input_format="markdown").jira_wiki or ""
    markdown_1 = cfxmark.from_jira_wiki(wiki_1)
    wiki_2 = cfxmark.to_jira_wiki(markdown_1.markdown or "", input_format="markdown").jira_wiki or ""
    markdown_2 = cfxmark.from_jira_wiki(wiki_2)

    for result in (markdown_1, markdown_2):
        image = _first_image(result)
        assert image.alt == expected_alt
        assert image.src == expected_target
    assert wiki_2 == wiki_1
    assert markdown_2.markdown == markdown_1.markdown


def test_r5_non_table_leading_pipe_with_inline_code_is_a_paragraph() -> None:
    result = cfxmark.to_cfx("| prose `a|b` only\n", options=EDITABLE)

    assert result.xhtml == "<p>| prose <code>a|b</code> only</p>"


def test_preservation_signature_keeps_collateral_and_color_identity() -> None:
    signature: Callable[[str], Any] = _public("preservation_signature")

    assert signature("<p>A  </p>") != signature("<p>A</p>")
    assert signature("<p>A</p><p></p>") != signature("<p>A</p>")
    assert signature("<ul><li><p>A</p></li></ul>") != signature("<ul><li>A</li></ul>")
    assert signature("<p>A</p>\n<h2>B</h2>") == signature("<p>A</p><h2>B</h2>")
    assert signature("<p><strong>A</strong> <em>B</em></p>") != signature("<p><strong>A</strong><em>B</em></p>")
    assert signature('<p style="text-align:left">A</p>') == signature('<p style="text-align: left;">A</p>')
    assert signature('<p style="text-align:LEFT">A</p>') == signature('<p style="text-align:left">A</p>')
    assert signature('<table><tbody><tr><td data-highlight-colour="#ff0000">A</td></tr></tbody></table>') != signature(
        '<table><tbody><tr><td data-highlight-colour="#00ff00">A</td></tr></tbody></table>'
    )


def test_source_maps_are_bidirectional_and_artifact_reaches_a_fixed_point() -> None:
    to_md_artifact: Callable[..., Any] = _public("to_md_artifact")
    to_cfx_artifact: Callable[..., Any] = _public("to_cfx_artifact")
    storage = "<p>A</p><table><thead><tr><th>H</th></tr></thead><tbody><tr><td>X</td></tr></tbody></table>"

    base = to_md_artifact(storage, options=EDITABLE)
    candidate = to_cfx_artifact(base.markdown, presentation=base.presentation, options=EDITABLE)
    again = to_md_artifact(candidate.xhtml, options=EDITABLE)

    assert base.base_storage_to_node
    assert base.base_node_to_storage
    for leaf, node_id in base.base_storage_to_node.items():
        assert leaf in base.base_node_to_storage[node_id]
    for node_id, leaves in base.base_node_to_storage.items():
        assert all(base.base_storage_to_node[leaf] == node_id for leaf in leaves)
    assert candidate.edited_operation_to_storage
    assert candidate.candidate_storage_to_operation
    for operation_id, leaves in candidate.edited_operation_to_storage.items():
        assert all(candidate.candidate_storage_to_operation[leaf] == operation_id for leaf in leaves)
    assert again.markdown == base.markdown
    assert again.presentation == base.presentation
    assert _public("preservation_signature")(candidate.xhtml) == _public("preservation_signature")(
        to_cfx_artifact(again.markdown, presentation=again.presentation, options=EDITABLE).xhtml
    )


def test_empty_link_does_not_synthesize_visible_text_and_is_a_zero_delta_fixed_point() -> None:
    storage = '<p><a href="x"></a>A</p>'

    base = cfxmark.to_md_artifact(storage, options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact(
        base.markdown,
        presentation=base.presentation,
        base_artifact=base,
        options=EDITABLE,
    )

    assert base.status == "ready_to_edit"
    assert base.markdown == "[](x)A\n"
    assert candidate.edit_operations == ()
    assert cfxmark.preservation_signature(candidate.xhtml) == cfxmark.preservation_signature(storage)


@pytest.mark.parametrize(
    "storage",
    [
        "<p><strong></strong>A</p>",
        "<p><em></em>A</p>",
        "<p><del></del>A</p>",
        "<p><code></code>A</p>",
    ],
)
def test_converter_induced_base_round_trip_delta_fails_closed_during_pull(storage: str) -> None:
    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert artifact.status == "needs_migration"
    assert artifact.push_safe is False
    diagnostic = _diagnostic(artifact, "managed-base-roundtrip-changed")
    assert diagnostic.category == "structural_loss"
    assert diagnostic.severity == "blocking"


def test_edit_operation_contract_covers_delete_tombstone_and_move_correspondence() -> None:
    storage_leaf_ref = _public("StorageLeafRef")
    edit_operation = _public("EditOperation")
    leaf = storage_leaf_ref(("p[2]",), "text", None, 0, "sha256:deleted")

    deleted = edit_operation("delete", "node-x", None, "root", None, 1, None, (leaf,))
    moved = edit_operation("move", "node-x", "node-x", "left", "right", 1, 0)

    assert deleted.deleted_leaf_tombstones == (leaf,)
    assert deleted.base_node_id == "node-x"
    assert moved.base_node_id == moved.edited_node_id == "node-x"
    assert (moved.old_parent_id, moved.new_parent_id, moved.old_ordinal, moved.new_ordinal) == ("left", "right", 1, 0)


def test_candidate_source_map_carries_stable_node_identity_through_moves() -> None:
    base = cfxmark.to_md_artifact("<p>A</p><p>B</p>", options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact("B\n\nA\n", base_artifact=base, options=EDITABLE)
    moved_ids = {operation.base_node_id for operation in candidate.edit_operations if operation.kind == "move"}

    assert len(moved_ids) == 2
    candidate_stable_ids = {
        entry.node_or_occurrence_id for entry in candidate.source_map_entries if entry.source_kind == "ast-node"
    }
    assert moved_ids <= candidate_stable_ids


def test_fragment_topology_leaf_proves_cross_tag_top_level_reorder() -> None:
    base = cfxmark.to_md_artifact("<h1>A</h1><p>B</p>", options=EDITABLE)
    candidate = cfxmark.to_cfx_artifact("B\n\n# A\n", base_artifact=base, options=EDITABLE)
    fragment_base = next(leaf for leaf in base.base_storage_to_node if leaf.semantic_path == ("#fragment[0]",))
    fragment_candidate = next(
        leaf for leaf in candidate.candidate_storage_to_operation if leaf.semantic_path == ("#fragment[0]",)
    )

    assert fragment_base.value_fingerprint != fragment_candidate.value_fingerprint
    fragment_owner = candidate.candidate_storage_to_operation[fragment_candidate]
    fragment_source = next(
        entry
        for entry in candidate.source_map_entries
        if entry.operation_id == fragment_owner and fragment_candidate in entry.leaves
    )
    assert fragment_source.node_or_occurrence_id in candidate.serializer_ancestor_node_ids


def test_source_maps_use_actual_xml_leaf_paths_instead_of_ast_dataclass_paths() -> None:
    to_md_artifact: Callable[..., Any] = _public("to_md_artifact")
    to_cfx_artifact: Callable[..., Any] = _public("to_cfx_artifact")
    base = to_md_artifact("<p>A</p><p>B</p>", options=EDITABLE)

    assert base.push_safe
    assert base.unmapped_storage_leaves == ()
    assert base.base_storage_to_node
    assert all("Document" not in component for leaf in base.base_storage_to_node for component in leaf.semantic_path)
    assert any(leaf.semantic_path == ("p[0]",) and leaf.field == "text" for leaf in base.base_storage_to_node)

    candidate = to_cfx_artifact(
        "A\n\nChanged\n",
        base_artifact=base,
        splice_source="<p>A</p><p>B</p>",
        options=EDITABLE,
    )

    assert candidate.push_safe
    assert candidate.unmapped_storage_leaves == ()
    assert candidate.candidate_storage_to_operation
    assert all(
        "Document" not in component
        for leaf in candidate.candidate_storage_to_operation
        for component in leaf.semantic_path
    )
    assert any(
        leaf.semantic_path == ("p[1]",) and leaf.field == "text" for leaf in candidate.candidate_storage_to_operation
    )


def test_base_source_map_uses_the_caller_macro_registry() -> None:
    registry = default_registry.copy()
    handler = AdmonitionHandler("info")
    handler.directive_name = "callout"
    registry.register(handler)
    storage = (
        '<ac:structured-macro ac:name="info" ac:schema-version="1">'
        "<ac:rich-text-body><p>Hello</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )

    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE, macros=registry)

    assert artifact.status == "ready_to_edit"
    assert "::: callout" in artifact.markdown
    assert artifact.base_storage_to_node
    assert not artifact.unmapped_storage_leaves


def test_duplicate_sibling_delete_is_ambiguous_instead_of_first_match() -> None:
    to_md_artifact: Callable[..., Any] = _public("to_md_artifact")
    to_cfx_artifact: Callable[..., Any] = _public("to_cfx_artifact")
    base = to_md_artifact("<p>A</p><p>X</p><p>X</p><p>B</p>", options=EDITABLE)

    candidate = to_cfx_artifact("A\n\nX\n\nB\n", presentation=base.presentation, base_artifact=base, options=EDITABLE)

    assert candidate.push_safe is False
    diagnostic = _diagnostic(candidate, "semantic-mapping-ambiguous")
    assert diagnostic.category == "structural_loss"
    assert diagnostic.severity == "blocking"


def test_r9_table_background_is_typed_and_string_only_editable_api_fails_safe() -> None:
    to_md_artifact: Callable[..., Any] = _public("to_md_artifact")
    storage = (
        "<table><thead><tr><th>H</th></tr></thead><tbody><tr>"
        '<td data-highlight-colour="#ff0000">X</td></tr></tbody></table>'
    )

    artifact = to_md_artifact(storage, options=EDITABLE)
    table = artifact.document.children[0]
    cell = table.body[0].cells[0]

    assert cell.background.exact == "#ff0000"
    assert cell.background.equality_key
    assert cell.background.source_encoding == "data-highlight-colour"
    assert artifact.presentation
    string_only = cfxmark.to_md(storage, options=EDITABLE)
    assert string_only.push_safe is False
    diagnostic = _diagnostic(string_only, "table-cell-background-untracked")
    assert diagnostic.category == "presentation_loss"
    assert diagnostic.severity == "blocking"


def test_valid_but_unverified_color_keeps_exact_value_as_remote_baseline() -> None:
    to_md_artifact: Callable[..., Any] = _public("to_md_artifact")
    exact = "var(--ds-background-accent-blue-subtlest)"
    storage = f'<table><tbody><tr><td style="background-color:{exact}">X</td></tr></tbody></table>'

    artifact = to_md_artifact(storage, options=EDITABLE)
    table = artifact.document.children[0]
    background = table.body[0].cells[0].background

    assert background.exact == exact
    assert background.value_kind == "design-token"
    assert background.capability_id is None
    assert background.render_strategy is None
    assert artifact.status == "ready_to_edit"
    candidate = cfxmark.to_cfx_artifact(
        artifact.markdown,
        presentation=artifact.presentation,
        base_artifact=artifact,
        splice_source=storage,
        options=EDITABLE,
    )
    assert candidate.push_safe is True
    assert exact in candidate.xhtml


def test_exact_storage_migration_resolver_changes_only_the_approved_occurrence() -> None:
    storage = '<p><ac:emoticon ac:name="smile"/></p><p><span style="color:red">X</span></p>'
    artifact = cfxmark.to_md_artifact(storage)
    occurrence = next(item for item in artifact.diagnostics if item.code == "emoticon-to-unicode")

    resolved = cfxmark.resolve_storage_migrations(storage, {occurrence.occurrence_id: "unicode"})

    assert cfxmark.preservation_signature(resolved) == cfxmark.preservation_signature(
        '<p>🙂</p><p><span style="color:red">X</span></p>'
    )


def test_exact_storage_migration_preserves_unapproved_comments_and_fragment_tails() -> None:
    storage = '<p>before<!-- keep --><ac:emoticon ac:name="smile"/>after</p>tail'
    artifact = cfxmark.to_md_artifact(storage)
    occurrence = next(item for item in artifact.diagnostics if item.code == "emoticon-to-unicode")

    resolved = cfxmark.resolve_storage_migrations(storage, {occurrence.occurrence_id: "unicode"})

    assert resolved == "<p>before<!-- keep -->🙂after</p>tail"
    assert cfxmark.canonicalize_cfx("<p>x</p>tail") == "<p>x</p>tail"
    assert cfxmark.preservation_signature("<p>x</p>tail") != cfxmark.preservation_signature("<p>x</p>")
    assert cfxmark.preservation_signature("<p>x<!-- keep --></p>") != cfxmark.preservation_signature("<p>x</p>")


def test_fragment_text_and_element_tail_are_source_mapped_or_blocked() -> None:
    artifact = cfxmark.to_md_artifact("leading<p>x</p>tail", options=EDITABLE)

    assert artifact.status == "needs_migration"
    assert artifact.unmapped_storage_leaves
    assert any(
        leaf.semantic_path == ("#fragment[0]",) and leaf.field == "text" for leaf in artifact.unmapped_storage_leaves
    )
    assert any(leaf.semantic_path == ("p[0]",) and leaf.field == "tail" for leaf in artifact.unmapped_storage_leaves)


def test_pull_reports_residual_roundtrip_drift_alongside_approvable_migration() -> None:
    storage = '<p><ac:emoticon ac:name="smile"/></p><p><span style="color:red">X</span></p>'

    artifact = cfxmark.to_md_artifact(storage, options=EDITABLE)

    assert artifact.status == "ready_to_edit"
    diagnostics = {item.code: item for item in artifact.diagnostics}
    assert "emoticon-to-unicode" in diagnostics
    assert diagnostics["emoticon-to-unicode"].severity == "informational"
    assert "managed-base-roundtrip-changed" not in diagnostics


def test_exact_storage_migration_resolver_rejects_wrong_occurrence_or_resolution() -> None:
    storage = '<p><ac:emoticon ac:name="smile"/></p>'
    artifact = cfxmark.to_md_artifact(storage)
    occurrence = next(item for item in artifact.diagnostics if item.code == "emoticon-to-unicode")

    with pytest.raises(cfxmark.ConversionError):
        cfxmark.resolve_storage_migrations(storage, {"occ-missing": "unicode"})
    with pytest.raises(cfxmark.ConversionError):
        cfxmark.resolve_storage_migrations(storage, {occurrence.occurrence_id: "keep-remote"})
