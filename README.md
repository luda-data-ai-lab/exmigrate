# ExMigrate

Migrate Excel workbooks into relational databases (sheet = table) to SQLite
and PostgreSQL via CLI and a small web UI. Phase 2 adds same-workbook formula
analysis (derived columns), PK/FK inference and a Mermaid ERD.
`SPEC.md` is authoritative; `DEVIN.md` holds the playbooks.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## CLI

```bash
exmigrate analyze book.xlsx                                  # print Schema IR
exmigrate erd book.xlsx > erd.mmd                            # Mermaid erDiagram
exmigrate migrate book.xlsx --target sqlite --out out/       # out/migration.db
exmigrate migrate book.xlsx --target postgres --dump --out out/   # out/migration.sql
PG_DSN_DEFAULT=postgresql://user:pass@host/db exmigrate migrate book.xlsx --target postgres
```

## Web

```bash
python -m exmigrate.web.app        # http://127.0.0.1:5000
```

Upload → review (rename tables/columns, change types, confirm PK/FK, view the
ERD tab) → choose targets → report with artifact downloads. Job state lives
under `./jobs/` (override with `EXMIGRATE_JOBS_DIR`); the **History** page
(`/jobs`) lists past jobs so you can reopen their review/report or delete them.
The REST API is described in `exmigrate/contracts/openapi.yaml`.

## Key inference

- A second `data_only=False` read collects formulas. Columns that are mostly
  formulas are marked `derived`; same-file `VLOOKUP`/`XLOOKUP` targets are
  recorded as lookup evidence. External links, `INDIRECT`/`OFFSET` and
  aggregates are out of scope (Phase 3).
- PK: unique + not-null column, preferring `id`, `*_id`, `code`, `no`.
- FK confidence: lookup + name + values 0.98, lookup 0.95, name + ≥95%
  containment 0.9, containment alone 0.7 (drawn dashed / "tentative" in the
  ERD when below 0.9). Everything is editable in the review UI.
- Adapters emit `FOREIGN KEY` constraints, load parents before children and
  break cycles by adding the constraint after the load (PostgreSQL); SQLite
  cannot add constraints later and reports them as warnings.

## Verification

```bash
ruff check . && mypy && pytest -q
# PostgreSQL round-trip tests run when PG_DSN_DEFAULT is set:
docker run -d --name exm-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=exmigrate -p 55432:5432 postgres:16
PG_DSN_DEFAULT=postgresql://postgres:postgres@localhost:55432/exmigrate pytest -q
```

Fixtures are generator scripts in `fixtures/`; no `.xlsx` binaries are committed.
