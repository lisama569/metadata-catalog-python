"""资产结构的登记、幂等判定与兼容性演进。

兼容规则(判定依据, 也同步写进 docs/design.md)
-------------------------------------------------
1. 新增字段                -> 兼容, 记录 added。
2. 删除字段                -> 兼容, 旧字段从结构中消失即视为删除,
                              记录 removed(显式体现, 不静默保留/覆盖)。
3. 重命名字段              -> 兼容, 但必须在 ``renames`` 显式声明,
                              记录 renamed; 字段 uid 在重命名间保持不变。
4. 改变字段数据类型        -> 不兼容 FIELD_TYPE_CHANGED, 拒绝, 结构保持原状。
5. 疑似“删除旧名 + 新增同类型新名”而未声明 rename
                          -> 不兼容 FIELD_MISSING_RENAME, 拒绝, 强制显式声明,
                              避免把重命名误判成两个无关操作。
6. 入参字段列表内部重名    -> 不兼容 DUPLICATE_FIELD_IN_PAYLOAD。
7. rename 声明本身有问题   -> RENAME_SOURCE_NOT_FOUND / RENAME_TARGET_CONFLICT /
                              RENAME_TYPE_CHANGED, 分别拒绝。
8. 结构与元信息完全一致的重复上报 -> IDEMPOTENT, 不产生新版本, 不改 updated_at。
"""

from __future__ import annotations

import logging

from .errors import (
    IncompatibleReason,
    SchemaIncompatibleError,
    VersionConflictError,
)
from .models import (
    Asset,
    AssetRegistration,
    FieldChange,
    FieldDef,
    RegistrationOutcome,
    RegistrationResult,
)

logger = logging.getLogger("metadata_catalog.schema")


def register_asset(data, req: AssetRegistration, *, now: float) -> RegistrationResult:
    """在已持锁的数据快照上登记 / 演进一个资产。

    - expected_schema_version 为 None: upsert 语义, 重复上报幂等, 变更走兼容校验;
    - expected_schema_version 为整数: CAS 语义, 与当前版本不一致直接拒绝。
    """
    logger.info(
        "register_asset 输入 asset_id=%s fields=%s renames=%s expected_ver=%s",
        req.asset_id,
        [(f.name, f.data_type) for f in req.fields],
        [(r.from_name, r.to_name) for r in req.renames],
        req.expected_schema_version,
    )

    try:
        new_fields = _normalise_fields(req)
        existing = data.assets.get(req.asset_id)
        if existing is None:
            return _create(data, req, new_fields, now)
        return _evolve(data, existing, req, new_fields, now)
    except (SchemaIncompatibleError, VersionConflictError) as exc:
        # 拒绝也必须把输入与判定依据打印出来, 便于解释与排障。
        logger.warning("登记被拒绝 asset_id=%s 判定=%s 依据=%s", req.asset_id,
                       getattr(exc, "error_code", exc.__class__.__name__), exc.message)
        raise


# ---- 新建 ---------------------------------------------------------------
def _create(data, req, new_fields: list[FieldDef], now: float) -> RegistrationResult:
    # CAS: 显式要求资产已存在于某版本, 但它并不存在。
    if req.expected_schema_version is not None:
        raise VersionConflictError(
            req.asset_id,
            expected=req.expected_schema_version,
            actual=0,
            scope="schema",
        )

    fields = [
        f.model_copy(update={"uid": f.uid or f.name}) for f in new_fields
    ]
    asset = Asset(
        asset_id=req.asset_id,
        name=req.name or req.asset_id,
        asset_type=req.asset_type,
        fields=fields,
        schema_version=1,
        meta_version=0,
        created_at=now,
        updated_at=now,
    )
    data.assets[req.asset_id] = asset
    changes = [
        FieldChange(
            kind="added", field_uid=f.uid, to_name=f.name, data_type=f.data_type
        )
        for f in fields
    ]
    result = RegistrationResult(
        asset=asset,
        outcome=RegistrationOutcome.CREATED,
        schema_version=1,
        changes=changes,
        reason="资产不存在, 首次登记, schema_version=1",
    )
    logger.info("判定 CREATED asset_id=%s fields=%d", req.asset_id, len(fields))
    return result


