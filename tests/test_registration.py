"""资产登记、幂等与结构演进兼容性测试。"""

from __future__ import annotations

import logging

import pytest

from metadata_catalog.errors import (
    IncompatibleSchemaError,
    ValidationError,
    VersionConflictError,
)
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    RegisterRequest,
)

logger = logging.getLogger("metadata_catalog.tests")


def table(name: str, fields: list[FieldSpec], **kw) -> AssetSpec:
    return AssetSpec(name=name, asset_type=AssetType.TABLE, fields=fields, **kw)


def F(name: str, ftype: FieldType = FieldType.STRING, nullable: bool = True) -> FieldSpec:
    return FieldSpec(name=name, field_type=ftype, nullable=nullable)


def test_first_register_creates_version_1(catalog):
    logger.info("用例输入: 首次登记 t1 字段 id(long,not null), name(string)")
    outcome = catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False), F("name")]))
    )
    logger.info("判定依据: action=%s schema_version=%s", outcome.action, outcome.schema_version)
    assert outcome.action == "created"
    assert outcome.schema_version == 1
    snap = catalog.get_asset("t1")
    assert set(snap.fields) == {"id", "name"}
    assert snap.fields["id"].since_version == 1


def test_duplicate_register_is_idempotent(catalog):
    fields = [F("id", FieldType.LONG, False), F("name")]
    catalog.register(RegisterRequest(spec=table("t1", fields)))
    logger.info("用例输入: 完全相同结构重复上报第二次")
    outcome = catalog.register(RegisterRequest(spec=table("t1", list(fields))))
    logger.info("判定依据: action=%s schema_version=%s reason=%s",
                outcome.action, outcome.schema_version, outcome.reason)
    assert outcome.action == "re_registered_idempotent"
    assert outcome.schema_version == 1  # 不产生新版本
    assert outcome.changed_fields == {}


def test_compatible_evolution_add_and_remove(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False), F("name")]))
    )
    logger.info("用例输入: 删除 name，新增 age(integer)")
    outcome = catalog.register(
        RegisterRequest(
            spec=table(
                "t1",
                [F("id", FieldType.LONG, False), F("age", FieldType.INTEGER)],
            )
        )
    )
    logger.info("判定依据: action=%s changes=%s", outcome.action, outcome.changed_fields)
    assert outcome.action == "evolved"
    assert outcome.schema_version == 2
    assert outcome.changed_fields == {"name": "removed", "age": "added"}
    snap = catalog.get_asset("t1")
    assert snap.fields["id"].since_version == 1
    assert snap.fields["age"].since_version == 2


def test_rename_is_recognized_via_explicit_declaration(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False), F("name")]))
    )
    logger.info("用例输入: renames={name: full_name}，类型保持 string")
    outcome = catalog.register(
        RegisterRequest(
            spec=table("t1", [F("id", FieldType.LONG, False), F("full_name")]),
            renames={"name": "full_name"},
        )
    )
    logger.info("判定依据: changes=%s", outcome.changed_fields)
    assert outcome.action == "evolved"
    assert outcome.changed_fields == {"full_name": "renamed_from:name"}
    assert "name" not in catalog.get_asset("t1").fields


def test_rename_without_declaration_is_delete_plus_add(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False), F("name")]))
    )
    logger.info("用例输入: 未声明 renames，name 消失/full_name 出现")
    outcome = catalog.register(
        RegisterRequest(
            spec=table("t1", [F("id", FieldType.LONG, False), F("full_name")])
        )
    )
    # 删除+新增本身仍是兼容演进，但字段被视为全新字段（since_version 推进）
    logger.info("判定依据: action=%s changes=%s", outcome.action, outcome.changed_fields)
    assert outcome.changed_fields == {"name": "removed", "full_name": "added"}
    assert catalog.get_asset("t1").fields["full_name"].since_version == 2


def test_type_change_is_rejected_with_distinct_reason(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False)]))
    )
    logger.info("用例输入: id 类型 long -> string")
    with pytest.raises(IncompatibleSchemaError) as exc:
        catalog.register(
            RegisterRequest(spec=table("t1", [F("id", FieldType.STRING, False)]))
        )
    logger.info("判定依据: code=%s reasons=%s", exc.value.code, exc.value.reasons)
    assert exc.value.code == "incompatible_schema"
    assert any("字段类型变更不兼容" in r for r in exc.value.reasons)
    # 被拒绝后结构不得被静默覆盖
    assert (
        catalog.get_asset("t1").fields["id"].spec.field_type == FieldType.LONG
    )
    assert catalog.get_asset("t1").schema_version == 1


