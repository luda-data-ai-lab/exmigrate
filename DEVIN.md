# ExMigrate — Devin Setup (v2, Phased)

Copy-paste source for building ExMigrate on Devin. Companion to `SPEC.md` (v2, phased). Repo: **new, standalone `exmigrate`**, built entirely from scratch.

**Execution model:**
- **Phase 1 & 2 — single Devin session each.** The feature surface is small; one well-scoped session per playbook is faster and cheaper than orchestration.
- **Phase 3 — parallel Managed Devins.** By then the IR contract is stable and the work splits cleanly (analyzer / SharePoint adapter / UI / QA).

- **Part A — Knowledge**: paste into Settings → Knowledge, scoped to the `exmigrate` repo.
- **Part B — Playbooks**: attach when launching each session.
- **Part C — Orchestration** for Phase 3 + PR review checklist.

---

## Part A — Knowledge Entries (repo-scoped)

### A1. Project context (pin)

> **Trigger:** always, when working in the exmigrate repo.
>
> ExMigrate is a standalone Flask web app that migrates Excel workbooks into databases. It ships in three phases: (1) values-only migration to SQLite/PostgreSQL, (2) same-file formula analysis → PK/FK inference → Mermaid ERD with a review/edit UI, (3) cross-file references, data-lineage flow diagram, and a SharePoint List adapter.
> Stack: Python 3.11, Flask 3.x, pandas, openpyxl, SQLAlchemy 2.x, psycopg[binary], MSAL + Graph (Phase 3 only), Bootstrap 5 + vanilla JS + Mermaid.js via CDN (no build step).
> `SPEC.md` at the repo root is authoritative. Read the section for the current phase before starting. Do not build ahead of the current phase.

### A2. Architecture rules

> **Trigger:** when writing or modifying any code.
>
> 1. Everything derives from the Schema IR (and, in Phase 3, the Lineage IR) in `exmigrate/contracts/`. Adapters and the web UI never read Excel directly.
> 2. The IR models carry all fields (`pk`, `fk`, `derived`) from Phase 1, even while unpopulated — later phases fill them in; they never restructure them. Contract changes require explicit approval from the human lead in chat before editing.
> 3. Type hints everywhere; pydantic for IR models; no global mutable state; docstrings on public functions; pytest tests accompany every module.
> 4. Credentials via env vars only (`PG_DSN_DEFAULT`, `SP_TENANT_ID`, `SP_CLIENT_ID`, `SP_CLIENT_SECRET`). Never hardcode or commit secrets.
> 5. Frontend is Bootstrap 5 + vanilla JS + CDN libraries only. No npm, no bundler.

### A3. Repo layout & branch strategy

> **Trigger:** when creating files, branches, or PRs.
>
> ```
> exmigrate/
>   contracts/    # IR models, adapter Protocol, openapi.yaml
>   analyzer/     # Excel reading + inference
>   lineage/      # Phase 3 formula lineage
>   adapters/     # sqlite / postgres / sharepoint
>   web/          # Flask app, templates, static
> tests/
> fixtures/       # generator scripts only, no committed .xlsx binaries
> ```
> Phases 1–2: one branch per playbook (`feat/phase1-core`, `feat/phase2-erd`), whole-repo write access, one PR per playbook.
> Phase 3: one branch per workstream with directory ownership — `feat/p3-analyzer` (analyzer/, lineage/), `feat/p3-sharepoint` (adapters/), `feat/p3-webui` (web/), `feat/p3-qa` (tests/, fixtures/). In Phase 3, never modify files outside your owned directories; PRs touching foreign directories are rejected. Rebase on `main` before opening a PR.

### A4. Verification standard

> **Trigger:** before declaring any task complete or opening a PR.
>
> "Done" means: `ruff check`, `mypy`, and `pytest` pass locally; new behavior has tests; the playbook's Specifications are satisfied point by point. Include command outputs in the PR description. If something couldn't be verified (e.g. no SharePoint credentials), say so explicitly and ship the mocked/dry-run verification instead.

---

## Part B — Playbooks

### B1. Playbook `!phase1-core` — Values-Only Migration (single session)

**Overview**
Bootstrap the repo and ship Phase 1: upload `.xlsx` → sheet=table, values only → migrate to SQLite and PostgreSQL, via both CLI and a minimal web flow. SPEC.md §3.

