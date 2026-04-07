"""Confluence ``jira`` issue macro.

Confluence has two common forms:

* Single issue: ``<ac:structured-macro ac:name="jira"><ac:parameter
  ac:name="key">PROJ-1</ac:parameter></ac:structured-macro>``
* JQL query: ``<ac:structured-macro ac:name="jira"><ac:parameter
  ac:name="jqlQuery">project = PROJ</ac:parameter>...``

We model both as a single :class:`DirectiveMacro` with ``parameters``
carrying the original ``ac:parameter`` pairs. The directive name is
``jira``.
"""

from __future__ import annotations

import lxml.etree as ET

from cfxmark.ast import DirectiveMacro
from cfxmark.macros.registry import BodyParser, BodyRenderer
from cfxmark.xml_ns import ac_attr, build_structured_macro


class JiraHandler:
    name = "jira"
    directive_name = "jira"

    def from_cfx(
        self,
        element: ET._Element,
        parse_body: BodyParser,
    ) -> DirectiveMacro | None:
        parameters: list[tuple[str, str]] = []
        for child in element:
            local = child.tag.rsplit("}", 1)[-1]
            if local == "parameter":
                pname = child.get(ac_attr("name")) or ""
                pvalue = (child.text or "").strip()
                if pname:
                    parameters.append((pname, pvalue))
        return DirectiveMacro(
            name=self.directive_name,
            parameters=tuple(parameters),
            body=None,
        )

    def to_cfx(
        self,
        directive: DirectiveMacro,
        render_body: BodyRenderer,
    ) -> ET._Element:
        return build_structured_macro(
            name=self.name,
            parameters=list(directive.parameters),
            body=None,
        )


__all__ = ["JiraHandler"]
