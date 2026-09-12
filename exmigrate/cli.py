"""Command-line interface: ``exmigrate analyze|erd|migrate <files...>``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from exmigrate.analyzer import analyze_with_data
from exmigrate.contracts.adapter import IssueSeverity
from exmigrate.erd import to_mermaid
from exmigrate.service import TARGETS, run_migration


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="exmigrate", description="Migrate Excel to databases.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze_p = sub.add_parser("analyze", help="print the Schema IR for workbooks")
    analyze_p.add_argument("files", nargs="+", type=Path)

    erd_p = sub.add_parser("erd", help="print a Mermaid ER diagram for workbooks")
    erd_p.add_argument("files", nargs="+", type=Path)

    migrate_p = sub.add_parser("migrate", help="analyze and migrate workbooks")
    migrate_p.add_argument("files", nargs="+", type=Path)
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

    if not result.ir.tables:
        print("no tables found", file=sys.stderr)
        return 1

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
