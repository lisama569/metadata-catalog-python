"""pytest 公共配置。

* 开启 metadata_catalog 日志，使每个用例都能看到"输入"与"判定依据"；
* 提供隔离的 catalog fixture。
"""

from __future__ import annotations

import logging

import pytest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.store import LocalStore


@pytest.fixture
def catalog() -> MetadataCatalog:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        force=True,
    )
    logging.getLogger("metadata_catalog").setLevel(logging.INFO)
    return MetadataCatalog(LocalStore())
