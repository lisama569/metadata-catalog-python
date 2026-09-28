"""真实 HTTP 并发测试：独立 uvicorn 进程 + 持久化仓库 + 多线程客户端。

不使用 TestClient 的同步包装，而是真正拉起一个监听端口的服务进程
（与生产 ``uv run fastapi run main.py`` 同一路径，默认 PersistentStore），
再用线程池同时发起请求，覆盖：

* 并发读 + 并发写混合：读请求不超时、不出现 5xx 未分类异常；
* 同版本并发演进 / 并发治理：恰好一个 200，其余 409 version_conflict；
* 并发首次登记：一个 created，其余幂等，版本恒为 1；
* 并发重复血缘：一个 accepted，其余 duplicate_edge。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

logger = logging.getLogger("metadata_catalog.tests")

_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    """启动一个真实 uvicorn 进程，仓库指向临时目录；返回 base_url。"""
    repo = str(tmp_path / "repo")
    port = _free_port()
    app_file = tmp_path / "http_app.py"
    app_file.write_text(
        "import os\n"
        f"os.environ['METADATA_CATALOG_REPO'] = {repo!r}\n"
        "from metadata_catalog.api import create_app\n"
        "app = create_app()\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(_ROOT))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "http_app:app",
            "--app-dir",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    try:
        while time.time() < deadline:
            try:
                if httpx.get(base + "/", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace")
                pytest.fail(f"服务进程提前退出:\n{out}")
        else:
            proc.terminate()
            pytest.fail("服务进程在 30s 内未就绪")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _register(client, base, name, fields=None, expected=None):
    body = {"name": name, "asset_type": "table", "fields": fields or []}
    if expected is not None:
        body["expected_schema_version"] = expected
    return client.post(base + "/assets/register", json=body)


def test_http_concurrent_reads_and_writes(server):
    base = server
    with httpx.Client(timeout=10) as c:
        r = _register(c, base, "hot", [
            {"name": "id", "field_type": "long", "nullable": False}])
        assert r.status_code == 200

    n_readers, n_writers = 6, 6
    errors: list[str] = []
    statuses: list[int] = []

    def read():
        try:
            with httpx.Client(timeout=10) as c:
                for _ in range(20):
                    r1 = c.get(base + "/assets/hot")
                    r2 = c.get(base + "/assets")
                    if r1.status_code != 200 or r2.status_code != 200:
                        errors.append(f"read status {r1.status_code}/{r2.status_code}")
                        return
                    body = r1.json()
                    # 结构版本与字段必须始终自洽：不允许半更新
                    assert len(body["fields"]) == body["schema_version"]
                    statuses.append(r1.status_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def write(i):
        try:
            with httpx.Client(timeout=10) as c:
                cur = c.get(base + "/assets/hot").json()
                fields = [
                    {"name": f["name"], "field_type": f["field_type"],
                     "nullable": f["nullable"]}
                    for f in cur["fields"]
                ]
                fields.append({"name": f"c{i}", "field_type": "string"})
                r = _register(c, base, "hot", fields,
                              expected=cur["schema_version"])
                # 成功或冲突都合法；不允许 5xx
                if r.status_code not in (200, 409):
                    errors.append(f"write status {r.status_code}: {r.text}")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=n_readers + n_writers) as pool:
        futs = [pool.submit(read) for _ in range(n_readers)]
        futs += [pool.submit(write, i) for i in range(n_writers)]
        for f in futs:
            f.result()

    logger.info("判定依据: 读状态样本=%s 错误=%s", statuses[:5], errors)
    assert errors == []
    with httpx.Client(timeout=10) as c:
        body = c.get(base + "/assets/hot").json()
        assert len(body["fields"]) == body["schema_version"]
        assert body["schema_version"] >= 2


def test_http_concurrent_evolution_one_wins(server):
    base = server
    with httpx.Client(timeout=10) as c:
        _register(c, base, "hot", [
            {"name": "id", "field_type": "long", "nullable": False}])

    n = 10

    def write(i):
        with httpx.Client(timeout=10) as c:
            return _register(
                c, base, "hot",
                [{"name": "id", "field_type": "long", "nullable": False},
                 {"name": f"col{i}", "field_type": "string"}],
                expected=1,
            )

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(write, range(n)))

    codes = [r.status_code for r in results]
    logger.info("判定依据: 状态码=%s", codes)
    assert codes.count(200) == 1
    assert codes.count(409) == n - 1
    for r in results:
        if r.status_code == 409:
            b = r.json()
            assert b["code"] == "version_conflict"
            assert b["expected_version"] == 1 and b["actual_version"] == 2
    with httpx.Client(timeout=10) as c:
        body = c.get(base + "/assets/hot").json()
        assert body["schema_version"] == 2
        names = {f["name"] for f in body["fields"]}
        assert "id" in names and len(names) == 2
        assert any(n.startswith("col") for n in names)


def test_http_concurrent_governance_one_wins(server):
    base = server
    with httpx.Client(timeout=10) as c:
        _register(c, base, "hot")

    n = 10

    def write(i):
        with httpx.Client(timeout=10) as c:
            return c.put(
                base + "/assets/hot/governance",
                json={"expected_governance_version": 0,
                      "owner": f"user{i}"},
            )

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(write, range(n)))
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1
    assert codes.count(409) == n - 1
    with httpx.Client(timeout=10) as c:
        body = c.get(base + "/assets/hot").json()
        assert body["governance_version"] == 1
        assert body["owner"].startswith("user")


def test_http_concurrent_register_and_duplicate_lineage(server):
    base = server
    with httpx.Client(timeout=10) as c:
        _register(c, base, "a")
        _register(c, base, "b")

    n = 10

    def register_dup():
        with httpx.Client(timeout=10) as c:
            return _register(c, base, "dup", [
                {"name": "id", "field_type": "long", "nullable": False}])

    def add_edge():
        with httpx.Client(timeout=10) as c:
            return c.post(base + "/lineage",
                          json={"upstream": "a", "downstream": "b"})

    with ThreadPoolExecutor(max_workers=2 * n) as pool:
        regs = list(pool.map(lambda _: register_dup(), range(n)))
        edges = list(pool.map(lambda _: add_edge(), range(n)))

    reg_codes = [r.json()["action"] for r in regs]
    edge_codes = [r.json()["code"] for r in edges]
    logger.info("判定依据: register=%s edges=%s", reg_codes, edge_codes)
    assert reg_codes.count("created") == 1
    assert reg_codes.count("re_registered_idempotent") == n - 1
    assert edge_codes.count("accepted") == 1
    assert edge_codes.count("duplicate_edge") == n - 1
    with httpx.Client(timeout=10) as c:
        assert c.get(base + "/assets/dup").json()["schema_version"] == 1
