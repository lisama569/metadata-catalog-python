"""结构登记、幂等与兼容性演进测试。"""

from __future__ import annotations

import pytest

from metadata_catalog.errors import (
    IncompatibleReason,
    SchemaIncompatibleError,
    VersionConflictError,
)
from metadata_catalog.models import (
    AssetRegistration,
    FieldDef,
    RegistrationOutcome,
    RenameOp,
)

from .conftest import assert_log_contains


def F(name: str, data_type: str, *, uid: str = "", nullable: bool = True) -> FieldDef:
    return FieldDef(name=name, data_type=data_type, uid=uid, nullable=nullable)


def reg(asset_id="t1", fields=None, renames=None, expected=None) -> AssetRegistration:
    return AssetRegistration(
        asset_id=asset_id,
        fields=fields if fields is not None else [],
        renames=renames or [],
        expected_schema_version=expected,
    )


# ---- 登记与幂等 ---------------------------------------------------------
def test_first_registration_creates_v1(service, caplog):
    res = service.register_asset(reg(fields=[F("id", "int"), F("name", "string")]))
    assert res.outcome is RegistrationOutcome.CREATED
    assert res.schema_version == 1
    assert {c.kind for c in res.changes} == {"added"}
    assert_log_contains(caplog, "register_asset 输入", "判定 CREATED")


def test_duplicate_report_is_idempotent(service, caplog):
    payload = [F("id", "int"), F("name", "string")]
    first = service.register_asset(reg(fields=payload))
    second = service.register_asset(reg(fields=payload))
    assert first.schema_version == second.schema_version == 1
    assert second.outcome is RegistrationOutcome.IDEMPOTENT
    assert second.changes == []
    # 重复上报不得改动资产对象
    assert service.get_asset("t1").schema_version == 1
    assert_log_contains(caplog, "判定 IDEMPOTENT", "幂等")


def test_idempotent_after_rename_then_same_shape(service):
    service.register_asset(reg(fields=[F("id", "int"), F("name", "string")]))
    service.register_asset(
        reg(
            fields=[F("id", "int"), F("full_name", "string")],
            renames=[RenameOp(from_name="name", to_name="full_name")],
        )
    )
    again = service.register_asset(
        reg(fields=[F("id", "int"), F("full_name", "string")])
    )
    assert again.outcome is RegistrationOutcome.IDEMPOTENT
    assert again.schema_version == 2


# ---- 兼容演进 -----------------------------------------------------------
def test_add_field_evolves(service):
    service.register_asset(reg(fields=[F("id", "int")]))
    res = service.register_asset(reg(fields=[F("id", "int"), F("email", "string")]))
    assert res.outcome is RegistrationOutcome.EVOLVED
    assert res.schema_version == 2
    added = [c for c in res.changes if c.kind == "added"]
    assert [c.to_name for c in added] == ["email"]


def test_remove_field_evolves_and_is_recorded(service, caplog):
    service.register_asset(reg(fields=[F("id", "int"), F("tmp", "string")]))
    res = service.register_asset(reg(fields=[F("id", "int")]))
    assert res.outcome is RegistrationOutcome.EVOLVED
    removed = [c for c in res.changes if c.kind == "removed"]
    assert [c.from_name for c in removed] == ["tmp"]
    assert_log_contains(caplog, "判定 EVOLVED", "removed")


def test_rename_keeps_stable_uid(service, caplog):
    service.register_asset(
        reg(fields=[F("id", "int", uid="id"), F("name", "string", uid="name")])
    )
    res = service.register_asset(
        reg(
            fields=[F("id", "int"), F("full_name", "string")],
            renames=[RenameOp(from_name="name", to_name="full_name")],
        )
    )
    assert res.outcome is RegistrationOutcome.EVOLVED
    asset = service.get_asset("t1")
    renamed = [f for f in asset.fields if f.name == "full_name"]
    assert renamed and renamed[0].uid == "name"  # 身份稳定
    assert_log_contains(caplog, "renamed", "full_name")


def test_evolution_appends_schema_version_monotonically(service):
    fields = [F("id", "int")]
    service.register_asset(reg(fields=fields))
    for i in range(3):
        fields = fields + [F(f"c{i}", "int")]
        res = service.register_asset(reg(fields=fields))
        assert res.schema_version == i + 2
    assert service.get_asset("t1").schema_version == 4


