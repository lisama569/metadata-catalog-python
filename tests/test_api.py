"""HTTP 接口端到端测试（FastAPI TestClient）。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from metadata_catalog.api import create_app
from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.store import LocalStore

logger = logging.getLogger("metadata_catalog.tests")


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(MetadataCatalog(LocalStore())))


@pytest.fixture
def persistent_client(tmp_path):
    """指向临时磁盘仓库的 app；测试可重新 create_app 模拟服务重启。"""
    repo = str(tmp_path / "repo")

    def _make():
        return TestClient(create_app(repo_dir=repo))

    return _make


def _register(client, name, fields=None, **kw):
    return client.post(
        "/assets/register",
        json={"name": name, "asset_type": "table", "fields": fields or [], **kw},
    )


def test_register_idempotent_and_incompatible_http_codes(client):
    logger.info("用例输入: 登记 orders，重复上报，再上报不兼容类型变更")
    fields = [{"name": "id", "field_type": "long", "nullable": False}]
    r1 = _register(client, "orders", fields)
    assert r1.status_code == 200 and r1.json()["action"] == "created"
    r2 = _register(client, "orders", fields)
    assert r2.json()["action"] == "re_registered_idempotent"

    bad = _register(
        client, "orders",
        [{"name": "id", "field_type": "string", "nullable": False}],
        expected_schema_version=1,
    )
    logger.info("判定依据: status=%s body=%s", bad.status_code, bad.json())
    assert bad.status_code == 422
    body = bad.json()
    assert body["code"] == "incompatible_schema"
    assert body["reasons"]  # 可区分的拒绝原因


def test_lineage_endpoints_flag_invalid_edges(client):
    _register(client, "orders")
    _register(client, "agg")
    logger.info("用例输入: 接受合法边，拒绝成环/缺失/重复")
    assert client.post("/lineage", json={"upstream": "orders", "downstream": "agg"}).json()["code"] == "accepted"
    dup = client.post("/lineage", json={"upstream": "orders", "downstream": "agg"}).json()
    cyc = client.post("/lineage", json={"upstream": "agg", "downstream": "orders"}).json()
    miss = client.post("/lineage", json={"upstream": "x", "downstream": "agg"}).json()
    logger.info("判定依据: dup=%s cycle=%s missing=%s", dup["code"], cyc["code"], miss["code"])
    assert dup["code"] == "duplicate_edge"
    assert cyc["code"] == "lineage_cycle"
    assert miss["code"] == "asset_not_found"

    impact = client.get("/lineage/impact/orders").json()
    assert impact["downstream_direct"] == ["agg"]


def test_batch_lineage_response(client):
    _register(client, "a")
    _register(client, "b")
    logger.info("用例输入: 批量 [a->b, a->b重复]")
    r = client.post(
        "/lineage/batch",
        json=[
            {"upstream": "a", "downstream": "b"},
            {"upstream": "a", "downstream": "b"},
        ],
    )
    codes = [x["code"] for x in r.json()["results"]]
    logger.info("判定依据: codes=%s", codes)
    assert codes == ["accepted", "duplicate_edge"]


def test_governance_conflict_http_409(client):
    _register(client, "orders")
    logger.info("用例输入: gv=0 更新成功，再次以 gv=0 写入应 409")
    ok = client.put(
        "/assets/orders/governance",
        json={"expected_governance_version": 0, "owner": "alice"},
    )
    assert ok.status_code == 200
    conflict = client.put(
        "/assets/orders/governance",
        json={"expected_governance_version": 0, "owner": "bob"},
    )
    logger.info("判定依据: status=%s body=%s", conflict.status_code, conflict.json())
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == "version_conflict"
    assert body["expected_version"] == 0 and body["actual_version"] == 1


def test_get_missing_asset_404(client):
    logger.info("用例输入: GET 不存在资产")
    r = client.get("/assets/ghost")
    logger.info("判定依据: status=%s code=%s", r.status_code, r.json()["code"])
    assert r.status_code == 404 and r.json()["code"] == "asset_not_found"


def test_http_data_survives_app_restart(persistent_client):
    """默认持久化仓库：写入后重新构造 app（等价于服务重启）数据完整。"""
    c1 = persistent_client()
    fields = [{"name": "id", "field_type": "long", "nullable": False}]
    assert _register(c1, "orders", fields).json()["action"] == "created"
    ok = c1.put(
        "/assets/orders/governance",
        json={"expected_governance_version": 0, "owner": "alice",
              "add_tags": ["pii"]},
    )
    assert ok.status_code == 200

    c2 = persistent_client()
    logger.info("用例输入: 重新构造 app 后 GET /assets/orders")
    body = c2.get("/assets/orders").json()
    logger.info("判定依据: sv=%s gv=%s owner=%s tags=%s",
                body["schema_version"], body["governance_version"],
                body["owner"], body["tags"])
    assert body["schema_version"] == 1
    assert body["governance_version"] == 1
    assert body["owner"] == "alice" and body["tags"] == ["pii"]
    assert c2.get("/assets/ghost").status_code == 404


def test_storage_error_maps_to_500(tmp_path, monkeypatch):
    """数据文件损坏时读接口给出 500 storage_corruption，而不是伪装成不存在。"""
    repo = tmp_path / "repo"
    c1 = TestClient(create_app(repo_dir=str(repo)))
    _register(c1, "orders", [{"name": "id", "field_type": "long"}])

    # 找到资产文件并破坏其内容
    files = list((repo / "assets").glob("*.json"))
    assert len(files) == 1
    files[0].write_text("{ broken json", encoding="utf-8")

    c2 = TestClient(create_app(repo_dir=str(repo)))
    r = c2.get("/assets/orders")
    logger.info("判定依据: 损坏文件 status=%s body=%s", r.status_code, r.json())
    assert r.status_code == 500
    assert r.json()["code"] == "storage_corruption"
