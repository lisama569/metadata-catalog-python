"""HTTP 层端到端测试: 状态码与错误码必须可被调用方区分。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from metadata_catalog.api import create_app
from metadata_catalog.service import MetadataCatalogService


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(create_app(service=MetadataCatalogService(store)))


def _register(client, asset_id, fields=None):
    return client.post(
        "/assets", json={"asset_id": asset_id, "fields": fields or []}
    )


def test_health(client):
    assert client.get("/health").status_code == 200


def test_register_and_get(client):
    r = _register(client, "a", [{"name": "id", "data_type": "int"}])
    assert r.status_code == 200
    assert r.json()["outcome"] == "CREATED"
    got = client.get("/assets/a")
    assert got.status_code == 200
    assert got.json()["asset_id"] == "a"
    assert client.get("/assets").json()["assets"][0]["asset_id"] == "a"


def test_idempotent_report_http(client):
    payload = {"asset_id": "a", "fields": [{"name": "id", "data_type": "int"}]}
    assert client.post("/assets", json=payload).json()["outcome"] == "CREATED"
    second = client.post("/assets", json=payload)
    assert second.json()["outcome"] == "IDEMPOTENT"
    assert second.json()["schema_version"] == 1


def test_type_change_422_with_reason(client):
    _register(client, "a", [{"name": "id", "data_type": "int"}])
    r = client.post("/assets", json={"asset_id": "a", "fields": [{"name": "id", "data_type": "bigint"}]})
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "SCHEMA_INCOMPATIBLE"
    assert body["details"]["reason"] == "FIELD_TYPE_CHANGED"


def test_schema_version_conflict_409(client):
    _register(client, "a", [{"name": "id", "data_type": "int"}])
    client.post("/assets", json={"asset_id": "a", "fields": [
        {"name": "id", "data_type": "int"}, {"name": "b", "data_type": "int"}
    ]})  # v2
    r = client.post("/assets", json={
        "asset_id": "a",
        "expected_schema_version": 1,
        "fields": [{"name": "id", "data_type": "int"}, {"name": "b", "data_type": "int"}],
    })
    assert r.status_code == 409
    assert r.json()["error_code"] == "VERSION_CONFLICT"
    assert r.json()["details"]["actual_version"] == 2


def test_lineage_duplicate_and_cycle_http(client):
    _register(client, "a")
    _register(client, "b")
    r1 = client.post("/lineage/edges", json={"upstream": "a", "downstream": "b"})
    assert r1.json()["status"] == "CREATED"
    r2 = client.post("/lineage/edges", json={"upstream": "a", "downstream": "b"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "DUPLICATE_EDGE"

    r3 = client.post("/lineage/edges", json={"upstream": "b", "downstream": "a"})
    assert r3.status_code == 422
    assert r3.json()["error_code"] == "LINEAGE_CYCLE"
    assert r3.json()["details"]["cycle"][0] == "b"


def test_lineage_missing_endpoint_http(client):
    _register(client, "a")
    r = client.post("/lineage/edges", json={"upstream": "a", "downstream": "x"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "LINEAGE_ENDPOINT_MISSING"
    assert r.json()["details"]["missing"] == ["x"]


def test_impact_http(client):
    _register(client, "a")
    _register(client, "b")
    _register(client, "c")
    client.post("/lineage/edges", json={"upstream": "a", "downstream": "b"})
    client.post("/lineage/edges", json={"upstream": "b", "downstream": "c"})
    r = client.get("/assets/a/impact?direction=downstream")
    body = r.json()
    assert body["direct"] == ["b"]
    assert body["transitive"] == ["c"]


def test_ownership_conflict_http(client):
    _register(client, "a")
    ok = client.put("/assets/a/ownership", json={"tags": ["x"], "expected_meta_version": 0})
    assert ok.status_code == 200 and ok.json()["meta_version"] == 1
    stale = client.put("/assets/a/ownership", json={"tags": ["y"], "expected_meta_version": 0})
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "VERSION_CONFLICT"
    assert stale.json()["details"]["scope"] == "meta"


def test_asset_not_found_http(client):
    assert client.get("/assets/nope").status_code == 404
    assert client.get("/assets/nope/impact").status_code == 404
