# ExMigrate 사용자 매뉴얼

ExMigrate는 Excel 워크북을 분석해 관계형 데이터베이스(SQLite / PostgreSQL)로 옮기는 도구입니다.
시트 하나가 테이블 하나가 되고, 수식·PK/FK·파일 간 참조를 자동으로 분석해 검토 화면에서 확인·수정한 뒤 마이그레이션합니다.

| 단계 | 기능 |
| --- | --- |
| Phase 1 | 값 기준 마이그레이션 (시트 → 테이블), SQLite / PostgreSQL(live·dump), CLI + 웹 UI |
| Phase 2 | 수식 분석(derived 열), PK/FK 추론, Mermaid ERD, FK 제약 생성 |
| Phase 3 | 외부 워크북 참조(`[Other.xlsx]Sheet!A1`), Data flow(Lineage) 그래프, derived 열 Include/Exclude |
| Phase 4 | 수식 열을 SQL 뷰(SQLite/PostgreSQL) + pandas 스크립트로 자동 변환 (Recompute) |

---

## 1. 설치와 실행

Python 3.10 이상이 필요합니다.

```bash
git clone https://github.com/luda-data-ai-lab/exmigrate.git
cd exmigrate
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

python -m exmigrate.web.app          # 웹 UI → http://127.0.0.1:5000  (PORT 환경변수로 변경)
```

샘플 워크북 생성:

```bash
python -m fixtures.clean_three_table sample.xlsx   # 단일 파일: Customers / Orders / Order Items (VLOOKUP, XLOOKUP)
python -m fixtures.cross_file out/                 # 4개 파일: customers, products, orders, summary (파일 간 참조, SUMIF, INDIRECT…)
```

> 코드로 생성한 샘플은 수식의 **계산값(캐시)이 없습니다.** Excel에서 한 번 열어 저장하면 derived 열 값도 함께 마이그레이션됩니다.

환경변수

| 변수 | 의미 |
| --- | --- |
| `PORT` | 웹 서버 포트 (기본 5000) |
| `EXMIGRATE_JOBS_DIR` | 작업(job) 저장 폴더 (기본 `./jobs`) |
| `PG_DSN_DEFAULT` | PostgreSQL 기본 접속 문자열 (DSN을 비워두면 사용) |

---

## 2. 웹 UI 흐름

상단 메뉴: **Upload** · **History** · **Manual**

### 2.1 Upload
`.xlsx` 파일을 드래그하거나 **Choose files…** 버튼으로 선택합니다(여러 번 추가 가능, 선택 개수는 옆에 "N files selected"로 표시). 서로 참조하는 워크북은 **한 번에 함께** 올려야 파일 간 FK와 데이터 흐름이 연결됩니다. 한글 파일명도 그대로 유지됩니다.

### 2.2 Review (검토)
자동 분석 결과를 확인·수정하는 화면입니다. 수정 후 **Save**를 눌러야 이후 단계(ERD, Recompute, 마이그레이션)에 반영됩니다.

**Schema 탭**

| 항목 | 설명 |
| --- | --- |
| 테이블/열 이름 | DB에 만들 이름. 시트/헤더 이름을 snake_case로 정규화한 값이 기본 |
| Type | `integer` / `float` / `text` / `date` / `datetime` / `boolean` — 자동 추론, 변경 가능 |
| PK | 값이 모두 고유하고 빈 칸이 없는 열을 추천 (`id`, `*_id`, `code`, `no` 우선) |
| FK | 참조 대상 `table.column` 선택. 신뢰도(0.7~0.99)가 함께 표시 |
| derived 배지 | 수식으로 채워진 열 |
| **Include** | 체크를 끄면 해당 열은 DDL/데이터에서 제외됨 (derived 열을 DB에서 다시 계산하려는 경우 사용) |

FK 신뢰도 기준: 파일 간 lookup 0.99 · lookup+이름+값 0.98 · lookup 0.95 · 이름+값 포함률 ≥95% 0.9 · 값 포함률만 0.7. 0.9 미만은 ERD에 점선(tentative)으로 표시됩니다.

**ERD 탭** — Mermaid `erDiagram`. PK/FK 편집 후 Save하면 갱신됩니다.

**Formulas 탭** — 파일별 사용 함수 목록(이름·횟수), 시트/열별 함수·수식 셀 수·참조 시트·예시 수식.

**Data flow 탭** — 수식 기반 데이터 흐름 그래프. 파일 / 시트 / 열 단위로 줌. 엣지 종류:
`join`(VLOOKUP·XLOOKUP·INDEX/MATCH), `aggregate`(SUMIF·COUNTIF 계열), `calculate`(사칙연산 등), `copy`(`=C2`), `dynamic`(INDIRECT·OFFSET — 출처 추적 불가, 별도 노드로 표시).
함께 올리지 않은 워크북을 참조하면 `missing referenced workbook` 경고가 표시됩니다.

**Recompute 탭 (Phase 4)** — 수식 열마다 SQL/pandas 변환 결과를 보여줍니다.

