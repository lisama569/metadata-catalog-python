"""元数据目录服务：资产登记/结构演进、血缘、治理信息。

本层是唯一的业务入口，负责：

* 输入校验与幂等判定；
* 结构 diff 与兼容性裁决（不兼容必须拒绝，不得静默覆盖）；
* 血缘边校验（不存在资产、重复、成环都要给出明确结论）；
* 治理信息的乐观并发控制（过期版本写入明确冲突）。

所有写操作在存储层锁内完成"检查 + 写入"，保证并发下结论一致。
"""

from __future__ import annotations

import logging

from .errors import (
    AssetNotFoundError,
    IncompatibleSchemaError,
    ValidationError,
    VersionConflictError,
)
from .models import (
    AssetSnapshot,
    AssetSpec,
    EdgeCheckResult,
    FieldRecord,
    FieldSpec,
    GovernanceOutcome,
    GovernanceUpdate,
    ImpactResult,
    LineageEdge,
    RegisterOutcome,
    RegisterRequest,
    now_ts,
)
from .store import LocalStore

logger = logging.getLogger("metadata_catalog")


def _validate_spec(spec: AssetSpec) -> None:
    """校验登记声明的基本约束，不满足直接抛 ValidationError。"""
    if not spec.name or not str(spec.name).strip():
        raise ValidationError("资产名称不能为空")
    seen: set[str] = set()
    for f in spec.fields:
        if not f.name or not f.name.strip():
            raise ValidationError(f"资产 {spec.name} 存在未命名字段")
        if f.name in seen:
            raise ValidationError(f"资产 {spec.name} 的字段名重复: {f.name}")
        seen.add(f.name)


def _validate_renames(
    renames: dict[str, str], old_names: set[str], new_names: set[str]
) -> None:
    """校验重命名声明本身是否自洽。"""
    if not renames:
        return
    targets: set[str] = set()
    for old, new in renames.items():
        if old not in old_names:
            raise ValidationError(f"重命名源字段在当前结构中不存在: {old}")
        if not new:
            raise ValidationError(f"字段 {old} 的重命名目标不能为空")
        if new not in new_names:
            raise ValidationError(
                f"重命名目标字段 {new} 未出现在新结构中（源: {old}）"
            )
        if new in targets:
            raise ValidationError(f"多个字段被重命名为同一目标名: {new}")
        targets.add(new)
        # 新名字若同时是一个"未声明重命名"的旧字段，则与保留字段冲突。
        if new in old_names and new not in renames:
            raise IncompatibleSchemaError(
                f"重命名目标 {new} 与保留的同名字段冲突，无法裁决归属",
                reasons=[f"rename_target_conflicts_with_kept_field:{new}"],
            )


