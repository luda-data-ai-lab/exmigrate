from __future__ import annotations

import sqlite3
from pathlib import Path

from flask.testing import FlaskClient


def _upload(client: FlaskClient, workbook: Path, created_by: str | None = None) -> str:
    with workbook.open("rb") as fh:
        data: dict[str, object] = {"files": (fh, workbook.name)}
        if created_by is not None:
            data["created_by"] = created_by
        res = client.post(
            "/api/upload",
            data=data,
            content_type="multipart/form-data",
        )
    assert res.status_code == 201, res.get_json()
    job_id: str = res.get_json()["job_id"]
    return job_id


def test_pages_render(client: FlaskClient, clean_workbook: Path) -> None:
    assert client.get("/").status_code == 200
    job_id = _upload(client, clean_workbook)
    for page in ("review", "target", "report"):
        assert client.get(f"/jobs/{job_id}/{page}").status_code == 200
        assert client.get(f"/jobs/nope/{page}").status_code == 404


def test_upload_rejects_empty(client: FlaskClient) -> None:
    res = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert res.status_code == 400


def test_erd_unknown_job(client: FlaskClient) -> None:
    assert client.get("/api/jobs/nope/erd").status_code == 404


def test_unknown_job(client: FlaskClient) -> None:
    assert client.get("/api/jobs/nope/schema").status_code == 404
    assert client.get("/api/jobs/nope/status").status_code == 404
    assert client.get("/api/jobs/../etc/status").status_code == 404
    assert client.get("/api/jobs/nope/formulas").status_code == 404


def test_formulas_endpoint(client: FlaskClient, clean_workbook: Path) -> None:
    job_id = _upload(client, clean_workbook)
    assert b"Formulas" in client.get(f"/jobs/{job_id}/review").data
    body = client.get(f"/api/jobs/{job_id}/formulas").get_json()
    assert body["functions"] == {"XLOOKUP": 150, "VLOOKUP": 60}
    assert len(body["files"]) == 1
    file = body["files"][0]
    assert file["file"] == clean_workbook.name and file["functions"] == body["functions"]
    cols = {(c["sheet"], c["column"]): c for c in file["columns"]}
    name = cols[("Orders", "customer_name")]
    assert name["functions"] == {"VLOOKUP": 60} and name["references"] == ["Customers"]
    assert name["derived"] is True and name["formula_cells"] == 60
    assert cols[("Order Items", "line_total")]["sample"] == "=D2*E2"

    # jobs created before the inventory existed report an empty list
    (client.application.extensions["job_store"].path(job_id) / "formulas.json").unlink()
    assert client.get(f"/api/jobs/{job_id}/formulas").get_json() == {"functions": {}, "files": []}


def test_manual_page(client: FlaskClient) -> None:
    page = client.get("/manual")
    assert page.status_code == 200 and b"manual.ko.md" in page.data
    en = client.get("/manual?lang=en")
    assert en.status_code == 200 and b"manual.en.md" in en.data
    assert client.get("/manual?lang=xx").status_code == 404
    for name in ("manual.ko.md", "manual.en.md"):
        md = client.get(f"/static/{name}")
        assert md.status_code == 200 and md.data.startswith(b"# ExMigrate")


def test_footer(client: FlaskClient) -> None:
    page = client.get("/").data
    assert b"Created by <strong>LUDA</strong>" in page
    assert b"Lighting universe through Data and AI" in page
    assert b"mailto:contact@ludaresearch.org" in page
    assert client.get("/static/luda-logo.png").status_code == 200


def test_job_history(client: FlaskClient, clean_workbook: Path) -> None:
    assert client.get("/jobs").status_code == 200
    assert client.get("/api/jobs").get_json() == []

    first = _upload(client, clean_workbook)
    second = _upload(client, clean_workbook, created_by="  James  ")
    jobs = client.get("/api/jobs").get_json()
    assert [j["job_id"] for j in jobs] == [second, first]
    assert jobs[0]["state"] == "analyzed" and jobs[0]["created_at"]
    assert jobs[0]["created_by"] == "James" and jobs[1]["created_by"] is None
    assert jobs[0]["files"] == ["clean.xlsx"]
    assert jobs[0]["tables"] == ["customers", "orders", "order_items"]
    assert jobs[0]["targets"] == []

    schema = client.get(f"/api/jobs/{second}/schema").get_json()
    schema["tables"][0]["name"] = "customer"
    assert client.put(f"/api/jobs/{second}/schema", json=schema).status_code == 200
    res = client.post(f"/api/jobs/{second}/migrate", json={"targets": ["sqlite"]})
    assert res.status_code == 202
    jobs = client.get("/api/jobs").get_json()
    assert jobs[0]["tables"][0] == "customer"
    assert jobs[0]["state"] == "done" and jobs[0]["targets"] == ["sqlite"]

    assert client.delete(f"/api/jobs/{first}").status_code == 204
    assert client.delete(f"/api/jobs/{first}").status_code == 404
    assert client.delete("/api/jobs/../etc").status_code == 404
    for bad in ("%2e", "%2e%2e", ".", "..", "%2e%2e%2fjobs"):
        assert client.delete(f"/api/jobs/{bad}").status_code in (404, 405)
        assert client.get(f"/api/jobs/{bad}/status").status_code == 404
    assert [j["job_id"] for j in client.get("/api/jobs").get_json()] == [second]