| 열 | 설명 |
| --- | --- |
| Status | `ok` 변환 완료 · `partial` 일부만 변환(notes 참고) · `unsupported` 변환 불가 |
| Formula | 원본 Excel 수식 |
| SQLite SQL / pandas | 생성된 표현식 |
| Notes | TODO·주의 사항 (예: `TODO INDIRECT() resolves its target at runtime; rewrite by hand`) |

상단 링크로 SQLite 뷰 SQL / PostgreSQL 뷰 SQL / pandas 스크립트를 바로 내려받을 수 있습니다.
**Include를 끄고 Save한 열**은 "excluded → recomputed"로 표시되며 뷰에서 원래 이름으로 재계산됩니다.

### 2.3 Choose targets
- **SQLite** — 파일(`migration.db`)로 생성. 접속 정보 불필요.
- **PostgreSQL**
  - **Live**: DSN 입력 (`postgresql://user:pw@host:5432/db`). 비워두면 `PG_DSN_DEFAULT`.
  - **Dump**: 접속 없이 `migration.sql` 파일만 생성.

### 2.4 Report
테이블별 적재 행 수, 경고/오류(Issue), 다운로드 가능한 산출물(artifact) 목록.

| 산출물 | 내용 |
| --- | --- |
| `migration.db` / `migration.sql` | 마이그레이션 결과 (뷰 포함) |
| `recompute_sqlite.sql` / `recompute_postgres.sql` | 수식 열 재계산 뷰 SQL |
| `recompute.py` | 동일 로직의 pandas 스크립트 |

### 2.5 History
지난 작업 목록(시간·파일·테이블·상태·타깃). Review/Report 다시 열기, 삭제.

---

## 3. 마이그레이션 결과의 의미

- **테이블**: 시트 = 테이블. 여러 파일을 올려도 하나의 DB/스키마에 들어갑니다.
- **PK/FK**: 실제 제약으로 생성됩니다. 부모 → 자식 순서로 적재하고, 순환 참조는 PostgreSQL에서는 적재 후 `ALTER TABLE … ADD CONSTRAINT`, SQLite에서는 경고(`fk_not_created`)로 남깁니다.
  - SQLite에서 FK 검사는 `PRAGMA foreign_keys=ON` 후 동작합니다.
- **수식 열(derived)**: 수식이 아니라 **Excel이 계산해 둔 값**이 저장됩니다. 캐시가 없으면 NULL이 들어가며 `formula_cache_empty` 경고가 표시됩니다.
- **Include를 끈 열**: DDL/데이터에서 빠지고 `column_excluded`로 리포트에 남습니다. 대신 Recompute 뷰에서 다시 계산됩니다.

### 3.1 Recompute 뷰 (Phase 4)

수식 열이 있는 테이블마다 `<테이블>_v` 뷰가 만들어집니다 (`--no-views`로 끌 수 있음).

| 열 상태 | 뷰에서의 모습 |
| --- | --- |
| Include **해제**(excluded) | **원래 이름**으로 재계산된 열 (`orders_v.customer_name`) |
| Include **유지**(included) | Excel 캐시값은 그대로, 옆에 `<열>_calc` 열이 추가되어 비교 가능 (`orders_v.order_total`, `orders_v.order_total_calc`) |
| unsupported | `NULL` + SQL 주석 `-- TODO … 원본 수식` |

변환 규칙

| Excel | SQL | pandas |
| --- | --- | --- |
| `=C2`, `=D2*E2`, `IF`, `ROUND`, `LEFT`, `TEXT`, `IFERROR` … | 행 단위 표현식 | 벡터 연산 |
| `VLOOKUP` / `XLOOKUP` / `INDEX(MATCH)` | 대상 테이블에 대한 상관 서브쿼리 (`SELECT … LIMIT 1`) | `_lookup()` (merge) |
| `SUMIF(S)` / `COUNTIF(S)` / `AVERAGEIF(S)` / `MINIFS` / `MAXIFS` | 조건을 `WHERE`로 옮긴 상관 집계 | `_cond_agg()` |
| `INDIRECT` / `OFFSET`, 배열 수식, 미지원 함수 | `NULL` + TODO | `np.nan` + TODO |

의존 관계: 뷰는 의존 순서대로 생성되며, 다른 테이블의 **제외된** 열을 참조하는 수식은 그 테이블의 `_v` 뷰를 읽습니다.
**포함된**(included) derived 열은 신뢰할 수 있는 입력값(캐시)으로 취급되므로, 연쇄적으로 전부 다시 계산하려면 중간 열도 Include를 해제하세요.
예: `summary.revenue`(SUMIF of `orders.order_total`)를 재계산하려면 `orders.order_total`도 제외.

pandas 스크립트 실행:

```bash
python recompute.py migration.db          # 테이블을 읽어 재계산 결과 출력
# 또는
from recompute import recompute, load
tables = recompute(load(sqlite3.connect("migration.db")))
```

PostgreSQL dump 모드에서는 뷰 SQL이 `migration.sql`의 `COMMIT` 뒤에 추가됩니다.

---

## 4. CLI