def _diff_fields(
    old_fields: dict[str, FieldRecord],
    new_fields: list[FieldSpec],
    renames: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """对比新旧结构，返回 (变更说明, 不兼容原因列表)。

    兼容变更：新增字段、删除字段、显式声明的重命名、放宽 nullable。
    不兼容变更：字段类型变化、收紧 nullable、重命名同时改类型。
    """
    changes: dict[str, str] = {}
    reasons: list[str] = []
    old_to_new = dict(renames)  # old_name -> new_name
    new_by_name = {f.name: f for f in new_fields}

    # 1. 逐字段检查"旧字段"的去向。
    for old_name, record in old_fields.items():
        old_spec = record.spec
        if old_name in old_to_new:
            new_name = old_to_new[old_name]
            new_spec = new_by_name[new_name]
            changes[new_name] = f"renamed_from:{old_name}"
            if new_spec.field_type != old_spec.field_type:
                reasons.append(
                    f"字段重命名同时变更类型: {old_name}({old_spec.field_type.value})"
                    f" -> {new_name}({new_spec.field_type.value})"
                )
            if not new_spec.nullable and old_spec.nullable:
                reasons.append(
                    f"字段重命名同时收紧为非空: {old_name} -> {new_name}"
                )
            continue
        if old_name in new_by_name:
            new_spec = new_by_name[old_name]
            if new_spec.field_type != old_spec.field_type:
                reasons.append(
                    f"字段类型变更不兼容: {old_name} "
                    f"{old_spec.field_type.value} -> {new_spec.field_type.value}"
                )
            if not new_spec.nullable and old_spec.nullable:
                reasons.append(f"字段由可空收紧为非空不兼容: {old_name}")
            elif new_spec.nullable and not old_spec.nullable:
                changes[old_name] = "nullable_relaxed"
            elif new_spec.description != old_spec.description:
                changes[old_name] = "description_updated"
        else:
            changes[old_name] = "removed"

    # 2. 检查"新字段"的来源。
    rename_targets = {v: k for k, v in old_to_new.items()}
    for new_spec in new_fields:
        if new_spec.name in rename_targets:
            continue  # 已按重命名处理
        if new_spec.name not in old_fields:
            changes[new_spec.name] = "added"

    return changes, reasons


def _find_cycle_if_added(
    store: LocalStore, upstream: str, downstream: str
) -> list[str] | None:
    """若新增 ``upstream -> downstream`` 会成环，返回环路径，否则 None。

    成环条件等价于：当前图中已存在从 ``downstream`` 到 ``upstream`` 的路径。
    邻接关系通过存储层增量维护的 ``downstream_of`` 索引按需展开（O(可达规模)），
    而不是每条边都从全量边重建整张图。
    """
    # DFS 找 downstream -> ... -> upstream 的路径
    stack: list[tuple[str, list[str]]] = [(downstream, [downstream])]
    visited: set[str] = {downstream}
    while stack:
        node, path = stack.pop()
        if node == upstream:
            return path + [downstream]
        for nxt in store.downstream_of(node):
            if nxt not in visited:
                visited.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


def _transitive(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    """从 ``start`` 沿邻接表求传递可达集合（不含起点自身）。

    已登记的图默认无环，但仍用 visited 集合保证即使存在历史脏数据也不死循环。
    """
    reached: set[str] = set()
    stack = list(adjacency.get(start, []))
    while stack:
        node = stack.pop()
        if node in reached or node == start:
            continue
        reached.add(node)
        stack.extend(adjacency.get(node, []))
    return reached


class MetadataCatalog:
    """元数据目录服务。"""

    def __init__(self, store: LocalStore | None = None) -> None:
        self.store = store or LocalStore()

    # ---- 资产登记与结构演进 ----
    def register(self, request: RegisterRequest) -> RegisterOutcome:
        """登记新资产或上报已有资产的新结构。

        语义见模块文档与 docs：新建 / 幂等重复 / 兼容演进 / 拒绝。
        """
        spec = request.spec
        _validate_spec(spec)
        logger.info(
            "register 输入: asset=%s type=%s fields=%s renames=%s expected_v=%s",
            spec.name,
            spec.asset_type.value,
            [f"{f.name}:{f.field_type.value}" for f in spec.fields],
            request.renames,
            request.expected_schema_version,
        )

        with self.store.transaction():
            existing = self.store.get_asset(spec.name)

            # ---- 新建 ----
            if existing is None:
                if request.renames:
                    raise ValidationError(
                        f"资产 {spec.name} 尚未登记，不能携带重命名声明"
                    )
                snapshot = AssetSnapshot(
                    name=spec.name,
                    asset_type=spec.asset_type,
                    description=spec.description,
                    schema_version=1,
                    fields={
                        f.name: FieldRecord(spec=f, since_version=1)
                        for f in spec.fields
                    },
                    owner=None,
                    tags=set(),
                    governance_version=0,
                    created_at=now_ts(),
                    updated_at=now_ts(),
                )
                self.store.put_asset(snapshot)
                changed = {f.name: "added" for f in spec.fields}
                logger.info(
                    "register 判定: created asset=%s schema_version=1 fields=%d",
                    spec.name,
                    len(spec.fields),
                )
                return RegisterOutcome(
                    asset=spec.name,
                    action="created",
                    schema_version=1,
                    changed_fields=changed,
                    reason="首次登记，schema_version=1",
                )

            # ---- 乐观版本检查 ----
            if (
                request.expected_schema_version is not None
                and request.expected_schema_version != existing.schema_version
            ):
                logger.info(
                    "register 判定: version_conflict asset=%s expected=%s actual=%s",
                    spec.name,
                    request.expected_schema_version,
                    existing.schema_version,
                )
                raise VersionConflictError(
                    f"结构版本过期: 期望 {request.expected_schema_version}, "
                    f"实际 {existing.schema_version}",
                    expected=request.expected_schema_version,
                    actual=existing.schema_version,
                )

            old_fields = existing.fields
            _validate_renames(
                request.renames, set(old_fields), {f.name for f in spec.fields}
            )

            # 资产种类变化视为不兼容。
            if spec.asset_type != existing.asset_type:
                reasons = [
                    f"资产种类变更不兼容: {existing.asset_type.value} "
                    f"-> {spec.asset_type.value}"
                ]
                logger.info(
                    "register 判定: incompatible asset=%s reasons=%s",
                    spec.name,
                    reasons,
                )
                raise IncompatibleSchemaError(
                    "资产种类不可变更", reasons=reasons
                )

            changes, reasons = _diff_fields(old_fields, spec.fields, request.renames)
            if reasons:
                logger.info(
                    "register 判定: incompatible asset=%s reasons=%s",
                    spec.name,
                    reasons,
                )
                raise IncompatibleSchemaError(
                    f"资产 {spec.name} 的结构演进不兼容，已拒绝且未写入",
                    reasons=reasons,
                )

            # ---- 幂等：结构完全一致 ----
            structural = {
                f.name: (f.field_type, f.nullable) for f in spec.fields
            }
            old_structural = {
                n: (r.spec.field_type, r.spec.nullable)
                for n, r in old_fields.items()
            }
            if not changes and structural == old_structural:
                # 描述等非结构信息允许刷新，但结构版本不动，结论幂等。
                existing.description = spec.description
                existing.updated_at = now_ts()
                self.store.put_asset(existing)
                logger.info(
                    "register 判定: idempotent asset=%s schema_version=%s",
                    spec.name,
                    existing.schema_version,
                )
                return RegisterOutcome(
                    asset=spec.name,
                    action="re_registered_idempotent",
                    schema_version=existing.schema_version,
                    changed_fields={},
                    reason="结构与已登记版本完全一致，重复上报幂等，不产生新版本",
                )

            # ---- 兼容演进：构造下一个版本 ----
            next_version = existing.schema_version + 1
            rename_targets = {v: k for k, v in request.renames.items()}
            next_fields: dict[str, FieldRecord] = {}
            for f in spec.fields:
                if f.name in rename_targets:
                    next_fields[f.name] = FieldRecord(
                        spec=f, since_version=next_version
                    )
                elif f.name in old_fields:
                    # 保留字段：沿用首次登记版本，但采用新声明（如 nullable 放宽）
                    next_fields[f.name] = FieldRecord(
                        spec=f, since_version=old_fields[f.name].since_version
                    )
                else:
                    next_fields[f.name] = FieldRecord(
                        spec=f, since_version=next_version
                    )

            existing.fields = next_fields
            existing.schema_version = next_version
            existing.description = spec.description
            existing.updated_at = now_ts()
            self.store.put_asset(existing)
            logger.info(
                "register 判定: evolved asset=%s schema_version=%s changes=%s",
                spec.name,
                next_version,
                changes,
            )
            return RegisterOutcome(
                asset=spec.name,
                action="evolved",
                schema_version=next_version,
                changed_fields=changes,
                reason=f"兼容变更 {dict(changes)}，schema_version 推进到 {next_version}",
            )

    def get_asset(self, name: str) -> AssetSnapshot:
        snapshot = self.store.get_asset(name)
        if snapshot is None:
            logger.info("get_asset 判定: not_found asset=%s", name)
            raise AssetNotFoundError(f"资产不存在: {name}")
        return snapshot

    def list_assets(self) -> list[AssetSnapshot]:
        return self.store.list_assets()

    # ---- 血缘 ----
    def _check_and_add_edge(self, edge: LineageEdge) -> EdgeCheckResult:
        """在临界区内校验并尝试写入单条边，返回明确结论。

        校验顺序固定，结论 code 可区分：
        validation_error / asset_not_found / duplicate_edge /
        lineage_cycle / accepted。
        """
        upstream, downstream = edge.upstream, edge.downstream
        if not upstream or not downstream:
            return EdgeCheckResult(
                edge=edge,
                accepted=False,
                code="validation_error",
                reason="血缘上下游名称不能为空",
            )
        if upstream == downstream:
            return EdgeCheckResult(
                edge=edge,
                accepted=False,
                code="lineage_cycle",
                reason=f"资产不能指向自身形成自环: {upstream}",
            )
        if self.store.get_asset(upstream) is None:
            return EdgeCheckResult(
                edge=edge,
                accepted=False,
                code="asset_not_found",
                reason=f"上游资产不存在: {upstream}",
            )
        if self.store.get_asset(downstream) is None:
            return EdgeCheckResult(
                edge=edge,
                accepted=False,
                code="asset_not_found",
                reason=f"下游资产不存在: {downstream}",
            )
        if self.store.has_edge(upstream, downstream):
            return EdgeCheckResult(
                edge=edge,
                accepted=False,
                code="duplicate_edge",
                reason=f"血缘边已存在: {upstream} -> {downstream}，重复上报幂等跳过",
            )

        cycle = _find_cycle_if_added(self.store, upstream, downstream)
        if cycle is not None:
            return EdgeCheckResult(
                edge=edge,
                accepted=False,
                code="lineage_cycle",
                reason="新增边会形成环: " + " -> ".join(cycle),
            )

        self.store.add_edge(edge)
        return EdgeCheckResult(
            edge=edge,
            accepted=True,
            code="accepted",
            reason=f"血缘边已登记: {upstream} -> {downstream}",
        )

    def add_lineage(self, edge: LineageEdge) -> EdgeCheckResult:
        """校验并写入单条血缘边；任何拒绝场景都不抛异常，返回结构化结论。

        若调用方更希望以异常形式处理，可检查结果的 ``accepted`` 与 ``code``。
        """
        logger.info(
            "add_lineage 输入: %s -> %s (%s)",
            edge.upstream,
            edge.downstream,
            edge.edge_type,
        )
        with self.store.transaction():
            result = self._check_and_add_edge(edge)
        logger.info(
            "add_lineage 判定: accepted=%s code=%s reason=%s",
            result.accepted,
            result.code,
            result.reason,
        )
        return result

    def add_lineage_batch(self, edges: list[LineageEdge]) -> list[EdgeCheckResult]:
        """顺序登记多条边。

        语义：按输入顺序在同一临界区依次"校验 + 写入"，先通过的边对
        后续判定立即可见（因此批次内成环/重复也能被识别）；被拒绝的边
        跳过但保留结论，不影响其他边。不抛异常。
        """
        logger.info(
            "add_lineage_batch 输入: %s",
            [f"{e.upstream}->{e.downstream}" for e in edges],
        )
        results: list[EdgeCheckResult] = []
        with self.store.transaction():
            for edge in edges:
                results.append(self._check_and_add_edge(edge))
        for r in results:
            logger.info(
                "add_lineage_batch 判定: %s->%s accepted=%s code=%s reason=%s",
                r.edge.upstream,
                r.edge.downstream,
                r.accepted,
                r.code,
                r.reason,
            )
        return results

    def impact(self, asset: str) -> ImpactResult:
        """返回资产的直接与传递上下游影响范围。

        上游 = 数据来源（沿边反向遍历）；下游 = 被影响资产（沿边正向遍历）。
        结果排序以保证结论可复现；资产不存在时抛 AssetNotFoundError。
        """
        logger.info("impact 输入: asset=%s", asset)
        with self.store.read_transaction():
            if self.store.get_asset(asset) is None:
                logger.info("impact 判定: not_found asset=%s", asset)
                raise AssetNotFoundError(f"资产不存在: {asset}")
            forward: dict[str, list[str]] = {}
            reverse: dict[str, list[str]] = {}
            for e in self.store.list_edges():
                forward.setdefault(e.upstream, []).append(e.downstream)
                reverse.setdefault(e.downstream, []).append(e.upstream)

            upstream_direct = sorted(reverse.get(asset, []))
            downstream_direct = sorted(forward.get(asset, []))
            upstream_transitive = _transitive(asset, reverse)
            downstream_transitive = _transitive(asset, forward)

        result = ImpactResult(
            asset=asset,
            upstream_direct=upstream_direct,
            downstream_direct=downstream_direct,
            upstream_transitive=sorted(upstream_transitive),
            downstream_transitive=sorted(downstream_transitive),
        )
        logger.info(
            "impact 判定: asset=%s up_direct=%s down_direct=%s "
            "up_transitive=%s down_transitive=%s",
            asset,
            result.upstream_direct,
            result.downstream_direct,
            result.upstream_transitive,
            result.downstream_transitive,
        )
        return result

    # ---- 标签与负责人 ----
    def update_governance(self, update: GovernanceUpdate) -> GovernanceOutcome:
        """更新资产的负责人与标签，基于治理版本号做乐观并发控制。

        * 资产不存在 -> AssetNotFoundError；
        * 版本不匹配 -> VersionConflictError（携带 expected/actual，可区分）；
        * owner 为 None 表示不修改负责人；空串非法；
        * replace_tags=True 时用 add_tags 整体替换标签；否则做增量增删；
        * 同一次请求中既增又删同一标签 -> ValidationError；
        * 无任何实际变化时判定为幂等写入，不推进版本（changed=False）。
        """
        logger.info(
            "update_governance 输入: asset=%s expected_gv=%s owner=%s "
            "add=%s remove=%s replace=%s",
            update.asset,
            update.expected_governance_version,
            update.owner,
            sorted(update.add_tags),
            sorted(update.remove_tags),
            update.replace_tags,
        )
        if not update.asset or not str(update.asset).strip():
            raise ValidationError("资产名称不能为空")
        if update.owner is not None and not str(update.owner).strip():
            raise ValidationError("负责人不能为空串；如需保持不变请省略 owner")
        overlap = set(update.add_tags) & set(update.remove_tags)
        if overlap and not update.replace_tags:
            raise ValidationError(
                f"同一标签不能在一次更新中同时增加与删除: {sorted(overlap)}"
            )
        for t in set(update.add_tags) | set(update.remove_tags):
            if not t or not t.strip():
                raise ValidationError("标签不能为空串")

        with self.store.transaction():
            snapshot = self.store.get_asset(update.asset)
            if snapshot is None:
                logger.info(
                    "update_governance 判定: not_found asset=%s", update.asset
                )
                raise AssetNotFoundError(f"资产不存在: {update.asset}")

            if update.expected_governance_version != snapshot.governance_version:
                logger.info(
                    "update_governance 判定: version_conflict asset=%s expected=%s "
                    "actual=%s",
                    update.asset,
                    update.expected_governance_version,
                    snapshot.governance_version,
                )
                raise VersionConflictError(
                    f"治理信息版本过期: 期望 "
                    f"{update.expected_governance_version}, "
                    f"实际 {snapshot.governance_version}",
                    expected=update.expected_governance_version,
                    actual=snapshot.governance_version,
                )

            old_owner = snapshot.owner
            old_tags = set(snapshot.tags)

            new_owner = update.owner if update.owner is not None else old_owner
            if update.replace_tags:
                new_tags = set(update.add_tags)
            else:
                new_tags = (old_tags | set(update.add_tags)) - set(
                    update.remove_tags
                )

            owner_changed = new_owner != old_owner
            tags_changed = new_tags != old_tags
            changed = owner_changed or tags_changed

            if changed:
                snapshot.owner = new_owner
                snapshot.tags = new_tags
                snapshot.governance_version += 1
                snapshot.updated_at = now_ts()
                self.store.put_asset(snapshot)

            result = GovernanceOutcome(
                asset=update.asset,
                governance_version=snapshot.governance_version,
                owner=snapshot.owner,
                tags=sorted(snapshot.tags),
                changed=changed,
                reason=(
                    f"治理信息已更新，governance_version="
                    f"{snapshot.governance_version}"
                    if changed
                    else "与当前治理信息一致，幂等写入，版本不变"
                ),
            )
            logger.info(
                "update_governance 判定: asset=%s changed=%s gv=%s owner=%s tags=%s",
                update.asset,
                changed,
                result.governance_version,
                result.owner,
                result.tags,
            )
            return result
