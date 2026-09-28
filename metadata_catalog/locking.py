"""进程内并发原语：可重入读写锁与每线程事务门。

* :class:`ReentrantRWLock` 提供"多读单写、同线程可重入、写者优先防饿死"
  的进程内锁；
* :class:`TransactionGate` 在读写锁之上维护**每线程**的事务模式与重入深度，
  并在"本进程最外层事务"获取/释放时触发钩子（持久化仓库用它挂 flock 与
  缓存同步）。

锁状态必须按线程隔离：若把"当前是否持锁"放在实例字段上，一个线程会读到
另一个线程的持锁状态，从而错误地以"嵌套"为名免锁进入临界区。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

NONE = 0
SHARED = 1
EXCLUSIVE = 2


class ReentrantRWLock:
    """可重入读写锁：多读单写；同线程读可重入、写可重入。

    写者优先：有写者排队时新读者排队，防止持续读流饿死写者。
    不支持"读锁升级为写锁"（会自锁，直接抛 RuntimeError）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                # 写持锁线程重入：写锁已包含读权限。
                return
            if tid in self._readers:
                self._readers[tid] += 1
                return
            while self._writer is not None or self._waiting_writers > 0:
                self._cond.wait()
            self._readers[tid] = 1

    def release_read(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                return
            depth = self._readers.get(tid, 0)
            if depth <= 1:
                self._readers.pop(tid, None)
            else:
                self._readers[tid] = depth - 1
            if not self._readers:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                self._writer_depth += 1
                return
            if tid in self._readers:
                raise RuntimeError("不支持在读事务内嵌套写事务")
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = tid
            self._writer_depth = 1

    def release_write(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer != tid:
                raise RuntimeError("release_write 由未持写锁的线程调用")
            self._writer_depth -= 1
            if self._writer_depth == 0:
                self._writer = None
                self._cond.notify_all()


class _Local:
    __slots__ = ("mode", "depth")

    def __init__(self) -> None:
        self.mode = NONE
        self.depth = 0


class TransactionGate:
    """每线程可重入的共享/排他事务门。

    :param on_outer_enter: 本进程某个线程拿到"最外层"事务后调用的钩子，
        参数为事务模式（SHARED/EXCLUSIVE）；抛异常时事务不会生效。
    :param on_outer_exit: 最外层事务释放时调用（钩子异常被吞掉，以免掩盖
        业务异常）。

    同线程：写内可嵌套读/写；读内可嵌套读；读内嵌套写属于实现错误。
    """

    def __init__(
        self,
        *,
        on_outer_enter: Callable[[int], None] | None = None,
        on_outer_exit: Callable[[int], None] | None = None,
    ) -> None:
        self._rw = ReentrantRWLock()
        self._local = threading.local()
        self._on_outer_enter = on_outer_enter
        self._on_outer_exit = on_outer_exit

    def _state(self) -> _Local:
        st = self._local.__dict__.get("st")
        if st is None:
            st = _Local()
            self._local.st = st
        return st

    def held_mode(self) -> int:
        """当前线程所处的事务模式（NONE/SHARED/EXCLUSIVE）。"""
        return self._state().mode

    def enter(self, mode: int) -> None:
        st = self._state()
        if st.mode != NONE:
            if st.mode == SHARED and mode == EXCLUSIVE:
                raise RuntimeError("不支持在读事务内嵌套写事务")
            st.depth += 1
            return
        if mode == EXCLUSIVE:
            self._rw.acquire_write()
        else:
            self._rw.acquire_read()
        outer_ok = False
        try:
            if self._on_outer_enter is not None:
                self._on_outer_enter(mode)
            outer_ok = True
        finally:
            if not outer_ok:
                if mode == EXCLUSIVE:
                    self._rw.release_write()
                else:
                    self._rw.release_read()
        st.mode = mode
        st.depth = 1

    def leave(self) -> None:
        st = self._state()
        if st.mode == NONE or st.depth <= 0:
            raise RuntimeError("没有处于开启状态的事务")
        st.depth -= 1
        if st.depth > 0:
            return
        mode = st.mode
        st.mode = NONE
        if self._on_outer_exit is not None:
            try:
                self._on_outer_exit(mode)
            except Exception:
                pass
        if mode == EXCLUSIVE:
            self._rw.release_write()
        else:
            self._rw.release_read()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.enter(EXCLUSIVE)
        try:
            yield
        finally:
            self.leave()

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        self.enter(SHARED)
        try:
            yield
        finally:
            self.leave()
