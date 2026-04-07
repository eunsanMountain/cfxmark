"""Confluence ``expand`` macro: a collapsible section with a title.

Rendered as a directive:

```
::: expand title="Feature Roadmap"
Body content
:::
```
"""

from __future__ import annotations

import lxml.etree as ET

from cfxmark.ast import BlockNode, DirectiveMacro
from cfxmark.macros.registry import BodyParser, BodyRenderer
from cfxmark.xml_ns import AC, ac_attr, build_structured_macro


class ExpandHandler:
    name = "expand"
    directive_name = "expand"

    def from_cfx(
        self,
        element: ET._Element,
        parse_body: BodyParser,
    ) -> DirectiveMacro | None:
        parameters: list[tuple[str, str]] = []
        body_blocks: tuple[BlockNode, ...] = ()
        for child in element:
            local = child.tag.rsplit("}", 1)[-1]
            if local == "parameter":
                pname = child.get(ac_attr("name")) or ""
                pvalue = (child.text or "").strip()
                if pname:
                    parameters.append((pname, pvalue))
            elif local == "rich-text-body":
                body_blocks = parse_body(child)
        return DirectiveMacro(
            name=self.directive_name,
            parameters=tuple(parameters),
            body=body_blocks,
        )

    def to_cfx(
        self,
        directive: DirectiveMacro,
        render_body: BodyRenderer,
    ) -> ET._Element:
        body_elem = AC("rich-text-body")
        if directive.body:
            for child_elem in render_body(directive.body):
                body_elem.append(child_elem)
        return build_structured_macro(
            name=self.name,
            parameters=list(directive.parameters),
            body=body_elem,
        )


__all__ = ["ExpandHandler"]