```bash
exmigrate analyze  book.xlsx                       # Schema IR(JSON) 출력
exmigrate erd      book.xlsx > erd.mmd             # Mermaid ERD
exmigrate formulas book.xlsx [--json]              # 시트/열별 사용 함수
exmigrate lineage  a.xlsx b.xlsx [--level file|sheet|column] [--json]
exmigrate translate a.xlsx b.xlsx [--exclude T.C]... [--format summary|sqlite|postgres|pandas|json]

exmigrate migrate book.xlsx --target sqlite   --out out/                 # out/migration.db
exmigrate migrate book.xlsx --target postgres --dump --out out/          # out/migration.sql
exmigrate migrate book.xlsx --target postgres --dsn postgresql://u:p@h:5432/db
exmigrate migrate a.xlsx b.xlsx --target sqlite --exclude orders.customer_name --exclude order_items.line_total
exmigrate migrate book.xlsx --target sqlite --no-views                   # 뷰/recompute.py 생성 안 함
exmigrate migrate book.xlsx --target sqlite --json                       # 리포트 JSON 함께 출력
```

`--exclude`는 `테이블.열` 형식이며 반복 가능, 존재하지 않는 열이면 종료 코드 2.

---

## 5. REST API

기본 경로 `/api`. 상세 스키마는 `exmigrate/contracts/openapi.yaml`.

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/upload` | multipart `files` 업로드 → `201 {job_id}` |
| GET | `/jobs` | 작업 목록 |
| DELETE | `/jobs/{id}` | 작업 삭제 |
| GET / PUT | `/jobs/{id}/schema` | Schema IR 조회 / 저장 |
| GET | `/jobs/{id}/erd` | Mermaid ERD 텍스트 |
| GET | `/jobs/{id}/formulas` | 함수 인벤토리 |
| GET | `/jobs/{id}/lineage?level=file\|sheet\|column` | Lineage IR / Mermaid |
| GET | `/jobs/{id}/translation?format=json\|sqlite\|postgres\|pandas` | 수식 변환 결과 (저장된 IR 기준) |
| POST | `/jobs/{id}/migrate` | `{"targets":["sqlite","postgres"],"configs":{"postgres":{"dsn":"…","mode":"live\|dump"}}}` → `202` |
| GET | `/jobs/{id}/status` | 진행 상태와 리포트 |
| GET | `/jobs/{id}/artifacts/{name}` | 산출물 다운로드 |

---

## 6. 경고·오류 코드

| 코드 | 의미 |
| --- | --- |
| `no_header` / `blank_header` / `no_columns` | 헤더를 찾지 못함 / 빈 헤더 / 열 없음 |
| `duplicate_table` / `reserved_word` / `identifier_too_long` | 이름 충돌·예약어·길이 제한 (자동으로 이름 조정) |
| `unsupported_file` | `.xlsx`가 아닌 파일 |
| `formula_cache_empty` | 수식 열에 계산값(캐시)이 없어 NULL로 적재됨 |
| `missing_referenced_workbook` | 참조한 워크북이 함께 업로드되지 않음 |
| `dynamic_reference` | INDIRECT/OFFSET — 출처 추적 불가 |
| `fk_skipped` / `fk_not_created` / `fk_cycle` | FK 대상이 PK가 아님·타입 불일치 / SQLite에서 추가 불가 / 순환 참조 처리 |
| `column_excluded` | Include 해제로 제외된 열 |
| `view_not_created` / `views_skipped` | 재계산 뷰 생성 실패 / 테이블 적재 실패로 뷰 생성 건너뜀 |

---

## 7. DB에서 확인하기

```bash
# SQLite
sqlite3 migration.db
.tables                                 # 테이블과 *_v 뷰
.schema orders                          # FOREIGN KEY 확인
PRAGMA foreign_keys=ON; PRAGMA foreign_key_check;
SELECT order_id, customer_name, order_total, order_total_calc FROM orders_v LIMIT 5;
```

```sql
-- PostgreSQL
\d orders
\dv                                     -- 뷰 목록
SELECT conname, conrelid::regclass, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype='f';
```

---

## 8. 자주 묻는 질문

- **derived 열이 전부 NULL입니다.** 코드로 만든 파일이거나 Excel이 계산값을 저장하지 않은 경우입니다. Excel에서 열어 저장 후 다시 올리거나, Include를 해제해 뷰에서 재계산하세요.
- **XLOOKUP이 `#NAME?`로 나옵니다.** Excel 2019 이하에는 XLOOKUP이 없습니다. INDEX/MATCH로 바꾸면 동일하게 변환됩니다.
- **FK가 만들어지지 않았습니다.** 대상 열이 PK가 아니거나 타입이 다르면 `fk_skipped`로 건너뜁니다. Review에서 PK/타입을 수정 후 다시 마이그레이션하세요.
- **뷰 값이 0 또는 NULL입니다.** 수식이 참조하는 중간 derived 열이 Include 상태(캐시값 NULL)일 수 있습니다. 그 열도 Include를 해제하면 연쇄 재계산됩니다.
