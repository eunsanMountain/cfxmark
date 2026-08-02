from __future__ import annotations

import hashlib

import cfxmark


def test_replacement_manifest_is_built_from_the_final_identity_carried_xhtml() -> None:
    storage = (
        '<p>Keep</p><ac:structured-macro ac:name="info" ac:macro-id="kept-raw-uuid">'
        "<ac:rich-text-body><p>Old kept macro</p></ac:rich-text-body></ac:structured-macro>"
        '<ac:structured-macro ac:name="info" ac:macro-id="discarded-raw-uuid">'
        "<ac:rich-text-body><p>Old discarded macro</p></ac:rich-text-body></ac:structured-macro>"
    )
    options = cfxmark.ConversionOptions(profile="editable")
    base = cfxmark.to_md_artifact(storage, options=options)
    edited = base.markdown.replace("Keep", "Edited keep").replace("::: info\nOld discarded macro\n:::\n", "")
    candidate = cfxmark.to_cfx_artifact(
        edited,
        presentation=base.presentation,
        base_artifact=base,
        splice_source=storage,
        options=options,
    )

    manifest = cfxmark.build_replacement_manifest(base_artifact=base, candidate=candidate)
    public = manifest.to_dict()

    assert 'ac:macro-id="kept-raw-uuid"' in candidate.xhtml
    assert 'ac:macro-id="discarded-raw-uuid"' not in candidate.xhtml
    assert manifest.candidate_storage_sha256 == "sha256:" + hashlib.sha256(candidate.xhtml.encode("utf-8")).hexdigest()
    assert public["discarded_identities"] == [
        {
            "kind": "info",
            "semantic_path": ["ac:structured-macro[1]"],
            "ordinal": 1,
            "count": 1,
        }
    ]
    rendered = repr(public)
    assert "kept-raw-uuid" not in rendered
    assert "discarded-raw-uuid" not in rendered


def test_replacement_manifest_hash_changes_when_the_discarded_identity_changes() -> None:
    options = cfxmark.ConversionOptions(profile="editable")
    first_storage = '<ac:structured-macro ac:name="info" ac:macro-id="first-raw-uuid"/>'
    second_storage = '<ac:structured-macro ac:name="info" ac:macro-id="second-raw-uuid"/>'

    def manifest(storage: str) -> cfxmark.ReplacementManifest:
        base = cfxmark.to_md_artifact(storage, options=options)
        candidate = cfxmark.to_cfx_artifact(
            "replacement\n",
            presentation=base.presentation,
            base_artifact=base,
            splice_source=storage,
            options=options,
        )
        return cfxmark.build_replacement_manifest(base_artifact=base, candidate=candidate)

    first = manifest(first_storage)
    second = manifest(second_storage)

    assert first.discarded_identities == second.discarded_identities
    assert first.discarded_identities_sha256 != second.discarded_identities_sha256
