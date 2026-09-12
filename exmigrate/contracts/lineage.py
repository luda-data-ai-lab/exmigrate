"""Lineage IR: where formula columns get their data from.

Nodes are files, sheets or columns (one kind per zoom level) plus
``dynamic_reference`` placeholders for ``INDIRECT``/``OFFSET`` sources that
cannot be resolved statically. Edges carry the operation that moves data
(``copy``, ``calculate``, ``join``, ``aggregate`` or ``dynamic``) and the number
of formula cells that produced them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LineageLevel = Literal["file", "sheet", "column"]
LEVELS: tuple[LineageLevel, ...] = ("file", "sheet", "column")
NodeKind = Literal["file", "sheet", "column", "dynamic_reference"]
MISSING_SUFFIX = " (missing)"


class LineageNode(BaseModel):
    """A source or destination of data."""

    id: str
    kind: NodeKind
    label: str
    file: str
    sheet: str | None = None
    column: str | None = None
    missing: bool = False


class LineageEdge(BaseModel):
    """Data flowing from ``source`` into ``target`` (serialised as ``from``/``to``)."""

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    op: str
    formula_count: int = 0


class LineageIR(BaseModel):
    """Lineage graph at one zoom level."""

    version: int = 1
    level: LineageLevel = "column"
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)

    def node(self, node_id: str) -> LineageNode:
        """Return the node called ``node_id`` or raise ``KeyError``."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def at_level(self, level: LineageLevel) -> LineageIR:
        """Collapse column nodes to ``level``; dynamic nodes are kept as they are."""
        if level == "column" or self.level != "column":
            return self
        mapping: dict[str, str] = {}
        nodes: dict[str, LineageNode] = {}
        for node in self.nodes:
            if node.kind == "dynamic_reference":
                mapping[node.id] = node.id
                nodes[node.id] = node
                continue
            group = _group(node, level)
            mapping[node.id] = group.id
            existing = nodes.get(group.id)
            if existing is None:
                nodes[group.id] = group
            elif not node.missing and existing.missing:
                existing.missing = False
                existing.label = existing.label.removesuffix(MISSING_SUFFIX)
        merged: dict[tuple[str, str, str], int] = {}
        for edge in self.edges:
            src, dst = mapping[edge.source], mapping[edge.target]
            if src == dst:
                continue
            key = (src, dst, edge.op)
            merged[key] = merged.get(key, 0) + edge.formula_count
        return LineageIR(
            level=level,
            nodes=list(nodes.values()),
            edges=[
                LineageEdge(source=s, target=t, op=op, formula_count=n)
                for (s, t, op), n in merged.items()
            ],
        )


def _group(node: LineageNode, level: LineageLevel) -> LineageNode:
    suffix = MISSING_SUFFIX if node.missing else ""
    if level == "file":
        return LineageNode(
            id=f"file:{node.file}",
            kind="file",
            label=node.file + suffix,
            file=node.file,
            missing=node.missing,
        )
    sheet = node.sheet or ""
    return LineageNode(
        id=f"sheet:{node.file}/{sheet}",
        kind="sheet",
        label=f"{node.file} / {sheet}{suffix}",
        file=node.file,
        sheet=sheet,
        missing=node.missing,
    )
