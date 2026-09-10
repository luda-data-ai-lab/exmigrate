# ExMigrate — Technical Specification (v2, Phased)

Standalone project in a **new repository (`exmigrate`)**, built from scratch with no relationship to any existing codebase.

## 1. Overview

A Flask web application that ingests Excel workbooks and migrates their tables and data into relational targets. Capability grows across three phases, each shipping a usable product:

| Phase | Question it answers | Scope | Explicitly excluded |
|---|---|---|---|
| **1 — Core migration** | Does migration work at all? | Sheet = table, values only, load into SQLite & PostgreSQL | Formula analysis, FK inference, ERD, SharePoint |
| **2 — Simple formula analysis** | Can it understand relationships? | Same-file formulas (plain refs, arithmetic, basic VLOOKUP), name/value-based PK·FK inference, Mermaid ERD, review/edit UI | Cross-file references, lineage flow diagram, dynamic refs |
| **3 — Complex formula analysis** | Does it survive real-world workbooks? | Cross-file external references, aggregate functions (SUMIF*), dynamic refs (INDIRECT/OFFSET) as untracked nodes, data-lineage flow diagram, derived-column include/exclude, SharePoint List adapter | — |

Each phase is a strict superset of the previous one: Phase 1 establishes the minimal Schema IR, Phase 2 enriches it with keys/relations, Phase 3 adds the Lineage IR. No rework between phases.

## 2. Architecture

```
Flask web UI → Analyzer (pandas/openpyxl) → Schema IR (JSON)
                     │                       ├→ ERD generator (Mermaid)          [Phase 2]
                     │                       └→ Target adapters
                     │                            ├ SQLiteAdapter                [Phase 1]
                     │                            ├ PostgresAdapter              [Phase 1]
                     │                            └ SharePointAdapter            [Phase 3]
                     └→ Formula lineage analyzer → Lineage IR → data-flow view   [Phase 3]
```

**Core principle:** everything derives from the IR. Adapters and UI never read Excel directly. The IR schema is designed once (with all fields), and early phases simply leave later-phase fields empty — so the contract never breaks.

## 3. Phase 1 — Core Migration

