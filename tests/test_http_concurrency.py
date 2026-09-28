"""真实 HTTP 并发测试：启动真实 uvicorn 服务 + httpx 多客户端并发打 TCP。

TestClient 虽然也走 FastAPI 同步端点的 worker 线程池，但为了覆盖
"真实 HTTP 并发下"的承诺，这里在动态端口上启动真实的 uvicorn Server
（一个服务线程、多 worker 线程处理请求），并发请求来自多个 httpx
客户端线程，是最贴近线上部署的形态。

覆盖：

* 真实并发读 + 写：读不报错、不超时，读到的快照始终自洽；
* 并发写 + 写：治理/登记/血缘四类竞争，恰好一个成功，其余可区分冲突；
* 过期结构版本写入返回明确 409，绝不静默成功。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

try:
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None

from metadata_catalog.api import create_app

logger = logging.getLogger("metadata_catalog.tests")

pytestmark = pytest.mark.skipif(
    uvicorn is None, reason="需要 uvicorn 才能起真实 HTTP 服务"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(tmp_path):
    repo = str(tmp_path / "repo")
    app = create_app(repo_dir=repo)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("测试 HTTP 服务启动超时")
    yield base_url
    server.should_exit = True
    thread.join(timeout=10)


def _reg(client, base, name, fields=None, **kw):
    return client.post(
        f"{base}/assets/register",
        json={"name": name, "asset_type": "table", "fields": fields or [], **kw},
    )


def _seed(base):
    with httpx.Client(timeout=10) as c:
        assert _reg(c, base, "hot", [
            {"name": "id", "field_type": "long", "nullable": False}]).status_code == 200
        for nm in ("a", "b"):
            assert _reg(c, base, nm, [
                {"name": "id", "field_type": "long", "nullable": False}]).status_code == 200


def test_http_concurrent_reads_and_writes(http_server):
    base = http_server
    _seed(base)
    n_readers = 12
    n_writers = 12
    logger.info("用例输入: 真实 HTTP 下 %d 持续读者 + %d 并发治理写",
                n_readers, n_writers)

    read_errors: list[str] = []
    stop = threading.Event()
    start = threading.Event()

    def reader():
        count = 0
        with httpx.Client(timeout=10) as c:
            start.wait()
            while not stop.is_set():
                r = c.get(f"{base}/assets/hot")
                if r.status_code != 200:
                    read_errors.append(f"读返回 {r.status_code}: {r.text[:200]}")
                    return count
                body = r.json()
                # 快照自洽：治理版本只可能 0/1，结构始终是 sv=1 单字段
                if body["governance_version"] not in (0, 1):
                    read_errors.append(f"异常 gv: {body}")
                    return count
                if len(body["fields"]) != 1 or body["fields"][0]["name"] != "id":
                    read_errors.append(f"快照不自洽: {body}")
                    return count
                count += 1
        return count

    def writer(i):
        with httpx.Client(timeout=10) as c:
            start.wait()
            return c.put(
                f"{base}/assets/hot/governance",
                json={"expected_governance_version": 0, "owner": f"u{i}"},
            )

    with ThreadPoolExecutor(max_workers=n_readers + n_writers) as pool:
        read_futs = [pool.submit(reader) for _ in range(n_readers)]
        write_futs = [pool.submit(writer, i) for i in range(n_writers)]
        # 等所有线程就绪后统一起跑
        time.sleep(0.3)
        start.set()
        write_responses = [f.result() for f in write_futs]
        # 让读者再跑一会儿，覆盖写完成后的读
        time.sleep(0.3)
        stop.set()
        total_reads = sum(f.result() for f in read_futs)

    assert not read_errors, read_errors[:1]
    statuses = [r.status_code for r in write_responses]
    logger.info("判定依据: 写 200=%d 409=%d，读完成 %d 次零错误零超时",
                statuses.count(200), statuses.count(409), total_reads)
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_writers - 1
    for r in write_responses:
        if r.status_code == 409:
            b = r.json()
            assert b["code"] == "version_conflict"
            assert b["expected_version"] == 0 and b["actual_version"] == 1
    with httpx.Client(timeout=10) as c:
        body = c.get(f"{base}/assets/hot").json()
    assert body["governance_version"] == 1
    assert body["owner"] in {f"u{i}" for i in range(n_writers)}
    assert total_reads > 0


def test_http_concurrent_register_and_lineage(http_server):
    base = http_server
    _seed(base)
    n = 16
    fields = [{"name": "id", "field_type": "long", "nullable": False}]
    start = threading.Event()

    def register_once(_i):
        with httpx.Client(timeout=10) as c:
            start.wait()
            r = _reg(c, base, "dup", fields)
            body = r.json()
            return (r.status_code, body["action"], body["schema_version"])

    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(register_once, i) for i in range(n)]
        time.sleep(0.2)
        start.set()
        results = [f.result() for f in futs]
    created = [r for r in results if r[1] == "created"]
    idem = [r for r in results if r[1] == "re_registered_idempotent"]
    logger.info("判定依据: created=%d idem=%d", len(created), len(idem))
    assert len(created) == 1 and len(idem) == n - 1
    assert {r[2] for r in results} == {1}

    start = threading.Event()

    def edge_once(_i):
        with httpx.Client(timeout=10) as c:
            start.wait()
            r = c.post(f"{base}/lineage", json={"upstream": "a", "downstream": "b"})
            b = r.json()
            return (b["accepted"], b["code"])

    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(edge_once, i) for i in range(n)]
        time.sleep(0.2)
        start.set()
        er = [f.result() for f in futs]
    accepted = [r for r in er if r[0]]
    dup = [r for r in er if r[1] == "duplicate_edge"]
    logger.info("判定依据: accepted=%d duplicate=%d", len(accepted), len(dup))
    assert len(accepted) == 1 and len(dup) == n - 1
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{base}/lineage", json={"upstream": "a", "downstream": "b"})
    assert r.json()["code"] == "duplicate_edge"


def test_http_stale_schema_version_is_conflict(http_server):
    base = http_server
    _seed(base)
    n = 12
    start = threading.Event()
    with httpx.Client(timeout=10) as c:
        _reg(c, base, "hot", [
            {"name": "id", "field_type": "long", "nullable": False},
            {"name": "name", "field_type": "string"},
        ])

        def stale_evolve(i):
            with httpx.Client(timeout=10) as cc:
                start.wait()
                return _reg(cc, base, "hot", [
                    {"name": "id", "field_type": "long", "nullable": False},
                    {"name": f"col{i}", "field_type": "string"},
                ], expected_schema_version=1)

        with ThreadPoolExecutor(max_workers=n) as pool:
            futs = [pool.submit(stale_evolve, i) for i in range(n)]
            time.sleep(0.2)
            start.set()
            rs = [f.result() for f in futs]
    codes = [r.status_code for r in rs]
    logger.info("判定依据: 全部应 409，实际 %s", codes)
    assert codes == [409] * n
    for r in rs:
        b = r.json()
        assert b["code"] == "version_conflict"
        assert (b["expected_version"], b["actual_version"]) == (1, 2)
    # 冲突写入一律未生效
    with httpx.Client(timeout=10) as c:
        body = c.get(f"{base}/assets/hot").json()
    assert body["schema_version"] == 2
    assert {f["name"] for f in body["fields"]} == {"id", "name"}
