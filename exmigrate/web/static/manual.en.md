# ExMigrate User Manual

ExMigrate analyzes Excel workbooks and migrates them to a relational database (SQLite / PostgreSQL).
Each sheet becomes a table; formulas, PK/FK relationships and cross-workbook references are inferred automatically, reviewed and edited in the web UI, then migrated.

| Phase | Features |
| --- | --- |
| Phase 1 | Values-only migration (sheet → table), SQLite / PostgreSQL (live · dump), CLI + web UI |
| Phase 2 | Formula analysis (derived columns), PK/FK inference, Mermaid ERD, FK constraints |
| Phase 3 | External workbook references (`[Other.xlsx]Sheet!A1`), Data flow (lineage) graph, derived column Include/Exclude |
| Phase 4 | Translate formula columns into SQL views (SQLite/PostgreSQL) + a pandas script (Recompute) |

---

## 1. Installation and startup

Python 3.10 or newer is required.

```bash
git clone https://github.com/luda-data-ai-lab/exmigrate.git
cd exmigrate
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

python -m exmigrate.web.app          # web UI → http://127.0.0.1:5000  (override with the PORT env var)
```

Generate sample workbooks:

```bash
python -m fixtures.clean_three_table sample.xlsx   # single file: Customers / Orders / Order Items (VLOOKUP, XLOOKUP)
python -m fixtures.cross_file out/                 # 4 files: customers, products, orders, summary (cross-file refs, SUMIF, INDIRECT…)
```

> Generated samples contain **no cached formula results**. Open and save each file once in Excel so derived column values are migrated as well.

Environment variables

| Variable | Meaning |
| --- | --- |
| `PORT` | Web server port (default 5000) |
| `EXMIGRATE_JOBS_DIR` | Job storage directory (default `./jobs`) |
| `PG_DSN_DEFAULT` | Default PostgreSQL connection string (used when the DSN field is left empty) |

---

## 2. Web UI workflow

Top menu: **Upload** · **History** · **Manual**

### 2.1 Upload
Drag and drop `.xlsx` files or pick them with the **Choose files…** button (you can add more than once; the count is shown as "N files selected"). Workbooks that reference each other must be uploaded **together in one job** so cross-file FKs and data flow are connected. Non-ASCII (e.g. Korean) file names are preserved.

### 2.2 Review
Inspect and edit the automatic analysis. Press **Save** so the changes are picked up by the following steps (ERD, Recompute, migration).

**Schema tab**

| Item | Description |
| --- | --- |
| Table / column name | Name created in the DB. Defaults to the sheet/header name normalized to snake_case |
| Type | `integer` / `float` / `text` / `date` / `datetime` / `boolean` — inferred, editable |
| PK | Suggested for columns whose values are all unique and non-empty (`id`, `*_id`, `code`, `no` preferred) |
| FK | Choose the referenced `table.column`. Confidence (0.7–0.99) is shown alongside |
| derived badge | Column filled by formulas |
| **Include** | Untick to drop the column from DDL/data (use when you want the DB to recompute a derived column) |

FK confidence: cross-file lookup 0.99 · lookup + name + values 0.98 · lookup 0.95 · name + ≥95 % value containment 0.9 · value containment only 0.7. Below 0.9 the edge is drawn dashed (tentative) in the ERD.

**ERD tab** — Mermaid `erDiagram`. Refreshes after you edit PK/FK and Save.

**Formulas tab** — functions used per file (name · count), and per sheet/column: functions, formula cell count, referenced sheets, sample formula.

**Data flow tab** — formula-based data flow graph, zoomable to file / sheet / column level. Edge kinds:
`join` (VLOOKUP · XLOOKUP · INDEX/MATCH), `aggregate` (SUMIF · COUNTIF family), `calculate` (arithmetic etc.), `copy` (`=C2`), `dynamic` (INDIRECT · OFFSET — source cannot be traced, shown as a separate node).
Referencing a workbook that was not uploaded produces a `missing referenced workbook` warning.

**Recompute tab (Phase 4)** — shows the SQL/pandas translation for every formula column.