**Procedure**
1. Read SPEC.md §1–3 and §6–7. Scaffold the repo layout from Knowledge A3, plus ruff/mypy/pytest config and CI workflow.
2. `contracts/`: pydantic Schema IR with the **full** field shape (type enum, nullable, pk, fk, derived, source_sheet, row_count) even though Phase 1 leaves pk/fk/derived unpopulated. Adapter Protocol: `validate(ir)`, `plan(ir)`, `migrate(ir, data)` + `Issue`, `MigrationPlan`, `MigrationReport`. `openapi.yaml` for the P1 endpoints in SPEC §6.
3. `analyzer/`: `data_only=True` read; header detection (first row ≥80% non-null strings); one sheet → one table; type inference (integer/float/boolean/date/datetime/text, sample ≤10k rows, null ratio, max length). Public API: `analyze(paths) -> tuple[SchemaIR, list[Issue]]`.
4. `adapters/`: SQLiteAdapter (SQLAlchemy DDL, one transaction per table, `.db` artifact) and PostgresAdapter (live DSN mode + `.sql` dump mode, COPY for >10k rows, identifier-length/reserved-word validation).
5. `web/`: Flask app factory + filesystem job store (IR JSON + parquet cache per job_id). Pages: upload (drag-and-drop) → review (table list, editable names/types, manual PK checkbox) → target selection (SQLite / Postgres DSN) → migrate → report (row counts, artifact downloads). Implement openapi.yaml exactly.
6. CLI entry point: `exmigrate migrate <files...> --target sqlite|postgres` covering the same path headlessly.
7. Fixture generator: clean 3-table workbook (customer/order/order_item). E2E tests for CLI and web (Flask test client); Postgres against Docker.

**Specifications**
- A multi-sheet workbook migrates to both targets with correct types and row counts, via CLI and via the web flow.
- ruff/mypy/pytest green; fixture is a generator script, not a binary.

**Forbidden actions**
- No formula parsing, no FK inference, no ERD, no SharePoint code, no auth.

### B2. Playbook `!phase2-erd` — Simple Formula Analysis & ERD (single session)

**Overview**
On `feat/phase2-erd`, extend the analyzer with same-file formula parsing, PK/FK inference, and Mermaid ERD; extend the UI with an ERD tab and PK/FK editing. SPEC.md §4.

**Procedure**
1. Read SPEC.md §4. Add a second read pass (`data_only=False`) collecting formula strings, same-file scope only.
2. Handle plain references, arithmetic, and same-workbook VLOOKUP/XLOOKUP. Formula-populated columns → `derived=true` (informational).
3. PK inference: unique + not-null; prefer `id`, `*_id`, `code`, `no`. FK inference with confidence: name match, ≥95% value containment, same-file VLOOKUP target = 0.95.
4. ERD generator: Schema IR → Mermaid `erDiagram`; FK confidence <0.9 annotated tentative. New endpoint `GET /api/jobs/{id}/erd`.
5. UI: ERD tab (Mermaid.js CDN) on the review page; PK/FK toggles + type overrides PUT back to the IR; migration consumes the edited IR (FKs now emitted in DDL, topological load order, cycle fallback drop→load→re-add).
6. Extend fixtures: add VLOOKUP columns to the 3-table set; snapshot tests on inferred IR; E2E: infer → edit → migrate.

**Specifications**
- The 3-table VLOOKUP fixture yields the correct ERD with zero manual hints; user edits round-trip; SQL targets create real FK constraints in dependency order.

**Forbidden actions**
- No cross-file/externalLinks handling, no lineage graph, no INDIRECT/OFFSET logic, no SharePoint.

### B3. Phase 3 playbooks (parallel Managed Devins)

#### B3a. `!p3-analyzer` — Complex Formulas & Lineage (owns `analyzer/`, `lineage/`)

**Procedure**
1. Read SPEC.md §5.1–5.2. External references: parse `xl/externalLinks`; cross-file refs = FK evidence 0.99; formula cells with empty cache → Issue "missing referenced workbook: <name>".
2. Aggregates (SUMIF*/COUNTIF*/AVERAGEIF*, pivot refs). INDIRECT/OFFSET → `dynamic_reference` untracked nodes, never guessed. Charts ignored.
3. Lineage: tokenize (`openpyxl.formula.tokenizer`), collect cell-level refs, aggregate cell→column→sheet→file, label edges join/aggregate/calculate/copy. Emit Lineage IR per contracts. Public API becomes `analyze(paths) -> tuple[SchemaIR, LineageIR, list[Issue]]`.

**Specifications** — cross-file fixture set produces correct cross-file FKs and a correct file-level flow graph; hostile fixtures degrade gracefully (issues, not crashes); 100k-row sheet under 30s.

**Forbidden** — no writes outside `analyzer/`, `lineage/`; contract changes go through the coordinator.

