"""本地内存存储，模拟元数据仓库。

用一把可重入锁保护全部可变状态，保证并发下的结论一致性。
服务层在同一个 ``transaction()`` 临界区内完成"检查 + 写入"，
避免 TOCTOU 问题。数据不跨进程持久化，定位为本地可运行的模拟仓库。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .models import AssetSnapshot, LineageEdge


class LocalStore:
    """线程安全的本地元数据仓库。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._assets: dict[str, AssetSnapshot] = {}
        self._edges: dict[tuple[str, str], LineageEdge] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """进入全局临界区；可重入，服务层在其中完成检查与写入。"""
        with self._lock:
            yield

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """读临界区。

        内存仓库没有进程间可见性问题，读直接复用同一把可重入锁；
        持久化仓库则在此处放宽为跨进程共享锁以允许并发读者。
        """
        with self._lock:
            yield

    # ---- 资产 ----
    def get_asset(self, name: str) -> AssetSnapshot | None:
        with self._lock:
            return self._assets.get(name)

    def put_asset(self, snapshot: AssetSnapshot) -> None:
        with self._lock:
            self._assets[snapshot.name] = snapshot

    def list_assets(self) -> list[AssetSnapshot]:
        with self._lock:
            return list(self._assets.values())

    # ---- 血缘 ----
    def has_edge(self, upstream: str, downstream: str) -> bool:
        with self._lock:
            return (upstream, downstream) in self._edges

    def add_edge(self, edge: LineageEdge) -> None:
        with self._lock:
            self._edges[edge.key()] = edge

    def remove_edge(self, upstream: str, downstream: str) -> None:
        with self._lock:
            self._edges.pop((upstream, downstream), None)

    def list_edges(self) -> list[LineageEdge]:
        with self._lock:
            return list(self._edges.values())

    # ---- 调试/测试辅助 ----
    def reset(self) -> None:
        with self._lock:
            self._assets.clear()
            self._edges.clear()
