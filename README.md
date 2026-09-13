# ExMigrate

Migrate Excel workbooks into relational databases (sheet = table) to SQLite
and PostgreSQL via CLI and a small web UI. Phase 2 adds same-workbook formula
analysis (derived columns), PK/FK inference and a Mermaid ERD; Phase 3 adds
cross-workbook references, a data-flow (lineage) graph and derived-column
include/exclude; Phase 4 translates formula columns into SQL views and a
pandas script so excluded columns can be recomputed in the database.
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
exmigrate formulas book.xlsx [--json]                        # functions used per sheet column
exmigrate lineage a.xlsx b.xlsx [--level file|sheet|column] [--json]   # data-flow graph
exmigrate migrate book.xlsx --target sqlite --exclude orders.total     # skip a (derived) column
exmigrate translate a.xlsx b.xlsx [--exclude T.C] [--format summary|sqlite|postgres|pandas|json]
exmigrate migrate book.xlsx --target sqlite --no-views      # skip recompute views / recompute.py
exmigrate migrate book.xlsx --target sqlite --out out/       # out/migration.db
exmigrate migrate book.xlsx --target postgres --dump --out out/   # out/migration.sql
PG_DSN_DEFAULT=postgresql://user:pass@host/db exmigrate migrate book.xlsx --target postgres
```

## Web

```bash
python -m exmigrate.web.app        # http://127.0.0.1:5000
```

Upload → review (rename tables/columns, change types, confirm PK/FK, view the
ERD tab, browse the **Formulas** tab: functions used per file / sheet column
with counts, referenced sheets and a sample formula; the **Data flow** tab
renders the lineage graph zoomable to files / sheets / columns; untick
**Include** to leave a column out of every target; the **Recompute** tab shows
each formula column's SQL/pandas translation and TODOs) → choose targets →
report with artifact downloads. Job state lives
under `./jobs/` (override with `EXMIGRATE_JOBS_DIR`); the **History** page
(`/jobs`) lists past jobs so you can reopen their review/report or delete them.
The REST API is described in `exmigrate/contracts/openapi.yaml`. The user
manual (`exmigrate/web/static/manual.ko.md`, `manual.en.md`) is rendered in-app
under **Manual** (`/manual?lang=ko|en`).

## Key inference

- A second `data_only=False` read collects formulas. Columns that are mostly
  formulas are marked `derived`; `VLOOKUP`/`XLOOKUP`/`INDEX`/`MATCH` and the
  conditional aggregates (`SUMIF(S)`, `COUNTIF(S)`, `AVERAGEIF(S)`, …) are
  recorded as lookup evidence, including `[Other.xlsx]Sheet!A1` external links
  when the other workbook is uploaded in the same job (otherwise a
  `missing_referenced_workbook` warning asks you to upload it).
  `INDIRECT`/`OFFSET` are never guessed: they appear as an explicit
  "unresolved" node in the lineage graph plus a warning.
- Lineage IR (`exmigrate/contracts/lineage.py`): column-level nodes/edges with
  ops `join`, `aggregate`, `calculate`, `copy`, `dynamic`; collapsible to sheet
  and file level. Excluded columns are dropped from DDL and data; the report
  lists them (`column_excluded`) so they can be recreated as SQL views.
- PK: unique + not-null column, preferring `id`, `*_id`, `code`, `no`.
- FK confidence: cross-workbook lookup 0.99, lookup + name + values 0.98, lookup 0.95, name + ≥95%
  containment 0.9, containment alone 0.7 (drawn dashed / "tentative" in the
  ERD when below 0.9). Everything is editable in the review UI.
- Adapters emit `FOREIGN KEY` constraints, load parents before children and
  break cycles by adding the constraint after the load (PostgreSQL); SQLite
  cannot add constraints later and reports them as warnings.

## Formula translation (recompute views)

`exmigrate/translate/` parses each derived column's formula (openpyxl
tokenizer → AST), resolves references against the Schema IR and emits SQLite
and PostgreSQL SQL plus a pandas expression:

| Excel | SQL | pandas |
| --- | --- | --- |
| `=C2`, `=D2*E2`, `IF`, `ROUND`, `LEFT`, … | expression on the row | vectorised Series expression |
| `VLOOKUP`/`XLOOKUP`/`INDEX(MATCH)` | correlated `SELECT … LIMIT 1` on the target table | `_lookup()` (merge) |
| `SUMIF(S)`/`COUNTIF(S)`/`AVERAGEIF(S)`/`MINIFS`/`MAXIFS` | correlated aggregate with the criteria as `WHERE` | `_cond_agg()` |
| `INDIRECT`/`OFFSET`, arrays, unknown functions | `NULL` + `-- TODO` comment with the original formula | `np.nan` + TODO |

Every migration writes `recompute_<dialect>.sql` and `recompute.py` next to
the database (unless `--no-views`) and creates one view per table with formula
columns, `<table>_v`: columns you **excluded** are recomputed under their own
name, columns you kept get a `<column>_calc` twin next to the cached Excel value
so both can be compared. Views are created in dependency order and read other
views when a formula depends on an excluded column. In dump mode the view SQL
is appended to `migration.sql`. `python recompute.py migration.db` does the
same in pandas; `recompute(tables)` is importable. Statuses per column: `ok`,
`partial` (translated with caveats, see notes) and `unsupported`.

## Verification

```bash
ruff check . && mypy && pytest -q
# PostgreSQL round-trip tests run when PG_DSN_DEFAULT is set:
docker run -d --name exm-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=exmigrate -p 55432:5432 postgres:16
PG_DSN_DEFAULT=postgresql://postgres:postgres@localhost:55432/exmigrate pytest -q
```

Fixtures are generator scripts in `fixtures/`; no `.xlsx` binaries are committed.