# ---- 不兼容变更(原因可区分) --------------------------------------------
def test_type_change_rejected_with_distinct_reason(service, caplog):
    service.register_asset(reg(fields=[F("id", "int")]))
    with pytest.raises(SchemaIncompatibleError) as exc:
        service.register_asset(reg(fields=[F("id", "bigint")]))
    assert exc.value.reason is IncompatibleReason.FIELD_TYPE_CHANGED
    assert exc.value.fields == ["id"]
    # 被拒绝后旧结构与版本保持不变, 未被静默覆盖
    asset = service.get_asset("t1")
    assert asset.fields[0].data_type == "int"
    assert asset.schema_version == 1
    assert_log_contains(caplog, "类型不可变更")


def test_rename_without_declaration_rejected(service):
    service.register_asset(reg(fields=[F("id", "int"), F("name", "string")]))
    # name 消失 + 同类型新名 title, 未声明 rename
    with pytest.raises(SchemaIncompatibleError) as exc:
        service.register_asset(reg(fields=[F("id", "int"), F("title", "string")]))
    assert exc.value.reason is IncompatibleReason.FIELD_MISSING_RENAME
    assert set(exc.value.fields) == {"name", "title"}
    # 旧结构不变
    assert [f.name for f in service.get_asset("t1").fields] == ["id", "name"]


def test_rename_with_type_change_rejected(service):
    service.register_asset(reg(fields=[F("id", "int"), F("name", "string")]))
    with pytest.raises(SchemaIncompatibleError) as exc:
        service.register_asset(
            reg(
                fields=[F("id", "int"), F("full_name", "text")],
                renames=[RenameOp(from_name="name", to_name="full_name")],
            )
        )
    assert exc.value.reason is IncompatibleReason.RENAME_TYPE_CHANGED


def test_rename_source_not_found_rejected(service):
    service.register_asset(reg(fields=[F("id", "int")]))
    with pytest.raises(SchemaIncompatibleError) as exc:
        service.register_asset(
            reg(
                fields=[F("id", "int"), F("k2", "int")],
                renames=[RenameOp(from_name="ghost", to_name="k2")],
            )
        )
    assert exc.value.reason is IncompatibleReason.RENAME_SOURCE_NOT_FOUND


def test_rename_target_conflict_rejected(service):
    service.register_asset(reg(fields=[F("id", "int"), F("a", "int"), F("b", "int")]))
    with pytest.raises(SchemaIncompatibleError) as exc:
        service.register_asset(
            reg(
                fields=[F("id", "int"), F("z", "int")],
                renames=[
                    RenameOp(from_name="a", to_name="z"),
                    RenameOp(from_name="b", to_name="z"),
                ],
            )
        )
    assert exc.value.reason is IncompatibleReason.RENAME_TARGET_CONFLICT


def test_duplicate_field_in_payload_rejected(service):
    with pytest.raises(SchemaIncompatibleError) as exc:
        service.register_asset(reg(fields=[F("id", "int"), F("id", "string")]))
    assert exc.value.reason is IncompatibleReason.DUPLICATE_FIELD_IN_PAYLOAD


def test_rejection_does_not_partially_mutate(service):
    service.register_asset(reg(fields=[F("id", "int"), F("name", "string")]))
    with pytest.raises(SchemaIncompatibleError):
        service.register_asset(
            reg(fields=[F("id", "int"), F("name", "string"), F("id", "int")])
        )
    asset = service.get_asset("t1")
    assert [f.name for f in asset.fields] == ["id", "name"]
    assert asset.schema_version == 1


# ---- 结构版本 CAS -------------------------------------------------------
def test_cas_stale_schema_version_rejected(service):
    service.register_asset(reg(fields=[F("id", "int")]))
    service.register_asset(reg(fields=[F("id", "int"), F("b", "int")]))  # v2
    with pytest.raises(VersionConflictError) as exc:
        service.register_asset(
            reg(fields=[F("id", "int"), F("b", "int"), F("c", "int")], expected=1)
        )
    err = exc.value
    assert err.expected_version == 1 and err.actual_version == 2
    assert err.scope == "schema"


def test_cas_correct_schema_version_accepted(service):
    service.register_asset(reg(fields=[F("id", "int")]))
    res = service.register_asset(
        reg(fields=[F("id", "int"), F("b", "int")], expected=1)
    )
    assert res.outcome is RegistrationOutcome.EVOLVED
    assert res.schema_version == 2
