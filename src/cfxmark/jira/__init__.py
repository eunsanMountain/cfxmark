"""Jira-specific converters (experimental, lossy).

This submodule collects the Jira-flavoured entry points that share the
same :class:`cfxmark.ConversionResult` shape as the core Confluence
converters but operate on **different input and output contracts**:

* ``to_jira_wiki`` — render Markdown or Confluence Storage XHTML to
  Jira wiki markup. Stable. Re-exported from :mod:`cfxmark.api`.
* ``from_jira_wiki`` — parse Jira wiki markup back into Markdown.
  **Experimental and lossy** — see the module docstring and the
  README for the exact contract. Added in v0.3.

The Confluence round-trip (``to_cfx``/``to_md``) is **byte-identical**
for every construct in its supported subset. The Jira round-trip is
**not**: Jira wiki markup is a looser dialect without opaque macro
identity preservation, so
``to_md(to_jira_wiki(m)) ≈ canonicalize(m)`` is the strongest
guarantee we can offer. Keeping the Jira entry points in their own
namespace makes this asymmetry obvious at the import site.

Typical wrapper usage::

    from cfxmark.jira import from_jira_wiki, to_jira_wiki

    # Jira issue description → Markdown (experimental, lossy)
    result = from_jira_wiki(jira_issue.description)
    local_md = result.markdown

    # Local Markdown → Jira wiki markup (stable)
    result = to_jira_wiki(local_md)
    jira_issue.description = result.jira_wiki
"""

from __future__ import annotations

from cfxmark.api import from_jira_wiki, to_jira_wiki

__all__ = [
    "to_jira_wiki",
    "from_jira_wiki",
]