| Column | Description |
| --- | --- |
| Status | `ok` fully translated · `partial` partly translated (see notes) · `unsupported` cannot be translated |
| Formula | Original Excel formula |
| SQLite SQL / pandas | Generated expression |
| Notes | TODOs and caveats (e.g. `TODO INDIRECT() resolves its target at runtime; rewrite by hand`) |

Links at the top download the SQLite view SQL / PostgreSQL view SQL / pandas script directly.
Columns with **Include unticked and saved** are shown as "excluded → recomputed" and are recomputed under their original name in the view.

### 2.3 Choose targets
- **SQLite** — written to a file (`migration.db`). No connection details needed.
- **PostgreSQL**
  - **Live**: enter a DSN (`postgresql://user:pw@host:5432/db`). Empty → `PG_DSN_DEFAULT`.
  - **Dump**: no connection; only a `migration.sql` file is produced.

### 2.4 Report
Rows loaded per table, warnings/errors (issues), and downloadable artifacts.

| Artifact | Content |
| --- | --- |
| `migration.db` / `migration.sql` | Migration result (views included) |
| `recompute_sqlite.sql` / `recompute_postgres.sql` | View SQL that recomputes formula columns |
| `recompute.py` | The same logic as a pandas script |

### 2.5 History
List of past jobs (time · files · tables · status · targets). Reopen Review/Report, delete.

---

## 3. What the migration result means

- **Tables**: sheet = table. Multiple uploaded files land in one DB/schema.
- **PK/FK**: created as real constraints. Tables are loaded parent → child; cyclic references are added after load with `ALTER TABLE … ADD CONSTRAINT` on PostgreSQL and reported as `fk_not_created` on SQLite.
  - On SQLite, FK enforcement requires `PRAGMA foreign_keys=ON`.
- **Formula (derived) columns**: the **values Excel cached** are stored, not the formulas. Without a cache the column is NULL and a `formula_cache_empty` warning is raised.
- **Columns with Include unticked**: dropped from DDL/data and reported as `column_excluded`; they are recomputed in the Recompute views instead.

### 3.1 Recompute views (Phase 4)

For every table that has formula columns a `<table>_v` view is created (disable with `--no-views`).

| Column state | In the view |
| --- | --- |
| Include **unticked** (excluded) | Recomputed under the **original name** (`orders_v.customer_name`) |
| Include **ticked** (included) | Cached Excel value kept; a `<column>_calc` column is added for comparison (`orders_v.order_total`, `orders_v.order_total_calc`) |
| unsupported | `NULL` + SQL comment `-- TODO … original formula` |

Translation rules

| Excel | SQL | pandas |
| --- | --- | --- |
| `=C2`, `=D2*E2`, `IF`, `ROUND`, `LEFT`, `TEXT`, `IFERROR` … | row-level expression | vectorized operation |
| `VLOOKUP` / `XLOOKUP` / `INDEX(MATCH)` | correlated subquery on the target table (`SELECT … LIMIT 1`) | `_lookup()` (merge) |
| `SUMIF(S)` / `COUNTIF(S)` / `AVERAGEIF(S)` / `MINIFS` / `MAXIFS` | correlated aggregate with the criteria moved into `WHERE` | `_cond_agg()` |
| `INDIRECT` / `OFFSET`, array formulas, unsupported functions | `NULL` + TODO | `np.nan` + TODO |

Dependencies: views are created in dependency order, and a formula referencing an **excluded** column of another table reads that table's `_v` view.
**Included** derived columns are treated as trusted input (cache), so to recompute a whole chain untick Include on the intermediate columns as well.
Example: to recompute `summary.revenue` (SUMIF of `orders.order_total`), exclude `orders.order_total` too.

Running the pandas script:

```bash
python recompute.py migration.db          # reads the tables and prints the recomputed result
# or
from recompute import recompute, load
tables = recompute(load(sqlite3.connect("migration.db")))
```

In PostgreSQL dump mode the view SQL is appended to `migration.sql` after `COMMIT`.

---

## 4. CLI