### 3.1 Analyzer (minimal)
- Read values only: `openpyxl` with `data_only=True` (Excel's cached computed values). Formula cells therefore migrate as static values in this phase.
- One sheet → one table. Header row = first row with ≥80% non-null string cells.
- Type inference per column (sample ≤10,000 rows): `integer | float | boolean | date | datetime | text`, plus null ratio and max length.
- No PK/FK inference. Optional: user manually marks a PK column in the UI.

### 3.2 Schema IR (full shape, minimally populated)

```json
{
  "version": 1,
  "tables": [{
    "name": "customer", "source_sheet": "Customers", "row_count": 1200,
    "columns": [{
      "name": "customer_id", "type": "integer", "nullable": false,
      "pk": false, "fk": null, "derived": false
    }]
  }]
}
```

`pk`, `fk`, `derived` exist from day one; Phase 1 just doesn't populate them automatically.

### 3.3 Adapters
- Common Protocol: `validate(ir) -> list[Issue]`, `plan(ir) -> MigrationPlan`, `migrate(ir, data) -> MigrationReport`.
- **SQLiteAdapter**: SQLAlchemy DDL from IR; bulk insert, one transaction per table; artifact = downloadable `.db`.
- **PostgresAdapter**: live DSN mode and `.sql` dump mode; `COPY` (`copy_expert`) for tables >10k rows; `validate()` flags identifiers >63 chars and reserved words.

### 3.4 Web UI
- Upload page: drag-and-drop, multiple `.xlsx`.
- Simple review: table list with inferred types, editable table/column names, optional manual PK checkbox.
- Target selection (SQLite / Postgres + DSN form) → migrate → report page with row counts and artifact downloads.
- Bootstrap 5, vanilla JS, no build step.

### 3.5 Done when
- CLI **and** web path both migrate a multi-sheet workbook into SQLite and Postgres with correct types and row counts.

## 4. Phase 2 — Simple Formula Analysis

### 4.1 Analyzer additions
- Second read pass with `data_only=False` to obtain formula strings (same-file scope only).
- Handled formula families: plain references (`=A2`, `=Sheet2!B3`), arithmetic, basic `VLOOKUP`/`XLOOKUP` within the same workbook.
- PK inference: unique + not-null; prefer names `id`, `*_id`, `code`, `no`.
- FK inference, two signals with a confidence score:
  1. Name match (`customer_id` ↔ table `customer` PK).
  2. Value containment: ≥95% of non-null values exist in the referenced PK column.
  3. Same-file VLOOKUP targets count as strong FK evidence (confidence 0.95).
- Columns populated by formulas get `derived: true` (informational in this phase).

### 4.2 ERD
- Schema IR → Mermaid `erDiagram` text; FK edges with confidence <0.9 annotated as tentative.
- Review UI gains an **ERD tab**; PK/FK toggles and type overrides PUT back into the IR before migration.

### 4.3 Done when
- A 3-table fixture (customer / order / order_item with VLOOKUPs) yields the correct ERD without manual hints; user edits round-trip; migration respects edited IR.

## 5. Phase 3 — Complex Formula Analysis

### 5.1 Analyzer additions
- **External references**: parse `xl/externalLinks` XML; cross-file references become FK evidence at confidence 0.99. Cells with a formula but an empty value cache → Issue "missing referenced workbook: <name>" prompting re-upload.
- **Aggregates**: `SUMIF(S)`, `COUNTIF(S)`, `AVERAGEIF(S)`, pivot references.
- **Dynamic references**: `INDIRECT`, `OFFSET` cannot be statically resolved → emit explicit `dynamic_reference` (untracked) nodes, never guess.
- Charts are ignored (presentation objects, not data).

### 5.2 Lineage IR & data-flow view
- Tokenize formulas (`openpyxl.formula.tokenizer`); collect cell-level references; aggregate cell → column → sheet → file.
- Edge semantics: VLOOKUP/XLOOKUP/INDEX+MATCH = *join*; SUMIF*/COUNTIF*/AVERAGEIF* = *aggregate*; arithmetic = *calculate*; bare reference = *copy*.
- Lineage IR: `{nodes: [{id, kind: file|sheet|column|dynamic_reference, label}], edges: [{from, to, op, formula_count}]}`.
- UI gains a **Data flow tab** (ERwin-style) with file / sheet / column zoom levels; rendered from Lineage IR via Mermaid flowchart or vis-network (CDN).
- Derived columns become actionable: include/exclude per column before migration (excluded ones suggested as views/computed columns on SQL targets).

### 5.3 SharePointAdapter (Microsoft Graph, MSAL client credentials)
- `validate()`: unsupported types, text >255 chars (suggest multiline), FK cycles, reserved column names, 5,000-item list view threshold warnings.
- `plan()`: dry-run JSON (lists + columns + Lookup mappings) that works **without** credentials.
- `migrate()`: create lists, FK → indexed Lookup columns, `$batch` inserts (20 ops per batch), partial-success reporting, cleanup option (delete created lists).
- Migration ordering across all adapters: topological sort by FK dependency; cycle fallback = drop constraint → load → re-add (SQL) / plain column (SharePoint).

### 5.4 Done when
- The cross-file fixture set (고객.xlsx + 단가표.xlsx + 주문.xlsx + 월별집계.xlsx) produces correct cross-file FKs and a correct 4-node file-level flow diagram; hostile fixtures (merged cells, blank headers, INDIRECT, empty caches) degrade gracefully; SharePoint dry-run snapshot tests pass with mocked Graph.

## 6. REST API (introduced in Phase 1, extended later)

```
POST /api/upload                      → {job_id}                     [P1]
GET  /api/jobs/{id}/schema            → Schema IR                    [P1]
PUT  /api/jobs/{id}/schema            → save user edits              [P1]
POST /api/jobs/{id}/migrate           → {targets, configs}           [P1]
GET  /api/jobs/{id}/status            → progress + report            [P1]
GET  /api/jobs/{id}/artifacts/{name}  → file download                [P1]
GET  /api/jobs/{id}/erd               → Mermaid text                 [P2]
GET  /api/jobs/{id}/lineage?level=    → Lineage IR (file|sheet|col)  [P3]
```

Job state: filesystem-backed per `job_id` (IR JSON + parquet cache of sheet data). The app itself needs no database.

## 7. Tech Stack

Python 3.11+, Flask 3.x, pandas, openpyxl, SQLAlchemy 2.x, psycopg[binary], MSAL + requests (Phase 3), Mermaid.js via CDN, Bootstrap 5. Credentials via env vars only: `PG_DSN_DEFAULT`, `SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET`.

## 8. Testing

- Fixtures are **generator scripts** (openpyxl), never committed binaries: clean 3-table set (P1–2), cross-file VLOOKUP/SUMIFS set (P3), hostile set (mixed types, blank headers, merged cells, empty-cache external refs, INDIRECT, >255-char text) (P3).
- Snapshot tests on Schema IR / Lineage IR per fixture; E2E via Flask test client; Dockerized Postgres round-trip; mocked-Graph SharePoint dry-run.
- CI: ruff + mypy + pytest on every PR.
