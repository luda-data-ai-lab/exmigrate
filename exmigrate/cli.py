"""Command-line interface: ``exmigrate analyze|erd|formulas|lineage|migrate <files...>``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from exmigrate.analyzer import analyze_with_data
from exmigrate.contracts.adapter import IssueSeverity
from exmigrate.contracts.lineage import LEVELS
from exmigrate.erd import to_mermaid
from exmigrate.lineage import to_flowchart
from exmigrate.service import TARGETS, run_migration


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="exmigrate", description="Migrate Excel to databases.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze_p = sub.add_parser("analyze", help="print the Schema IR for workbooks")
    analyze_p.add_argument("files", nargs="+", type=Path)

    erd_p = sub.add_parser("erd", help="print a Mermaid ER diagram for workbooks")
    erd_p.add_argument("files", nargs="+", type=Path)

    formulas_p = sub.add_parser("formulas", help="list the functions used per workbook column")
    formulas_p.add_argument("files", nargs="+", type=Path)
    formulas_p.add_argument("--json", action="store_true", help="emit the inventory as JSON")

    lineage_p = sub.add_parser("lineage", help="print the formula data-flow (Lineage IR)")
    lineage_p.add_argument("files", nargs="+", type=Path)
    lineage_p.add_argument("--level", choices=LEVELS, default="column", help="zoom level")
    lineage_p.add_argument(
        "--json", action="store_true", help="emit the Lineage IR as JSON instead of Mermaid"
    )

    migrate_p = sub.add_parser("migrate", help="analyze and migrate workbooks")
    migrate_p.add_argument("files", nargs="+", type=Path)
    migrate_p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="TABLE.COLUMN",
        help="skip a column (repeatable), e.g. derived ones to recompute in the database",
    )
    migrate_p.add_argument("--target", choices=TARGETS, action="append", required=True)
    migrate_p.add_argument("--out", type=Path, default=Path("out"), help="artifact directory")
    migrate_p.add_argument("--dsn", help="PostgreSQL DSN (default: $PG_DSN_DEFAULT)")
    migrate_p.add_argument(
        "--dump", action="store_true", help="write a .sql dump instead of connecting"
    )
    migrate_p.add_argument("--json", action="store_true", help="also print reports as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    result = analyze_with_data(args.files)
    for issue in result.issues:
        print(f"[{issue.severity.value}] {issue.message}", file=sys.stderr)

    if args.command == "analyze":
        print(result.ir.model_dump_json(indent=2))
        return 0
    if args.command == "erd":
        print(to_mermaid(result.ir), end="")
        return 0
    if args.command == "formulas":
        if args.json:
            print(result.formulas.model_dump_json(indent=2))
            return 0
        for file, columns in result.formulas.by_file().items():
            totals = result.formulas.function_totals(file)
            summary = ", ".join(f"{n} x{c}" for n, c in totals.items()) or "(no function calls)"
            print(f"== {file}: {summary}")
            for col in columns:
                funcs = ", ".join(f"{n} x{c}" for n, c in col.functions.items()) or "-"
                refs = f"  -> {', '.join(col.references)}" if col.references else ""
                print(
                    f"  {col.sheet}!{col.column:<24} {col.formula_cells:>6}/{col.row_count:<6} "
                    f"{funcs}{refs}  {col.sample}"
                )
        if not result.formulas.columns:
            print("no formulas found")
        return 0
    if args.command == "lineage":
        lineage = result.lineage.at_level(args.level)
        if args.json:
            print(lineage.model_dump_json(indent=2, by_alias=True))
        else:
            print(to_flowchart(lineage), end="")
        return 0

    if not result.ir.tables:
        print("no tables found", file=sys.stderr)
        return 1

    for spec in args.exclude:
        table_name, _, column_name = spec.rpartition(".")
        try:
            result.ir.table(table_name).column(column_name).include = False
        except KeyError:
            print(f"--exclude: unknown column '{spec}'", file=sys.stderr)
            return 2

    configs: dict[str, dict[str, object]] = {
        "postgres": {"mode": "dump" if args.dump else "live", "dsn": args.dsn or ""}
    }
    reports = run_migration(result.ir, result.table_data(), args.target, configs, args.out)
    ok = True
    for report in reports:
        print(f"== {report.target}")
        for table in report.tables:
            status = "ok" if table.ok else f"FAILED: {table.error}"
            print(f"  {table.name:<30} {table.rows_loaded:>8} rows  {status}")
        for issue in report.issues:
            print(f"  [{issue.severity.value}] {issue.message}")
        for artifact in report.artifacts:
            print(f"  artifact: {artifact}")
        blocked = any(i.severity is IssueSeverity.ERROR for i in report.issues)
        ok = ok and report.ok and not blocked and bool(report.tables)
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in reports], indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
