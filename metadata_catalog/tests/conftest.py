"""测试夹具与日志校验工具。

每个测试使用独立的临时数据文件, 并将 ``metadata_catalog`` 日志器的 INFO
级别日志(含输入与判定依据)传播到 caplog, 满足“单测日志打印输入与判定依据”。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from metadata_catalog.service import MetadataCatalogService
from metadata_catalog.store import LocalStore

CATALOG_LOGGER = "metadata_catalog"


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "catalog.json")


@pytest.fixture
def service(store: LocalStore) -> MetadataCatalogService:
    return MetadataCatalogService(store)


@pytest.fixture(autouse=True)
def _capture_catalog_logs(caplog):
    caplog.set_level(logging.INFO, logger=CATALOG_LOGGER)
    yield


def assert_log_contains(caplog, *needles: str) -> None:
    """断言日志中出现了所有给定片段(用于核对“输入”与“判定依据”被打印)。"""
    text = "\n".join(
        record.getMessage() for record in caplog.records
    )
    missing = [n for n in needles if n not in text]
    assert not missing, f"日志中缺少 {missing};\n实际日志:\n{text}"