# ---- 重复上报 / 演进 ----------------------------------------------------
def _evolve(
    data,
    existing: Asset,
    req: AssetRegistration,
    new_fields: list[FieldDef],
    now: float,
) -> RegistrationResult:
    # CAS: 携带了过期(或超前)的结构版本。
    if (
        req.expected_schema_version is not None
        and req.expected_schema_version != existing.schema_version
    ):
        raise VersionConflictError(
            req.asset_id,
            expected=req.expected_schema_version,
            actual=existing.schema_version,
            scope="schema",
        )

    rename_map = _validate_renames(existing, new_fields, req)

    old_by_name = {f.name: f for f in existing.fields}
    new_by_name = {f.name: f for f in new_fields}

    removed_names = [
        f.name for f in existing.fields if f.name not in new_by_name
    ]
    # “新增”候选: 新结构里出现、且既不是旧字段同名、也不是某个 rename 目标。
    rename_targets = set(rename_map.values())
    added_candidates = {
        n for n in new_by_name if n not in old_by_name and n not in rename_targets
    }

    # 规则 5: 存在被删字段, 同时出现同类型的“新增”字段, 又没声明 rename ->
    # 极可能是漏报的重命名, 拒绝以避免静默地“删一个 + 加一个”。
    for removed_name in removed_names:
        removed_type = old_by_name[removed_name].data_type
        ambiguous = [
            n for n in added_candidates if new_by_name[n].data_type == removed_type
        ]
        if ambiguous:
            raise SchemaIncompatibleError(
                IncompatibleReason.FIELD_MISSING_RENAME,
                (
                    f"字段 {removed_name} 消失且出现同类型新字段 {ambiguous}, "
                    "但未在 renames 中声明重命名; 若确为重命名请显式声明, "
                    "否则请确认是否应删除该字段"
                ),
                fields=[removed_name, *ambiguous],
            )

    # 建立“新字段名 -> 旧字段身份”映射: 默认按同名, rename 目标按声明指向源字段。
    identity: dict[str, FieldDef] = {}
    for name, old in old_by_name.items():
        if name in new_by_name:
            identity[name] = old
    for src, dst in rename_map.items():
        identity[dst] = old_by_name[src]

    changes: list[FieldChange] = []
    evolved_fields: list[FieldDef] = []
    matched_uids: set[str] = set()

    # 按本次上报顺序单遍装配, 字段顺序以最新上报为准。
    for nf in new_fields:
        old = identity.get(nf.name)
        if old is not None:
            matched_uids.add(old.uid)
            if old.data_type != nf.data_type:
                is_rename = nf.name in rename_map.values()
                reason = (
                    IncompatibleReason.RENAME_TYPE_CHANGED
                    if is_rename
                    else IncompatibleReason.FIELD_TYPE_CHANGED
                )
                involved = [old.name, nf.name] if is_rename else [nf.name]
                raise SchemaIncompatibleError(
                    reason,
                    f"字段 {old.name} -> {nf.name} 类型不可变更: "
                    f"{old.data_type} -> {nf.data_type}",
                    fields=involved,
                )
            evolved_fields.append(
                FieldDef(
                    uid=old.uid,
                    name=nf.name,
                    data_type=nf.data_type,
                    nullable=nf.nullable,
                    description=nf.description,
                )
            )
            if old.name != nf.name:
                changes.append(
                    FieldChange(
                        kind="renamed",
                        field_uid=old.uid,
                        from_name=old.name,
                        to_name=nf.name,
                        data_type=nf.data_type,
                    )
                )
            else:
                changes.append(
                    FieldChange(
                        kind="unchanged",
                        field_uid=old.uid,
                        to_name=nf.name,
                        data_type=nf.data_type,
                    )
                )
        else:
            uid = nf.uid or nf.name
            if any(f.uid == uid for f in existing.fields):
                raise SchemaIncompatibleError(
                    IncompatibleReason.NAME_COLLISION,
                    f"新增字段的 uid 与已有字段冲突: {uid}",
                    fields=[nf.name],
                )
            evolved_fields.append(nf.model_copy(update={"uid": uid}))
            changes.append(
                FieldChange(
                    kind="added",
                    field_uid=uid,
                    to_name=nf.name,
                    data_type=nf.data_type,
                )
            )

    # 未被任何新字段匹配上的旧身份 = 被删除字段。
    for old in existing.fields:
        if old.uid not in matched_uids:
            changes.append(
                FieldChange(
                    kind="removed",
                    field_uid=old.uid,
                    from_name=old.name,
                    data_type=old.data_type,
                )
            )

    structural_changes = [c for c in changes if c.kind in {"added", "removed", "renamed"}]
    # 基于稳定身份 uid 比较, 且要求顺序与字段属性逐位一致, 才算“完全重复”。
    existing_sig = [
        (f.uid, f.name, f.data_type, f.nullable, f.description)
        for f in existing.fields
    ]
    evolved_sig = [
        (f.uid, f.name, f.data_type, f.nullable, f.description)
        for f in evolved_fields
    ]
    same_shape = not structural_changes and existing_sig == evolved_sig

    if same_shape:
        result = RegistrationResult(
            asset=existing,
            outcome=RegistrationOutcome.IDEMPOTENT,
            schema_version=existing.schema_version,
            changes=[],
            reason="上报结构与已登记结构完全一致, 判定为重复上报(幂等), 不提升版本",
        )
        logger.info(
            "判定 IDEMPOTENT(重复上报,幂等) asset_id=%s schema_version=%s",
            req.asset_id,
            existing.schema_version,
        )
        return result

    evolved = existing.model_copy(
        update={
            "fields": evolved_fields,
            "schema_version": existing.schema_version + 1,
            "updated_at": now,
        }
    )
    if req.name:
        evolved.name = req.name
    evolved.asset_type = req.asset_type or existing.asset_type
    data.assets[req.asset_id] = evolved

    kinds = sorted({c.kind for c in structural_changes})
    result = RegistrationResult(
        asset=evolved,
        outcome=RegistrationOutcome.EVOLVED,
        schema_version=evolved.schema_version,
        changes=changes,
        reason=(
            "兼容演进, 变更类型=" + ",".join(kinds)
            + f", schema_version {existing.schema_version} -> {evolved.schema_version}"
        ),
    )
    logger.info(
        "判定 EVOLVED asset_id=%s changes=%s new_version=%s",
        req.asset_id,
        [(c.kind, c.from_name, c.to_name) for c in structural_changes],
        evolved.schema_version,
    )
    return result


