from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from exmigrate.adapters import PostgresAdapter, SQLiteAdapter
from exmigrate.adapters.sql_common import plan_schema
from exmigrate.analyzer import analyze_with_data
from exmigrate.contracts.adapter import Adapter, IssueSeverity
from exmigrate.contracts.ir import ColumnIR, ColumnType, ForeignKey, SchemaIR, TableIR


def _table(name: str, *cols: ColumnIR) -> TableIR:
    return TableIR(
        name=name, source_file="x.xlsx", source_sheet=name, row_count=0, columns=list(cols)
    )


def _cyclic_ir() -> SchemaIR:
    """employees.dept_id -> departments, departments.manager_id -> employees, self-ref boss_id."""
    return SchemaIR(
        version=1,
        tables=[
            _table(
                "employees",
                ColumnIR(name="id", source_name="id", type=ColumnType.INTEGER, pk=True),
                ColumnIR(
                    name="dept_id",
                    source_name="dept_id",
                    type=ColumnType.INTEGER,
                    fk=ForeignKey(table="departments", column="id"),
                ),
                ColumnIR(
                    name="boss_id",
                    source_name="boss_id",
                    type=ColumnType.INTEGER,
                    fk=ForeignKey(table="employees", column="id"),
                ),
                ColumnIR(
                    name="ghost",
                    source_name="ghost",
                    type=ColumnType.TEXT,
                    fk=ForeignKey(table="nowhere", column="id"),
                ),
            ),
            _table(
                "departments",
                ColumnIR(name="id", source_name="id", type=ColumnType.INTEGER, pk=True),
                ColumnIR(
                    name="manager_id",
                    source_name="manager_id",
                    type=ColumnType.INTEGER,
                    fk=ForeignKey(table="employees", column="id"),
                ),
            ),
        ],
    )


def test_plan_schema_orders_parents_first_and_defers_cycles() -> None:
    plan = plan_schema(_cyclic_ir())
    assert plan.order == ["departments", "employees"]
    assert {(e.table, e.column) for e in plan.deferred} == {
        ("employees", "boss_id"),
        ("departments", "manager_id"),
    }
    assert [(e.table, e.column) for e in plan.inline_edges()] == [("employees", "dept_id")]
    assert {i.code for i in plan.issues} == {"fk_skipped", "fk_cycle"}


def test_postgres_dump_re_adds_deferred_constraints(tmp_path: Path) -> None:
    adapter = PostgresAdapter(mode="dump", dump_path=tmp_path / "cyc.sql")
    report = adapter.migrate(_cyclic_ir(), {})
    assert report.ok
    sql = (tmp_path / "cyc.sql").read_text()
    assert sql.index("CREATE TABLE departments") < sql.index("CREATE TABLE employees")
    assert "CONSTRAINT fk_employees_dept_id FOREIGN KEY(dept_id) REFERENCES departments (id)" in sql
    assert "ALTER TABLE employees ADD CONSTRAINT fk_employees_boss_id" in sql
    assert "ALTER TABLE departments ADD CONSTRAINT fk_departments_manager_id" in sql
    assert sql.rindex("ALTER TABLE") < sql.rindex("COMMIT;")


