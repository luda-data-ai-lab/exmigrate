"""Schema IR → Mermaid ``erDiagram`` text."""

from __future__ import annotations

import re

from exmigrate.contracts.ir import ColumnIR, SchemaIR

TENTATIVE_BELOW = 0.9

_MERMAID_TYPES = {
    "integer": "int",
    "float": "float",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "text": "text",
}
_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def to_mermaid(ir: SchemaIR) -> str:
    """Render the IR as a Mermaid ER diagram.

    FK edges with confidence below ``TENTATIVE_BELOW`` are drawn dashed and
    labelled ``tentative``; derived columns carry a ``derived`` comment.
    """
    lines = ["erDiagram"]
    ids = _entity_ids(ir)
    for table in ir.tables:
        entity = ids[table.name]
        alias = f'["{_quote(table.name)}"]' if entity != table.name else ""
        lines.append(f"    {entity}{alias} {{")
        for col in table.columns:
            lines.append("        " + _column_line(col))
        lines.append("    }")
    for table in ir.tables:
        for col in table.columns:
            fk = col.fk
            if fk is None or fk.table not in ids:
                continue
            tentative = fk.confidence < TENTATIVE_BELOW
            arrow = "}o..o|" if tentative else "}o--||"
            label = f"{col.name} -> {fk.column}"
            if tentative:
                label += f" (tentative {fk.confidence:.2f})"
            lines.append(f'    {ids[table.name]} {arrow} {ids[fk.table]} : "{_quote(label)}"')
    return "\n".join(lines) + "\n"


def _entity_ids(ir: SchemaIR) -> dict[str, str]:
    """Map table names to unique Mermaid-safe entity identifiers."""
    ids: dict[str, str] = {}
    used: set[str] = set()
    for table in ir.tables:
        base = _ident(table.name)
        candidate, n = base, 2
        while candidate in used:
            candidate, n = f"{base}_{n}", n + 1
        used.add(candidate)
        ids[table.name] = candidate
    return ids


def _column_line(col: ColumnIR) -> str:
    keys = []
    if col.pk:
        keys.append("PK")
    if col.fk is not None:
        keys.append("FK")
    parts = [_MERMAID_TYPES.get(col.type.value, "text"), _ident(col.name)]
    if keys:
        parts.append(",".join(keys))
    comments = []
    if col.derived:
        comments.append("derived")
    if not col.nullable and not col.pk:
        comments.append("not null")
    if comments:
        parts.append(f'"{_quote(", ".join(comments))}"')
    return " ".join(parts)


def _ident(name: str) -> str:
    cleaned = _IDENT_RE.sub("_", name).strip("_")
    if not cleaned:
        return "table"
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned


def _quote(text: str) -> str:
    return text.replace('"', "'")