# ---- 入参与 rename 校验 -------------------------------------------------
def _normalise_fields(req: AssetRegistration) -> list[FieldDef]:
    seen_names: set[str] = set()
    seen_uids: set[str] = set()
    out: list[FieldDef] = []
    for f in req.fields:
        name = f.name.strip()
        if not name:
            raise SchemaIncompatibleError(
                IncompatibleReason.DUPLICATE_FIELD_IN_PAYLOAD,
                "字段名不能为空",
                fields=[],
            )
        if name in seen_names:
            raise SchemaIncompatibleError(
                IncompatibleReason.DUPLICATE_FIELD_IN_PAYLOAD,
                f"上报字段列表中字段名重复: {name}",
                fields=[name],
            )
        seen_names.add(name)
        uid = f.uid.strip() or name
        if uid in seen_uids:
            raise SchemaIncompatibleError(
                IncompatibleReason.NAME_COLLISION,
                f"上报字段列表中 uid 重复: {uid}",
                fields=[name],
            )
        seen_uids.add(uid)
        out.append(f.model_copy(update={"name": name, "uid": uid}))
    return out


def _validate_renames(
    existing: Asset, new_fields: list[FieldDef], req: AssetRegistration
) -> dict[str, str]:
    """返回 {from_name: to_name}, 并对 rename 声明本身做严格校验。"""
    old_names = {f.name for f in existing.fields}
    new_names = {f.name for f in new_fields}
    new_by_name = {f.name: f for f in new_fields}
    old_by_name = {f.name: f for f in existing.fields}

    rename_map: dict[str, str] = {}
    for op in req.renames:
        src = op.from_name.strip()
        dst = op.to_name.strip()
        if src not in old_names:
            raise SchemaIncompatibleError(
                IncompatibleReason.RENAME_SOURCE_NOT_FOUND,
                f"rename 源字段在当前结构中不存在: {src}",
                fields=[src],
            )
        if dst not in new_names:
            raise SchemaIncompatibleError(
                IncompatibleReason.RENAME_TARGET_CONFLICT,
                f"rename 目标字段未出现在新结构中: {dst}",
                fields=[dst],
            )
        if src in rename_map:
            raise SchemaIncompatibleError(
                IncompatibleReason.RENAME_TARGET_CONFLICT,
                f"字段 {src} 被声明了多次重命名",
                fields=[src],
            )
        if dst in rename_map.values():
            raise SchemaIncompatibleError(
                IncompatibleReason.RENAME_TARGET_CONFLICT,
                f"多个字段重命名到同一目标名: {dst}",
                fields=[dst],
            )
        if old_by_name[src].data_type != new_by_name[dst].data_type:
            raise SchemaIncompatibleError(
                IncompatibleReason.RENAME_TYPE_CHANGED,
                f"重命名 {src} -> {dst} 不允许同时改变类型: "
                f"{old_by_name[src].data_type} -> {new_by_name[dst].data_type}",
                fields=[src, dst],
            )
        rename_map[src] = dst
    return rename_map
