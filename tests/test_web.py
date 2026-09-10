from __future__ import annotations

import sqlite3
from pathlib import Path

from flask.testing import FlaskClient


def _upload(client: FlaskClient, workbook: Path) -> str:
    with workbook.open("rb") as fh:
        res = client.post(
            "/api/upload",
            data={"files": (fh, workbook.name)},
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


def test_unknown_job(client: FlaskClient) -> None:
    assert client.get("/api/jobs/nope/schema").status_code == 404
    assert client.get("/api/jobs/nope/status").status_code == 404
    assert client.get("/api/jobs/../etc/status").status_code == 404


def test_full_web_flow(client: FlaskClient, clean_workbook: Path, tmp_path: Path) -> None:
    job_id = _upload(client, clean_workbook)

    schema = client.get(f"/api/jobs/{job_id}/schema").get_json()
    assert [t["name"] for t in schema["tables"]] == ["customers", "orders", "order_items"]
    status = client.get(f"/api/jobs/{job_id}/status").get_json()
    assert status["state"] == "analyzed" and status["files"] == ["clean.xlsx"]

    schema["tables"][0]["name"] = "customer"
    schema["tables"][0]["columns"][0]["name"] = "id"
    schema["tables"][0]["columns"][0]["pk"] = True
    schema["tables"][1]["columns"][3]["type"] = "text"
    res = client.put(f"/api/jobs/{job_id}/schema", json=schema)
    assert res.status_code == 200
    saved = client.get(f"/api/jobs/{job_id}/schema").get_json()
    assert saved["tables"][0]["name"] == "customer"
    assert saved["tables"][0]["columns"][0]["pk"] is True

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
    assert sqlite_report["artifacts"] == ["migration.db"]
    assert pg_report["artifacts"] == ["migration.sql"]

    res = client.get(f"/api/jobs/{job_id}/artifacts/migration.db")
    assert res.status_code == 200
    db_path = tmp_path / "dl.db"
    db_path.write_bytes(res.data)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 25
    assert conn.execute("PRAGMA table_info(customer)").fetchone()[1] == "id"
    conn.close()

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