def test_sqlite_emits_foreign_keys_and_loads_in_order(clean_workbook: Path, tmp_path: Path) -> None:
    result = analyze_with_data([clean_workbook])
    result.ir.table("orders").name = "sales"
    for table in result.ir.tables:
        for col in table.columns:
            if col.fk is not None and col.fk.table == "orders":
                col.fk = ForeignKey(table="sales", column=col.fk.column, confidence=1.0)
    adapter = SQLiteAdapter(tmp_path / "fk.db")
    plan = adapter.plan(result.ir)
    assert [t.name for t in plan.tables] == ["customers", "sales", "order_items"]
    assert "FOREIGN KEY(customer_id) REFERENCES customers (customer_id)" in plan.tables[1].ddl
    report = adapter.migrate(result.ir, result.table_data())
    assert report.ok and not report.issues
    conn = sqlite3.connect(tmp_path / "fk.db")
    try:
        fks = conn.execute("PRAGMA foreign_key_list(order_items)").fetchall()
        assert [(row[2], row[3], row[4]) for row in fks] == [("sales", "order_id", "order_id")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_adapters_satisfy_protocol(tmp_path: Path) -> None:
    assert isinstance(SQLiteAdapter(tmp_path / "x.db"), Adapter)
    assert isinstance(PostgresAdapter(mode="dump", dump_path=tmp_path / "x.sql"), Adapter)


def test_sqlite_round_trip(clean_workbook: Path, tmp_path: Path) -> None:
    result = analyze_with_data([clean_workbook])
    result.ir.table("customers").column("customer_id").pk = True
    adapter = SQLiteAdapter(tmp_path / "out.db")

    plan = adapter.plan(result.ir)
    assert [t.name for t in plan.tables] == ["customers", "orders", "order_items"]
    assert "CREATE TABLE customers" in plan.tables[0].ddl

    report = adapter.migrate(result.ir, result.table_data())
    assert report.ok
    assert {t.name: t.rows_loaded for t in report.tables} == {
        "customers": 25,
        "orders": 60,
        "order_items": 150,
    }

    conn = sqlite3.connect(tmp_path / "out.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 25
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(customers)")}
        assert cols["customer_id"][5] == 1  # pk flag
        assert cols["credit_limit"][2] == "FLOAT"
        assert cols["signup_date"][2] == "DATE"
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE note IS NULL").fetchone()[0] > 0
        active = conn.execute("SELECT DISTINCT active FROM customers").fetchall()
        assert {row[0] for row in active} <= {0, 1}
    finally:
        conn.close()


def test_postgres_validate_flags_identifiers() -> None:
    ir = SchemaIR(
        tables=[
            TableIR(
                name="x" * 64,
                source_file="f.xlsx",
                source_sheet="S",
                columns=[
                    ColumnIR(name="select", source_name="select", type=ColumnType.TEXT),
                    ColumnIR(name="ok", source_name="ok", type=ColumnType.TEXT),
                ],
            )
        ]
    )
    adapter = PostgresAdapter(mode="dump", dump_path="unused.sql")
    codes = {(i.code, i.severity) for i in adapter.validate(ir)}
    assert ("identifier_too_long", IssueSeverity.ERROR) in codes
    assert ("reserved_word", IssueSeverity.WARNING) in codes
    report = adapter.migrate(ir, {})
    assert report.tables == [] and report.artifacts == []


def test_postgres_dump_mode(clean_workbook: Path, tmp_path: Path) -> None:
    result = analyze_with_data([clean_workbook])
    adapter = PostgresAdapter(mode="dump", dump_path=tmp_path / "out.sql")
    report = adapter.migrate(result.ir, result.table_data())
    assert report.ok
    sql = (tmp_path / "out.sql").read_text(encoding="utf-8")
    assert sql.startswith("BEGIN;")
    assert 'CREATE TABLE "customers"' in sql or "CREATE TABLE customers" in sql
    assert sql.count("INSERT INTO") == 3
    assert sql.rstrip().endswith("COMMIT;")


def test_postgres_plan_picks_copy_for_large(large_workbook: Path) -> None:
    result = analyze_with_data([large_workbook])
    adapter = PostgresAdapter(mode="dump", dump_path="unused.sql")
    plan = adapter.plan(result.ir)
    assert plan.tables[0].load_strategy.startswith("COPY")


def test_postgres_live_round_trip(clean_workbook: Path, pg_dsn: str) -> None:
    result = analyze_with_data([clean_workbook])
    result.ir.table("customers").column("customer_id").pk = True
    report = PostgresAdapter(dsn=pg_dsn).migrate(result.ir, result.table_data())
    assert report.ok, report
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone() == (25,)
        assert conn.execute("SELECT COUNT(*) FROM order_items").fetchone() == (150,)
        row = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='orders' AND column_name='ordered_at'"
        ).fetchone()
        assert row == ("timestamp without time zone",)


def test_postgres_live_copy_path(large_workbook: Path, pg_dsn: str) -> None:
    result = analyze_with_data([large_workbook])
    report = PostgresAdapter(dsn=pg_dsn).migrate(result.ir, result.table_data())
    assert report.ok, report
    assert report.tables[0].rows_loaded == 12_000
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT COUNT(*) FROM big").fetchone() == (12_000,)


@pytest.mark.parametrize("mode", ["live", "dump"])
def test_postgres_constructor_validation(mode: str) -> None:
    with pytest.raises(ValueError):
        PostgresAdapter(mode=mode)  # type: ignore[arg-type]
