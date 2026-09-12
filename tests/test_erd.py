from __future__ import annotations

from pathlib import Path

from exmigrate.analyzer import analyze
from exmigrate.contracts.ir import ColumnIR, ColumnType, ForeignKey, SchemaIR, TableIR
from exmigrate.erd import to_mermaid


def test_mermaid_from_fixture(clean_workbook: Path) -> None:
    ir, _ = analyze([clean_workbook])
    text = to_mermaid(ir)
    lines = text.splitlines()
    assert lines[0] == "erDiagram"
    assert "    customers {" in lines
    assert "        int customer_id PK" in lines
    assert '        text customer_name "derived"' in lines
    assert '    orders }o--|| customers : "customer_id -> customer_id"' in lines
    assert '    order_items }o--|| orders : "order_id -> order_id"' in lines
    assert "tentative" not in text


def test_mermaid_tentative_and_unsafe_names() -> None:
    ir = SchemaIR(
        version=1,
        tables=[
            TableIR(
                name="고객 목록",
                source_file="a.xlsx",
                source_sheet="고객",
                row_count=1,
                columns=[ColumnIR(name="id", source_name="id", type=ColumnType.INTEGER, pk=True)],
            ),
            TableIR(
                name="orders",
                source_file="a.xlsx",
                source_sheet="orders",
                row_count=1,
                columns=[
                    ColumnIR(
                        name="cust",
                        source_name="cust",
                        type=ColumnType.INTEGER,
                        fk=ForeignKey(table="고객 목록", column="id", confidence=0.7),
                    ),
                    ColumnIR(
                        name="dangling",
                        source_name="dangling",
                        type=ColumnType.INTEGER,
                        fk=ForeignKey(table="missing", column="id"),
                    ),
                ],
            ),
        ],
    )
    text = to_mermaid(ir)
    assert '    table["고객 목록"] {' in text
    assert '    orders }o..o| table : "cust -> id (tentative 0.70)"' in text
    assert "missing" not in text