```bash
exmigrate analyze  book.xlsx                       # print the Schema IR (JSON)
exmigrate erd      book.xlsx > erd.mmd             # Mermaid ERD
exmigrate formulas book.xlsx [--json]              # functions used per sheet/column
exmigrate lineage  a.xlsx b.xlsx [--level file|sheet|column] [--json]
exmigrate translate a.xlsx b.xlsx [--exclude T.C]... [--format summary|sqlite|postgres|pandas|json]

exmigrate migrate book.xlsx --target sqlite   --out out/                 # out/migration.db
exmigrate migrate book.xlsx --target postgres --dump --out out/          # out/migration.sql
exmigrate migrate book.xlsx --target postgres --dsn postgresql://u:p@h:5432/db
exmigrate migrate a.xlsx b.xlsx --target sqlite --exclude orders.customer_name --exclude order_items.line_total
exmigrate migrate book.xlsx --target sqlite --no-views                   # skip views / recompute.py
exmigrate migrate book.xlsx --target sqlite --json                       # also print the report as JSON
```

`--exclude` takes `table.column`, may be repeated, and exits with code 2 if the column does not exist.

---

## 5. REST API

Base path `/api`. Full schema: `exmigrate/contracts/openapi.yaml`.

| Method | Path | Description |
| --- | --- | --- |
| POST | `/upload` | multipart `files` upload → `201 {job_id}` |
| GET | `/jobs` | list jobs |
| DELETE | `/jobs/{id}` | delete a job |
| GET / PUT | `/jobs/{id}/schema` | read / save the Schema IR |
| GET | `/jobs/{id}/erd` | Mermaid ERD text |
| GET | `/jobs/{id}/formulas` | function inventory |
| GET | `/jobs/{id}/lineage?level=file\|sheet\|column` | Lineage IR / Mermaid |
| GET | `/jobs/{id}/translation?format=json\|sqlite\|postgres\|pandas` | formula translation (based on the saved IR) |
| POST | `/jobs/{id}/migrate` | `{"targets":["sqlite","postgres"],"configs":{"postgres":{"dsn":"…","mode":"live\|dump"}}}` → `202` |
| GET | `/jobs/{id}/status` | progress and report |
| GET | `/jobs/{id}/artifacts/{name}` | download an artifact |

---

## 6. Warning and error codes

| Code | Meaning |
| --- | --- |
| `no_header` / `blank_header` / `no_columns` | Header not found / empty header / no columns |
| `duplicate_table` / `reserved_word` / `identifier_too_long` | Name clash · reserved word · length limit (name adjusted automatically) |
| `unsupported_file` | File is not `.xlsx` |
| `formula_cache_empty` | Formula column has no cached values; loaded as NULL |
| `missing_referenced_workbook` | Referenced workbook was not uploaded together |
| `dynamic_reference` | INDIRECT/OFFSET — source cannot be traced |
| `fk_skipped` / `fk_not_created` / `fk_cycle` | FK target is not a PK · type mismatch / cannot be added on SQLite / cyclic reference handling |
| `column_excluded` | Column dropped because Include was unticked |
| `view_not_created` / `views_skipped` | Recompute view creation failed / views skipped because a table failed to load |

---

## 7. Checking the result in the DB

```bash
# SQLite
sqlite3 migration.db
.tables                                 # tables and *_v views
.schema orders                          # FOREIGN KEY clauses
PRAGMA foreign_keys=ON; PRAGMA foreign_key_check;
SELECT order_id, customer_name, order_total, order_total_calc FROM orders_v LIMIT 5;
```

```sql
-- PostgreSQL
\d orders
\dv                                     -- list views
SELECT conname, conrelid::regclass, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='f';
```

---

## 8. FAQ

- **All derived columns are NULL.** The file was generated by code or Excel did not store cached results. Open and save it in Excel and upload again, or untick Include to recompute in a view.
- **XLOOKUP shows `#NAME?`.** Excel 2019 and earlier lack XLOOKUP. Replace it with INDEX/MATCH; the translation is identical.
- **No FK was created.** If the target column is not a PK or types differ, the FK is skipped (`fk_skipped`). Fix PK/type in Review and migrate again.
- **View values are 0 or NULL.** An intermediate derived column the formula depends on may still be Included (with NULL cache). Untick Include on that column too to recompute the chain.
