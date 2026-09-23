"""本地持久化存储, 用单个 JSON 文件模拟元数据仓库。

并发模型
--------
- 进程内: 每实例一把不可重入锁 ``threading.Lock``, 服务层以“事务回调”在
  持锁期间完成 取文件锁-读-校验-写-放文件锁 完整序列, 保证检查与生效原子化;
- 进程间: 对锁文件加 ``fcntl`` 咨询排他锁, 多进程操作同一目录时以磁盘文件
  为裁决点; 写操作采用临时文件 + ``os.replace`` 原子替换, 不会出现半截文件。

唯一事实来源是磁盘文件: 每次事务开始时重新加载最新已提交快照, 结束时落盘,
因此同机多进程 / 多线程看到的结论一致。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, TypeVar

from .models import Asset, FieldDef

try:  # Unix 提供进程间文件锁
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - 目标环境为 Linux
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

T = TypeVar("T")
_EDGE_KEY_SEP = "\x1f"


class LineageData:
    """血缘图的持久化形态: 正向/反向邻接表 + 每条边的版本。"""

    def __init__(self) -> None:
        self.out_edges: dict[str, set[str]] = {}
        self.in_edges: dict[str, set[str]] = {}
        self.edge_versions: dict[tuple[str, str], int] = {}
        self.version: int = 0


class CatalogData:
    """整个仓库的持久化快照。"""

    def __init__(self) -> None:
        self.assets: dict[str, Asset] = {}
        self.lineage = LineageData()

    # ---- 资产维度的血缘便捷查询 ---------------------------------------
    def downstream_of(self, asset_id: str) -> set[str]:
        return self.lineage.out_edges.get(asset_id, set())

    def upstream_of(self, asset_id: str) -> set[str]:
        return self.lineage.in_edges.get(asset_id, set())


class LocalStore:
    """JSON 文件存储。"""

    def __init__(self, path: str | Path = "catalog_data.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 不可重入锁: 必须覆盖“取文件锁->加载->改->落盘->放文件锁”整个区间,
        # 否则同进程其它线程会在中途进入, 用旧快照覆盖已提交数据(丢更新)。
        self._lock = threading.Lock()
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock_fd: int | None = None

    # ---- 加载 / 持久化 -------------------------------------------------
    def load(self) -> CatalogData:
        """从磁盘加载最新快照; 文件不存在时返回空数据。"""
        if not self.path.exists():
            return CatalogData()
        with self.path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return self._data_from_dict(raw)

    def _persist(self, data: CatalogData) -> None:
        """原子落盘: 先写临时文件再 rename, 崩溃时旧文件保持完整。"""
        payload = self._data_to_dict(data)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    # ---- 事务 ----------------------------------------------------------
    def transaction(self, callback: Callable[[CatalogData], T]) -> T:
        """在排他持锁期间执行 ``callback(data)``。

        回调正常返回时落盘并返回其结果; 回调抛异常则不落盘, 变更全部丢弃。
        """
        with self._lock:
            if _HAS_FCNTL:
                if self._lock_fd is None:
                    self._lock_fd = os.open(
                        self._lock_path, os.O_RDWR | os.O_CREAT, 0o644
                    )
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
                try:
                    return self._run_locked(callback)
                finally:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            return self._run_locked(callback)  # pragma: no cover

    def _run_locked(self, callback: Callable[[CatalogData], T]) -> T:
        data = self.load()
        result = callback(data)
        self._persist(data)
        return result

    # ---- 序列化 --------------------------------------------------------
    @staticmethod
    def _asset_to_dict(asset: Asset) -> dict:
        return {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "fields": [f.model_dump() for f in asset.fields],
            "tags": sorted(asset.tags),
            "owners": sorted(asset.owners),
            "schema_version": asset.schema_version,
            "meta_version": asset.meta_version,
            "created_at": asset.created_at,
            "updated_at": asset.updated_at,
        }

    @staticmethod
    def _asset_from_dict(raw: dict) -> Asset:
        return Asset(
            asset_id=raw["asset_id"],
            name=raw.get("name", ""),
            asset_type=raw.get("asset_type", "table"),
            fields=[FieldDef(**f) for f in raw.get("fields", [])],
            tags=set(raw.get("tags", [])),
            owners=set(raw.get("owners", [])),
            schema_version=raw.get("schema_version", 0),
            meta_version=raw.get("meta_version", 0),
            created_at=raw.get("created_at", 0.0),
            updated_at=raw.get("updated_at", 0.0),
        )

    def _data_to_dict(self, data: CatalogData) -> dict:
        return {
            "version": 1,
            "assets": {
                aid: self._asset_to_dict(asset)
                for aid, asset in sorted(data.assets.items())
            },
            "lineage": {
                "version": data.lineage.version,
                "out_edges": {
                    k: sorted(v) for k, v in sorted(data.lineage.out_edges.items())
                },
                "in_edges": {
                    k: sorted(v) for k, v in sorted(data.lineage.in_edges.items())
                },
                "edge_versions": {
                    _EDGE_KEY_SEP.join(k): v
                    for k, v in sorted(data.lineage.edge_versions.items())
                },
            },
        }

    def _data_from_dict(self, raw: dict) -> CatalogData:
        data = CatalogData()
        for aid, araw in raw.get("assets", {}).items():
            data.assets[aid] = self._asset_from_dict(araw)
        lraw = raw.get("lineage", {})
        data.lineage.version = lraw.get("version", 0)
        for k, vs in lraw.get("out_edges", {}).items():
            data.lineage.out_edges[k] = set(vs)
        for k, vs in lraw.get("in_edges", {}).items():
            data.lineage.in_edges[k] = set(vs)
        for key, ver in lraw.get("edge_versions", {}).items():
            up, down = key.split(_EDGE_KEY_SEP, 1)
            data.lineage.edge_versions[(up, down)] = ver
        return data
