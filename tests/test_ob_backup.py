#!/usr/bin/env python3
"""Тесты профиля S3 и SQL бэкапа/архива (без кластера)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location("ob_backup", ROOT / "scripts" / "lib" / "ob_backup.py")
ob_backup = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ob_backup)


def full_s3_cfg(**overrides: object) -> dict:
    s3 = {
        "host": "storage.yandexcloud.net",
        "bucket": "ob-backups",
        "access_id": "YCAJexampleid",
        "access_key": "secret_key-1",
        "addressing_model": "path_style",
        "checksum_type": "md5",
        "delete_mode": "delete",
        "data_prefix": "backup/{tenant}/data",
        "archive_prefix": "backup/{tenant}/archive",
    }
    s3.update({k: v for k, v in overrides.items() if k != "tenant" and k != "archive"})
    cfg: dict = {
        "tenant": {"tenant_name": "tpcc"},
        "backup": {"s3": s3, "archive": {"binding": "Optional", "piece_switch_interval": "1d"}},
    }
    if "tenant" in overrides:
        cfg["backup"]["tenant"] = overrides["tenant"]
    if "archive" in overrides:
        cfg["backup"]["archive"] = overrides["archive"]
    return cfg


def test_missing_s3_lists_all_fields() -> None:
    cfg = {"tenant": {"tenant_name": "tpcc"}, "backup": {"s3": {}}}
    missing = ob_backup.missing_s3_fields(cfg, {})
    assert missing == ["host", "bucket", "access_id", "access_key"]
    try:
        ob_backup.require_s3_profile(cfg, {})
        raise AssertionError("ждали BackupConfigError")
    except ob_backup.BackupConfigError as exc:
        msg = str(exc)
        assert "host" in msg and "bucket" in msg
        assert "access_id" in msg and "access_key" in msg
        assert "backup.s3" in msg
        assert "OB_BACKUP_S3_ACCESS_KEY" in msg


def test_env_fills_keys() -> None:
    cfg = full_s3_cfg()
    cfg["backup"]["s3"]["access_id"] = ""
    cfg["backup"]["s3"]["access_key"] = ""
    env = {
        "OB_BACKUP_S3_ACCESS_ID": "fromenvid",
        "OB_BACKUP_S3_ACCESS_KEY": "fromenvkey",
    }
    assert ob_backup.missing_s3_fields(cfg, env) == []
    s3 = ob_backup.resolve_s3(cfg, "tpcc", env)
    assert s3["access_id"] == "fromenvid"
    assert s3["access_key"] == "fromenvkey"


def test_partial_yaml_still_errors_before_sql() -> None:
    cfg = full_s3_cfg()
    del cfg["backup"]["s3"]["bucket"]
    missing = ob_backup.missing_s3_fields(cfg, {})
    assert missing == ["bucket"]
    try:
        ob_backup.resolve_s3(cfg, "tpcc", {})
        raise AssertionError("ждали BackupConfigError")
    except ob_backup.BackupConfigError as exc:
        assert "bucket" in str(exc)


def test_aws_requires_region() -> None:
    cfg = full_s3_cfg(host="s3.eu-north-1.amazonaws.com")
    missing = ob_backup.missing_s3_fields(cfg, {})
    assert "s3_region" in missing
    cfg["backup"]["s3"]["s3_region"] = "eu-north-1"
    assert ob_backup.missing_s3_fields(cfg, {}) == []


def test_uri_and_sql() -> None:
    cfg = full_s3_cfg()
    tenant = ob_backup.resolve_tenant(cfg)
    assert tenant == "tpcc"
    s3 = ob_backup.resolve_s3(cfg, tenant, {})
    data = ob_backup.build_s3_uri(s3, s3["data_prefix"])
    archive = ob_backup.build_s3_uri(s3, s3["archive_prefix"])
    assert data.startswith("s3://ob-backups/backup/tpcc/data?")
    assert "host=storage.yandexcloud.net" in data
    assert "addressing_model=path_style" in data
    assert "access_key=secret_key-1" in data
    assert "backup/tpcc/archive" in archive
    redacted = ob_backup.redact_uri(data)
    assert "secret_key-1" not in redacted
    assert "access_key=***" in redacted
    dest = ob_backup.data_backup_dest_sql(data, tenant)
    assert dest.startswith("ALTER SYSTEM SET DATA_BACKUP_DEST = 's3://")
    assert dest.endswith("TENANT = tpcc")
    log_sql = ob_backup.log_archive_dest_sql(
        archive, tenant, binding="Optional", piece_switch_interval="1d"
    )
    assert "LOCATION=s3://" in log_sql
    assert "BINDING=Optional" in log_sql
    assert "PIECE_SWITCH_INTERVAL=1d" in log_sql
    assert ob_backup.archivelog_sql("tpcc") == "ALTER SYSTEM ARCHIVELOG TENANT = tpcc"
    assert ob_backup.noarchivelog_sql("tpcc") == "ALTER SYSTEM NOARCHIVELOG TENANT = tpcc"
    assert (
        ob_backup.backup_sql("tpcc", "full")
        == "ALTER SYSTEM BACKUP TENANT = tpcc"
    )
    assert (
        ob_backup.backup_sql("tpcc", "full", plus_archivelog=True)
        == "ALTER SYSTEM BACKUP TENANT = tpcc PLUS ARCHIVELOG"
    )
    assert (
        ob_backup.backup_sql("tpcc", "incremental")
        == "ALTER SYSTEM BACKUP INCREMENTAL TENANT = tpcc"
    )


def test_tenant_and_forbidden() -> None:
    assert ob_backup.resolve_tenant({"backup": {"tenant": "app1"}}) == "app1"
    try:
        ob_backup.resolve_tenant({})
        raise AssertionError("ждали ошибку тенанта")
    except ob_backup.BackupConfigError as exc:
        assert "не задан тенант" in str(exc)
    try:
        ob_backup.resolve_tenant({}, "sys")
        raise AssertionError("ждали запрет sys")
    except ob_backup.BackupConfigError:
        pass
    try:
        ob_backup.backup_sql("tpcc", "incremental", plus_archivelog=True)
        raise AssertionError("ждали запрет PLUS на incremental")
    except ob_backup.BackupConfigError:
        pass
    try:
        ob_backup.backup_sql("tpcc", "diff")
        raise AssertionError("ждали неизвестный режим")
    except ob_backup.BackupConfigError:
        pass


def test_bad_uri_chars() -> None:
    cfg = full_s3_cfg(access_key="sec ret")
    try:
        ob_backup.resolve_s3(cfg, "tpcc", {})
        raise AssertionError("ждали ошибку символов")
    except ob_backup.BackupConfigError as exc:
        assert "access_key" in str(exc)


def test_parse_status() -> None:
    rows = ob_backup.parse_status_rows("tpcc\tDOING\n")
    assert rows == [("tpcc", "DOING")]
    rows = ob_backup.parse_status_rows("101 tpcc COMPLETED\n")
    assert rows[0][1] == "COMPLETED"
    assert ob_backup.latest_status([]) == ""


def test_archive_off_does_not_need_s3() -> None:
    cfg = {"tenant": {"tenant_name": "tpcc"}}
    assert ob_backup.missing_s3_fields(cfg, {}) == [
        "host",
        "bucket",
        "access_id",
        "access_key",
    ]
    assert ob_backup.resolve_tenant(cfg) == "tpcc"


def test_cli_flags_after_subcommand() -> None:
    parser = ob_backup.build_parser()
    args = parser.parse_args(
        ["backup", "full", "--tenant", "app1", "--plus-archivelog", "--no-wait"]
    )
    assert args.command == "backup"
    assert args.mode == "full"
    assert args.tenant == "app1"
    assert args.plus_archivelog is True
    assert args.no_wait is True
    args = parser.parse_args(["archive", "off", "--tenant", "app1"])
    assert args.action == "off"
    assert args.tenant == "app1"
    args = parser.parse_args(
        [
            "restore",
            "run",
            "--dest-tenant",
            "tpcc_restore",
            "--pool",
            "restore_pool",
            "--until-time",
            "2026-09-16 12:00:00",
            "--activate",
            "--no-wait",
        ]
    )
    assert args.command == "restore"
    assert args.action == "run"
    assert args.dest_tenant == "tpcc_restore"
    assert args.pool == "restore_pool"
    assert args.until_time == "2026-09-16 12:00:00"
    assert args.activate is True
    assert args.no_wait is True
    args = parser.parse_args(["restore", "--pool", "p1"])
    assert args.action == "run"
    assert args.pool == "p1"
    before = parser.parse_args(
        ["--config", str(ROOT / "config" / "deploy.yaml.example"), "validate"]
    )
    after = parser.parse_args(
        ["validate", "--config", str(ROOT / "config" / "deploy.yaml.example")]
    )
    assert before.config.endswith("deploy.yaml.example")
    assert after.config.endswith("deploy.yaml.example")


def test_wrappers_and_deploy_sh() -> None:
    backup = (ROOT / "scripts" / "14-backup.sh").read_text(encoding="utf-8")
    archive = (ROOT / "scripts" / "15-archive-log.sh").read_text(encoding="utf-8")
    restore = (ROOT / "scripts" / "16-restore.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    example = (ROOT / "config" / "deploy.yaml.example").read_text(encoding="utf-8")
    assert "ob_backup.py" in backup and "full|incremental" in backup
    assert "ob_backup.py" in archive and "on|off" in archive
    assert "ob_backup.py" in restore and "run|show|validate" in restore
    assert "14-backup.sh" in deploy
    assert "15-archive-log.sh" in deploy
    assert "16-restore.sh" in deploy
    assert "backup:" in example
    assert "access_id:" in example
    assert "access_key:" in example
    assert "pool_list:" in example
    assert "restore:" in example


def test_restore_sql_and_plan() -> None:
    cfg = full_s3_cfg()
    cfg["backup"]["restore"] = {
        "pool_list": "restore_pool",
        "locality": "F,R{1}@z1,F,R{1}@z2,F,R{1}@z3",
        "method": "full",
    }
    plan = ob_backup.resolve_restore_plan(cfg, environ={})
    assert plan["source"] == "tpcc"
    assert plan["dest"] == "tpcc_restore"
    assert plan["pool_list"] == "restore_pool"
    assert plan["activate"] is False
    sql = plan["sql"]
    assert sql.startswith("ALTER SYSTEM RESTORE tpcc_restore FROM 's3://")
    assert "backup/tpcc/data?" in sql
    assert "backup/tpcc/archive?" in sql
    assert "WITH 'pool_list=restore_pool&locality=F,R{1}@z1,F,R{1}@z2,F,R{1}@z3&method=full'" in sql
    assert "UNTIL" not in sql
    assert "secret_key-1" in sql
    assert "secret_key-1" not in ob_backup.redact_uri(sql)
    assert (
        ob_backup.activate_standby_sql("tpcc_restore")
        == "ALTER SYSTEM ACTIVATE STANDBY TENANT tpcc_restore"
    )
    until_sql = ob_backup.restore_sql(
        "app_restore",
        "s3://b/data?access_key=k",
        "s3://b/archive?access_key=k",
        option="pool_list=p1&method=full",
        until_time="2026-09-16 12:00:00",
    )
    assert "UNTIL TIME = '2026-09-16 12:00:00'" in until_sql
    scn_sql = ob_backup.restore_sql(
        "app_restore",
        "s3://b/data?k=1",
        "s3://b/archive?k=1",
        option="pool_list=p1&method=full",
        until_scn="12345",
    )
    assert "UNTIL SCN = 12345" in scn_sql


def test_restore_fail_fast() -> None:
    cfg = full_s3_cfg()
    try:
        ob_backup.resolve_restore_plan(cfg, environ={})
        raise AssertionError("ждали ошибку pool_list")
    except ob_backup.BackupConfigError as exc:
        assert "pool_list" in str(exc)
    cfg["backup"]["restore"] = {"pool_list": "restore_pool", "dest_tenant": "tpcc"}
    try:
        ob_backup.resolve_restore_plan(cfg, environ={})
        raise AssertionError("ждали совпадение dest")
    except ob_backup.BackupConfigError as exc:
        assert "совпадает" in str(exc)
    cfg["backup"]["restore"] = {
        "pool_list": "restore_pool",
        "until_time": "2026-09-16 12:00:00",
        "until_scn": "1",
    }
    try:
        ob_backup.resolve_restore_plan(cfg, environ={})
        raise AssertionError("ждали конфликт UNTIL")
    except ob_backup.BackupConfigError as exc:
        assert "вместе" in str(exc)
    cfg["backup"]["restore"] = {
        "pool_list": "restore_pool",
        "method": "quick",
        "activate": True,
    }
    try:
        ob_backup.resolve_restore_plan(cfg, environ={})
        raise AssertionError("ждали запрет activate+quick")
    except ob_backup.BackupConfigError as exc:
        assert "quick" in str(exc)
    cfg = full_s3_cfg()
    del cfg["backup"]["s3"]["bucket"]
    cfg["backup"]["restore"] = {"pool_list": "restore_pool"}
    try:
        ob_backup.resolve_restore_plan(cfg, environ={})
        raise AssertionError("ждали ошибку S3 до SQL")
    except ob_backup.BackupConfigError as exc:
        assert "bucket" in str(exc)
    cfg = full_s3_cfg()
    dest = ob_backup.resolve_dest_tenant(cfg, "tpcc", "app_new")
    assert dest == "app_new"
    dest = ob_backup.resolve_dest_tenant(
        {"backup": {"restore": {"dest_tenant": "{tenant}_dr"}}}, "tpcc"
    )
    assert dest == "tpcc_dr"


if __name__ == "__main__":
    test_missing_s3_lists_all_fields()
    test_env_fills_keys()
    test_partial_yaml_still_errors_before_sql()
    test_aws_requires_region()
    test_uri_and_sql()
    test_tenant_and_forbidden()
    test_bad_uri_chars()
    test_parse_status()
    test_archive_off_does_not_need_s3()
    test_cli_flags_after_subcommand()
    test_wrappers_and_deploy_sh()
    test_restore_sql_and_plan()
    test_restore_fail_fast()
    print("ok")
