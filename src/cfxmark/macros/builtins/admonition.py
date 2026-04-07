"""Admonition panels: ``info``, ``note``, ``warning``, ``tip``.

These map to Confluence's ``ac:structured-macro`` with an
``ac:rich-text-body`` containing arbitrary block content. On the
Markdown side we use GitHub-style callout blockquotes:

```markdown
> [!INFO] Optional title
> Body paragraph.
```

The upper-cased name (``INFO``/``NOTE``/``WARNING``/``TIP``) inside
``[!...]`` is how GitHub renders coloured admonitions in issues and
READMEs — this gives a good preview anywhere the markdown is rendered.
"""

from __future__ import annotations

import lxml.etree as ET

from cfxmark.ast import BlockNode, DirectiveMacro
from cfxmark.macros.registry import BodyParser, BodyRenderer
from cfxmark.xml_ns import AC, ac_attr, build_structured_macro


class AdmonitionHandler:
    """Handler for a single admonition flavour."""

    directive_name: str
    name: str

    def __init__(self, name: str) -> None:
        if name not in {"info", "note", "warning", "tip"}:
            raise ValueError(f"unsupported admonition name: {name!r}")
        self.name = name
        self.directive_name = name

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


__all__ = ["AdmonitionHandler"]
