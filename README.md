# ExMigrate

Migrate Excel workbooks into relational databases. Phase 1: values-only
migration (sheet = table) to SQLite and PostgreSQL via CLI and a small web UI.
`SPEC.md` is authoritative; `DEVIN.md` holds the playbooks.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## CLI

```bash
exmigrate analyze book.xlsx                                  # print Schema IR
exmigrate migrate book.xlsx --target sqlite --out out/       # out/migration.db
exmigrate migrate book.xlsx --target postgres --dump --out out/   # out/migration.sql
PG_DSN_DEFAULT=postgresql://user:pass@host/db exmigrate migrate book.xlsx --target postgres
```

## Web

```bash
python -m exmigrate.web.app        # http://127.0.0.1:5000
```

Upload → review (rename tables/columns, change types, mark PK) → choose
targets → report with artifact downloads. Job state lives under `./jobs/`
(override with `EXMIGRATE_JOBS_DIR`). The REST API is described in
`exmigrate/contracts/openapi.yaml`.

## Verification

```bash
ruff check . && mypy && pytest -q
# PostgreSQL round-trip tests run when PG_DSN_DEFAULT is set:
docker run -d --name exm-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=exmigrate -p 55432:5432 postgres:16
PG_DSN_DEFAULT=postgresql://postgres:postgres@localhost:55432/exmigrate pytest -q
```

Fixtures are generator scripts in `fixtures/`; no `.xlsx` binaries are committed.
