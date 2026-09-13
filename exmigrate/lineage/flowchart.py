"""Lineage IR → Mermaid ``flowchart`` text."""

from __future__ import annotations

from exmigrate.contracts.lineage import LineageIR, LineageNode

_ARROWS = {"copy": "-.->", "dynamic": "-.->"}


def to_flowchart(lineage: LineageIR) -> str:
    """Render ``lineage`` left-to-right, grouping nodes by file (and sheet at column level)."""
    lines = ["flowchart LR"]
    ids = {node.id: f"n{i}" for i, node in enumerate(lineage.nodes)}
    grouped = _grouped(lineage)
    for gi, ((file, sheet), nodes) in enumerate(grouped.items()):
        indent = "    "
        if lineage.level != "file":
            title = file if sheet is None else f"{file} / {sheet}"
            lines.append(f'    subgraph g{gi}["{_quote(title)}"]')
            indent = "        "
        for node in nodes:
            lines.append(indent + _node_line(ids[node.id], node))
        if lineage.level != "file":
            lines.append("    end")
    for edge in lineage.edges:
        arrow = _ARROWS.get(edge.op, "-->")
        label = edge.op if edge.formula_count <= 0 else f"{edge.op} ×{edge.formula_count}"
        lines.append(f'    {ids[edge.source]} {arrow}|"{_quote(label)}"| {ids[edge.target]}')
    lines.append("    classDef missing stroke-dasharray: 4 4,color:#888;")
    lines.append("    classDef dynamic stroke-dasharray: 2 2,fill:#fff3cd;")
    missing = [ids[n.id] for n in lineage.nodes if n.missing]
    dynamic = [ids[n.id] for n in lineage.nodes if n.kind == "dynamic_reference"]
    if missing:
        lines.append(f"    class {','.join(missing)} missing;")
    if dynamic:
        lines.append(f"    class {','.join(dynamic)} dynamic;")
    return "\n".join(lines) + "\n"


def _grouped(lineage: LineageIR) -> dict[tuple[str, str | None], list[LineageNode]]:
    out: dict[tuple[str, str | None], list[LineageNode]] = {}
    for node in lineage.nodes:
        if node.kind == "dynamic_reference":
            key: tuple[str, str | None] = (node.file, "(dynamic)")
        elif lineage.level == "column":
            key = (node.file, node.sheet)
        else:
            key = (node.file, None)
        out.setdefault(key, []).append(node)
    return out


def _node_line(ident: str, node: LineageNode) -> str:
    if node.kind == "dynamic_reference":
        return f'{ident}{{{{"{_quote(node.label)}"}}}}'
    if node.kind == "file":
        return f'{ident}[("{_quote(node.label)}")]'
    return f'{ident}["{_quote(node.label)}"]'


def _quote(text: str) -> str:
    return text.replace('"', "#quot;")