#### B3b. `!p3-sharepoint` — SharePoint Adapter (owns `adapters/`)

**Procedure**
1. Read SPEC.md §5.3. MSAL client-credentials auth; all Graph calls behind an injectable client for mocking.
2. `validate()`: unsupported types, >255-char text (suggest multiline), FK cycles, reserved names, 5,000-item view threshold warnings. `plan()`: credential-free dry-run JSON (lists, columns, Lookup mappings). `migrate()`: create lists, FK → indexed Lookup, `$batch` inserts (20 ops/batch), partial-success reporting, cleanup option.
3. Respect derived include/exclude flags across **all three** adapters.

**Specifications** — dry-run snapshot tests green with mocked Graph; no credential literals anywhere; partial failures reported per table, never fatal.

**Forbidden** — no writes outside `adapters/`.

#### B3c. `!p3-webui` — Data Flow Tab & Derived Controls (owns `web/`)

**Procedure**
1. Read SPEC.md §5.2, §6. `GET /api/jobs/{id}/lineage?level=file|sheet|column`. Data flow tab next to ERD: render Lineage IR (Mermaid flowchart or vis-network via CDN) with the three zoom levels; `dynamic_reference` nodes visually distinct.
2. "Missing referenced workbook" issues → re-upload affordance on the upload page. Derived-column include/exclude controls on review; SharePoint config form (site URL + credentials or dry-run toggle); surface every adapter's `validate()` issues before enabling migrate; report page shows SharePoint list URLs / dry-run JSON artifact.

**Specifications** — full E2E clickthrough incl. SharePoint dry-run; vanilla JS + CDN only; endpoints match openapi.yaml exactly.

**Forbidden** — no writes outside `web/`; no re-implementation of analysis/migration logic in the web layer.

#### B3d. `!p3-qa` — Fixtures & Integration (owns `tests/`, `fixtures/`)

**Procedure**
1. Generator scripts: cross-file set — `고객.xlsx` + `단가표.xlsx` + `주문.xlsx` (VLOOKUP joins, amount calc) + `월별집계.xlsx` (SUMIFS); hostile set — mixed types, blank headers, merged cells, empty-cache external refs, INDIRECT, >255-char text.
2. Snapshot tests for SchemaIR & LineageIR per fixture; E2E upload→review→migrate; Dockerized Postgres round-trip; mocked-Graph SharePoint dry-run. CI reports failures **per module** for routing.

**Specifications** — ≥85% coverage on `analyzer/` and `adapters/`; all E2E green on `main`.

**Forbidden** — never "fix" production code to make tests pass; report the failure to the coordinator instead.

---

## Part C — Orchestration & Review

### C1. Phase sequencing

| Step | Mode | Gate to next |
|---|---|---|
| `!phase1-core` | 1 Devin session | Human lead merges PR; CLI + web E2E green |
| `!phase2-erd` | 1 Devin session | ERD fixture test green; edited-IR migration verified |
| Phase 3 kickoff | Coordinator session | Confirm contracts (incl. Lineage IR models added by coordinator or `!p3-analyzer` first commit, lead-approved) |
| `!p3-analyzer` + `!p3-qa` | parallel Managed Devins | analyzer merged |
| `!p3-sharepoint` + `!p3-webui` | parallel Managed Devins | all E2E green |

### C2. Coordinator prompt (Phase 3 only)

```
You are the coordinator for ExMigrate Phase 3. SPEC.md §5 is authoritative;
DEVIN.md Part B3 holds the playbooks. Launch !p3-analyzer and !p3-qa first
as managed Devins; launch !p3-sharepoint and !p3-webui after the analyzer
PR merges. Scope each delegation to one playbook. Enforce directory
ownership (Knowledge A3). Relay contract-change requests to the human lead
— never edit contracts/ unilaterally. Rebase feature branches on main to
resolve conflicts. Maintain STATUS.md after each merge. Every PR requires
the human lead's review.
```

### C3. Human lead PR review checklist

- [ ] Diff stays inside the branch's allowed directories (A3)
- [ ] No `contracts/` changes without prior approval
- [ ] ruff / mypy / pytest outputs included and green
- [ ] Playbook Specifications satisfied point by point
- [ ] No hardcoded credentials; no committed `.xlsx` binaries
- [ ] Session lessons folded back into the playbook or Knowledge

### C4. Usage notes (Devin Max)

Single-seat Max quota is weekly with no daily cap; parallel Managed Devins consume it faster in Phase 3, so keep sessions scoped to one playbook, let idle sessions sleep, and check Session Insights weekly before raising parallelism.