def test_full_web_flow(client: FlaskClient, clean_workbook: Path, tmp_path: Path) -> None:
    job_id = _upload(client, clean_workbook)

    schema = client.get(f"/api/jobs/{job_id}/schema").get_json()
    assert [t["name"] for t in schema["tables"]] == ["customers", "orders", "order_items"]
    status = client.get(f"/api/jobs/{job_id}/status").get_json()
    assert status["state"] == "analyzed" and status["files"] == ["clean.xlsx"]

    erd = client.get(f"/api/jobs/{job_id}/erd")
    assert erd.status_code == 200 and erd.mimetype == "text/plain"
    assert 'orders }o--|| customers : "customer_id -> customer_id"' in erd.get_data(as_text=True)

    schema["tables"][0]["name"] = "customer"
    schema["tables"][0]["columns"][0]["name"] = "id"
    schema["tables"][0]["columns"][0]["pk"] = True
    schema["tables"][1]["columns"][1]["fk"] = {"table": "customer", "column": "id", "confidence": 1}
    schema["tables"][1]["columns"][3]["type"] = "text"
    schema["tables"][2]["columns"][1]["fk"] = None
    res = client.put(f"/api/jobs/{job_id}/schema", json=schema)
    assert res.status_code == 200
    saved = client.get(f"/api/jobs/{job_id}/schema").get_json()
    assert saved["tables"][0]["name"] == "customer"
    assert saved["tables"][0]["columns"][0]["pk"] is True
    assert saved["tables"][2]["columns"][1]["fk"] is None
    erd_text = client.get(f"/api/jobs/{job_id}/erd").get_data(as_text=True)
    assert 'orders }o--|| customer : "customer_id -> id"' in erd_text
    assert "order_items }o" not in erd_text

    res = client.post(
        f"/api/jobs/{job_id}/migrate",
        json={"targets": ["sqlite", "postgres"], "configs": {"postgres": {"mode": "dump"}}},
    )
    assert res.status_code == 202, res.get_json()
    status = res.get_json()
    assert status["state"] == "done", status
    sqlite_report, pg_report = status["reports"]
    assert {t["name"]: t["rows_loaded"] for t in sqlite_report["tables"]} == {
        "customer": 25,
        "orders": 60,
        "order_items": 150,
    }
    assert sqlite_report["artifacts"] == ["migration.db", "recompute_sqlite.sql", "recompute.py"]
    assert pg_report["artifacts"] == ["migration.sql", "recompute_postgres.sql", "recompute.py"]

    res = client.get(f"/api/jobs/{job_id}/artifacts/migration.db")
    assert res.status_code == 200
    db_path = tmp_path / "dl.db"
    db_path.write_bytes(res.data)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 25
    assert conn.execute("PRAGMA table_info(customer)").fetchone()[1] == "id"
    fks = conn.execute("PRAGMA foreign_key_list(orders)").fetchall()
    assert [(row[2], row[3], row[4]) for row in fks] == [("customer", "customer_id", "id")]
    assert conn.execute("PRAGMA foreign_key_list(order_items)").fetchall() == []
    conn.close()
    assert sqlite_report["issues"] == []

    assert client.get(f"/api/jobs/{job_id}/artifacts/missing.bin").status_code == 404


def test_put_schema_rejects_bad_shape(client: FlaskClient, clean_workbook: Path) -> None:
    job_id = _upload(client, clean_workbook)
    schema = client.get(f"/api/jobs/{job_id}/schema").get_json()
    schema["tables"][0]["columns"].pop()
    assert client.put(f"/api/jobs/{job_id}/schema", json=schema).status_code == 400
    assert client.put(f"/api/jobs/{job_id}/schema", json={"tables": "x"}).status_code == 400


def test_migrate_validation(client: FlaskClient, clean_workbook: Path) -> None:
    job_id = _upload(client, clean_workbook)
    assert client.post(f"/api/jobs/{job_id}/migrate", json={"targets": []}).status_code == 400
    assert (
        client.post(f"/api/jobs/{job_id}/migrate", json={"targets": ["mysql"]}).status_code == 400
    )


def test_migrate_postgres_live_via_web(
    client: FlaskClient, clean_workbook: Path, pg_dsn: str
) -> None:
    job_id = _upload(client, clean_workbook)
    res = client.post(
        f"/api/jobs/{job_id}/migrate",
        json={"targets": ["postgres"], "configs": {"postgres": {"dsn": pg_dsn}}},
    )
    assert res.status_code == 202
    assert res.get_json()["state"] == "done", res.get_json()
