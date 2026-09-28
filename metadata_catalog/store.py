"""本地内存存储：线程安全的进程内元数据仓库。

并发模型与持久化仓库保持一致，这样"同一仓库在同一进程内"的结论不会因为
注入的存储实现不同而改变：

* 读临界区是共享锁，多个读者可以真正并行；
* 写临界区是全局排他锁，服务层在同一个 ``transaction()`` 内完成
  "检查 + 写入"，没有 TOCTOU 窗口；
* 读出来的快照一律是独立副本，调用方（或失败的写入路径）无法通过持有
  的引用污染仓库内部状态。

数据不跨进程持久化，定位为本地临时仓库/测试仓库。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .locking import TransactionGate
from .models import AssetSnapshot, LineageEdge


class LocalStore:
    """线程安全的本地元数据仓库（多读单写、可重入）。"""

    def __init__(self) -> None:
        self._gate = TransactionGate()
        self._assets: dict[str, AssetSnapshot] = {}
        self._edges: dict[tuple[str, str], LineageEdge] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """进入进程内排他临界区；可重入，服务层在其中完成检查与写入。"""
        with self._gate.transaction():
            yield

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """进入共享读临界区；读者之间并行、与写者互斥。可重入。"""
        with self._gate.read_transaction():
            yield

    # ---- 资产 ----
    def get_asset(self, name: str) -> AssetSnapshot | None:
        with self._gate.read_transaction():
            snap = self._assets.get(name)
            return snap.copy() if snap is not None else None

    def put_asset(self, snapshot: AssetSnapshot) -> None:
        with self._gate.transaction():
            self._put_asset(snapshot)

    def _put_asset(self, snapshot: AssetSnapshot) -> None:
        # 存一份独立副本：仓库内部状态不与调用方共享可变引用。
        self._assets[snapshot.name] = snapshot.copy()

    def list_assets(self) -> list[AssetSnapshot]:
        with self._gate.read_transaction():
            return [snap.copy() for snap in self._assets.values()]

    # ---- 血缘 ----
    def has_edge(self, upstream: str, downstream: str) -> bool:
        with self._gate.read_transaction():
            return (upstream, downstream) in self._edges

    def add_edge(self, edge: LineageEdge) -> None:
        with self._gate.transaction():
            self._edges[edge.key()] = edge

    def remove_edge(self, upstream: str, downstream: str) -> None:
        with self._gate.transaction():
            self._edges.pop((upstream, downstream), None)

    def list_edges(self) -> list[LineageEdge]:
        with self._gate.read_transaction():
            return list(self._edges.values())

    # ---- 调试/测试辅助 ----
    def reset(self) -> None:
        with self._gate.transaction():
            self._assets.clear()
            self._edges.clear()

    def close(self) -> None:
        """与 PersistentStore 对齐的空操作，方便统一的生命周期写法。"""
