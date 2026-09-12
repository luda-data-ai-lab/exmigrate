from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from exmigrate.cli import main


def test_cli_analyze(clean_workbook: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["analyze", str(clean_workbook)]) == 0
    ir = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in ir["tables"]] == ["customers", "orders", "order_items"]


def test_cli_erd(clean_workbook: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["erd", str(clean_workbook)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("erDiagram\n")
    assert "orders }o--|| customers" in out


def test_cli_migrate_sqlite_and_dump(
    clean_workbook: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "migrate",
            str(clean_workbook),
            "--target",
            "sqlite",
            "--target",
            "postgres",
            "--dump",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "== sqlite" in out and "== postgres" in out
    conn = sqlite3.connect(tmp_path / "migration.db")
    assert conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 150
    conn.close()
    assert (tmp_path / "migration.sql").exists()


def test_cli_migrate_postgres_live(clean_workbook: Path, tmp_path: Path, pg_dsn: str) -> None:
    code = main(
        [
            "migrate",
            str(clean_workbook),
            "--target",
            "postgres",
            "--dsn",
            pg_dsn,
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0


def test_cli_no_tables(tmp_path: Path) -> None:
    csv = tmp_path / "x.csv"
    csv.write_text("a\n1\n")
    assert main(["migrate", str(csv), "--target", "sqlite", "--out", str(tmp_path)]) == 1
