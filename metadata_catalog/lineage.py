"""上下游血缘的写入、校验与影响范围查询。

边方向: ``upstream -> downstream``(上游产出/影响下游)。

判定(均有明确结论, 不静默接受或丢弃):
- 端点不存在        -> LineageEndpointMissingError, details.missing 列出缺失资产;
- 成环(含自环)      -> LineageCycleError, details.cycle 给出闭环路径;
- 重复边            -> LineageWriteStatus.DUPLICATE_EDGE, 幂等不提升版本;
- 正常写入          -> LineageWriteStatus.CREATED。
"""

from __future__ import annotations

import logging
from collections import deque

from .errors import (
    AssetNotFoundError,
    InvalidArgumentError,
    LineageCycleError,
    LineageEndpointMissingError,
)
from .models import ImpactReport, LineageWriteResult, LineageWriteStatus

logger = logging.getLogger("metadata_catalog.lineage")


def add_edge(data, *, upstream: str, downstream: str) -> LineageWriteResult:
    """校验并写入一条血缘边(调用方需已持锁)。"""
    logger.info("add_edge 输入 upstream=%s downstream=%s", upstream, downstream)

    # 1) 端点必须存在(且非空)。
    missing = [
        a for a in dict.fromkeys((upstream, downstream)) if a not in data.assets
    ]
    if missing:
        err = LineageEndpointMissingError(
            upstream=upstream, downstream=downstream, missing=missing
        )
        logger.warning("血缘写入被拒绝 判定=%s 依据=%s", err.error_code.value, err.message)
        raise err

    # 2) 重复边: 幂等。
    key = (upstream, downstream)
    if downstream in data.lineage.out_edges.get(upstream, set()):
        version = data.lineage.edge_versions[key]
        logger.info(
            "判定 DUPLICATE_EDGE(重复边,幂等) %s -> %s edge_version=%s",
            upstream, downstream, version,
        )
        return LineageWriteResult(
            upstream=upstream,
            downstream=downstream,
            status=LineageWriteStatus.DUPLICATE_EDGE,
            version=version,
            reason="边已存在, 重复写入按幂等处理, 不重复计数也不提升版本",
        )

    # 3) 成环检测: 加入 u->v 后若 v 已能到达 u, 则形成闭环。
    cycle = would_create_cycle(data, upstream=upstream, downstream=downstream)
    if cycle is not None:
        err = LineageCycleError(
            upstream=upstream,
            downstream=downstream,
            cycle=cycle,
        )
        logger.warning("血缘写入被拒绝 判定=%s 依据=血缘成环 path=%s",
                       err.error_code.value, cycle)
        raise err

    # 4) 写入。
    data.lineage.out_edges.setdefault(upstream, set()).add(downstream)
    data.lineage.in_edges.setdefault(downstream, set()).add(upstream)
    # 保证孤立节点也有键, 便于遍历与持久化的稳定形态。
    data.lineage.out_edges.setdefault(downstream, set())
    data.lineage.in_edges.setdefault(upstream, set())
    data.lineage.version += 1
    data.lineage.edge_versions[key] = data.lineage.version

    logger.info(
        "判定 CREATED %s -> %s lineage_version=%s",
        upstream,
        downstream,
        data.lineage.version,
    )
    return LineageWriteResult(
        upstream=upstream,
        downstream=downstream,
        status=LineageWriteStatus.CREATED,
        version=data.lineage.version,
        reason="边不存在且端点齐全、不会成环, 写入成功",
    )


def would_create_cycle(data, *, upstream: str, downstream: str) -> list[str] | None:
    """若加入 ``upstream -> downstream`` 会成环, 返回闭环路径, 否则 None。

    判定依据: 新边从 downstream 出发若能沿已有边回到 upstream, 即
    ``upstream -> downstream -> ... -> upstream`` 构成闭环。自环同理。
    """
    if upstream == downstream:
        return [upstream, downstream]

    # BFS 找 downstream 到 upstream 的路径。
    prev: dict[str, str] = {}
    visited = {downstream}
    queue = deque([downstream])
    while queue:
        node = queue.popleft()
        for nxt in sorted(data.lineage.out_edges.get(node, set())):
            if nxt in visited:
                continue
            visited.add(nxt)
            prev[nxt] = node
            if nxt == upstream:
                # 回溯 upstream -> ... -> downstream 段。
                path = [upstream]
                cur = upstream
                while cur != downstream:
                    cur = prev[cur]
                    path.append(cur)
                path.append(upstream)  # 闭合, 直观展示环
                return path
            queue.append(nxt)
    return None


def impact(
    data,
    *,
    asset_id: str,
    direction: str = "downstream",
) -> ImpactReport:
    """计算直接(深度1)与传递(深度>=2)影响范围。"""
    if asset_id not in data.assets:
        raise AssetNotFoundError(asset_id, context="impact 查询")
    if direction not in ("downstream", "upstream"):
        raise InvalidArgumentError(
            f"direction 仅支持 downstream/upstream, 收到: {direction}",
            details={"direction": direction},
        )

    adj = (
        data.lineage.out_edges
        if direction == "downstream"
        else data.lineage.in_edges
    )
    logger.info("impact 输入 asset_id=%s direction=%s", asset_id, direction)

    depth: dict[str, int] = {}
    visited = {asset_id}
    queue = deque([(asset_id, 0)])
    while queue:
        node, d = queue.popleft()
        for nxt in sorted(adj.get(node, set())):
            if nxt in visited:
                continue
            visited.add(nxt)
            depth[nxt] = d + 1
            queue.append((nxt, d + 1))

    direct = sorted([n for n, dep in depth.items() if dep == 1])
    transitive = sorted([n for n, dep in depth.items() if dep >= 2])
    reachable = sorted(depth, key=lambda n: (depth[n], n))
    return ImpactReport(
        asset_id=asset_id,
        direction=direction,
        direct=direct,
        transitive=transitive,
        reachable=reachable,
        depth=depth,
        reason=(
            f"BFS 沿{('下游(out)' if direction == 'downstream' else '上游(in)')}邻接"
            "计算: 深度1为直接影响, 深度>=2为传递影响"
        ),
    )