def test_nullable_tightening_is_rejected(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("name", FieldType.STRING, True)]))
    )
    logger.info("用例输入: name nullable true -> false")
    with pytest.raises(IncompatibleSchemaError) as exc:
        catalog.register(
            RegisterRequest(spec=table("t1", [F("name", FieldType.STRING, False)]))
        )
    logger.info("判定依据: code=%s reasons=%s", exc.value.code, exc.value.reasons)
    assert any("非空" in r for r in exc.value.reasons)
    assert catalog.get_asset("t1").fields["name"].spec.nullable is True


def test_nullable_relaxation_is_compatible(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False)]))
    )
    logger.info("用例输入: id nullable false -> true（放宽）")
    outcome = catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, True)]))
    )
    logger.info("判定依据: action=%s v=%s", outcome.action, outcome.schema_version)
    assert outcome.action == "evolved"
    assert catalog.get_asset("t1").fields["id"].spec.nullable is True


def test_rename_with_type_change_is_rejected(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", [F("id", FieldType.LONG, False), F("name")]))
    )
    logger.info("用例输入: rename name->full_name 同时改类型为 integer")
    with pytest.raises(IncompatibleSchemaError) as exc:
        catalog.register(
            RegisterRequest(
                spec=table(
                    "t1",
                    [F("id", FieldType.LONG, False), F("full_name", FieldType.INTEGER)],
                ),
                renames={"name": "full_name"},
            )
        )
    logger.info("判定依据: reasons=%s", exc.value.reasons)
    assert any("重命名同时变更类型" in r for r in exc.value.reasons)
    assert "name" in catalog.get_asset("t1").fields


def test_asset_type_change_is_rejected(catalog):
    catalog.register(
        RegisterRequest(spec=table("t1", []))
    )
    logger.info("用例输入: asset_type table -> topic")
    with pytest.raises(IncompatibleSchemaError) as exc:
        catalog.register(
            RegisterRequest(
                spec=AssetSpec(name="t1", asset_type=AssetType.TOPIC, fields=[])
            )
        )
    logger.info("判定依据: reasons=%s", exc.value.reasons)
    assert any("资产种类" in r for r in exc.value.reasons)


def test_stale_expected_version_conflict(catalog):
    catalog.register(RegisterRequest(spec=table("t1", [F("id", FieldType.LONG)])))
    catalog.register(RegisterRequest(spec=table("t1", [F("id", FieldType.LONG), F("x")])))
    logger.info("用例输入: 基于过期版本 expected=1 在实际版本 2 上写入")
    with pytest.raises(VersionConflictError) as exc:
        catalog.register(
            RegisterRequest(
                spec=table("t1", [F("id", FieldType.LONG), F("y")]),
                expected_schema_version=1,
            )
        )
    logger.info("判定依据: code=%s expected=%s actual=%s",
                exc.value.code, exc.value.expected_version, exc.value.actual_version)
    assert exc.value.expected_version == 1
    assert exc.value.actual_version == 2
    assert "x" in catalog.get_asset("t1").fields
    assert "y" not in catalog.get_asset("t1").fields


def test_invalid_inputs(catalog):
    logger.info("用例输入: 空资产名 / 重复字段名 / 非法 rename 源")
    with pytest.raises(ValidationError):
        catalog.register(RegisterRequest(spec=table("  ", [])))
    with pytest.raises(ValidationError):
        catalog.register(
            RegisterRequest(spec=table("t1", [F("a"), F("a")]))
        )
    catalog.register(RegisterRequest(spec=table("t2", [F("a"), F("b")])))
    with pytest.raises(ValidationError):
        # a 与 b 不能在同一次演进里重命名为同一个目标
        catalog.register(
            RegisterRequest(
                spec=table("t2", [F("c")]),
                renames={"a": "c", "b": "c"},
            )
        )
    with pytest.raises(ValidationError):
        # 重命名源字段不存在
        catalog.register(
            RegisterRequest(
                spec=table("t2", [F("a"), F("b"), F("z")]),
                renames={"ghost": "z"},
            )
        )
