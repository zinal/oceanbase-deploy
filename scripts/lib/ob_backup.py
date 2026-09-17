#!/usr/bin/env python3
"""Физический бэкап user-тенанта, архив clog и restore на S3-совместимое хранилище.

Параметры dest — секция `backup` в config/deploy.yaml (профиль деплоя).
Ключи можно подставить переменными OB_BACKUP_S3_*. Нет обязательного поля —
сразу ошибка, без SQL к кластеру. Restore дополнительно требует pool_list
(существующий пустой resource pool) и создаёт новый standby-тенант.

Официально: сначала ARCHIVELOG (STATUS=DOING), потом BACKUP.
RESTORE создаёт новый standby и не перезаписывает существующий
тенант (имя dest может совпадать с исходным, если того уже нет).
https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000006615585
https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005282824
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable
REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = Path(__file__).resolve().parent

TENANT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Значения query URI: офиц. набор + точка/дефис (host, region).
URI_VALUE_RE = re.compile(r"^[A-Za-z0-9/_\-$+=.]+$")
PIECE_RE = re.compile(r"^[1-7]d$")
# locality=F,R{1}@z1; primary_zone=z1;z2,z3
RESTORE_OPTION_VALUE_RE = re.compile(r"^[A-Za-z0-9_,;{}@.\-]+$")
UNTIL_TIME_RE = re.compile(
    r"^[0-9]{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?$"
)
SCN_RE = re.compile(r"^[0-9]+$")
FORBIDDEN_TENANTS = frozenset({"sys", "oceanbase"})
REQUIRED_S3_FIELDS = ("host", "bucket", "access_id", "access_key")
S3_ENV = {
    "host": "OB_BACKUP_S3_HOST",
    "bucket": "OB_BACKUP_S3_BUCKET",
    "access_id": "OB_BACKUP_S3_ACCESS_ID",
    "access_key": "OB_BACKUP_S3_ACCESS_KEY",
    "s3_region": "OB_BACKUP_S3_REGION",
}
DEFAULT_DATA_PREFIX = "backup/{tenant}/data"
DEFAULT_ARCHIVE_PREFIX = "backup/{tenant}/archive"
DEFAULT_DEST_TENANT = "{tenant}_restore"
DEFAULT_RESTORE_METHOD = "full"
RESERVED_PREFIX_CHARS = re.compile(r"[?#&=\s]")
# CDB_OB_BACKUP_JOBS — только активные; после конца строка в CDB_OB_BACKUP_JOB_HISTORY.
BACKUP_SUCCESS_STATUS = frozenset({"COMPLETED", "SUCCESS"})
BACKUP_FAILED_STATUS = frozenset({"FAILED", "CANCELED", "CANCELLED"})
# Progress: RESTORE_SUCCESS; history: SUCCESS / FAILED.
RESTORE_SUCCESS_STATUS = frozenset({"RESTORE_SUCCESS", "SUCCESS"})
RESTORE_FAILED_STATUS = frozenset({"RESTORE_FAIL", "FAIL", "FAILED"})
TIMEOUT_STATUS_HINT = (
    "завершённый backup уходит в CDB_OB_BACKUP_JOB_HISTORY; "
    "restore history STATUS=SUCCESS, не RESTORE_SUCCESS"
)


class BackupConfigError(ValueError):
    """Профиль неполный или некорректный — команды не должны идти в кластер."""


def _load_ob_sys() -> Any:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    raw = cfg.get(name)
    return raw if isinstance(raw, dict) else {}


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sql_ident(name: str) -> str:
    if not TENANT_RE.match(name):
        raise BackupConfigError(f"Недопустимое имя тенанта: {name!r}")
    return name


def resolve_tenant(cfg: dict[str, Any], override: str | None = None) -> str:
    if override and override.strip():
        name = override.strip()
    else:
        backup = _section(cfg, "backup")
        name = _as_str(backup.get("tenant"))
        if not name:
            tenant = _section(cfg, "tenant")
            name = _as_str(tenant.get("tenant_name"))
    if not name:
        raise BackupConfigError(
            "не задан тенант. Укажите backup.tenant, tenant.tenant_name или --tenant"
        )
    if name.lower() in FORBIDDEN_TENANTS or name.upper().startswith("META"):
        raise BackupConfigError(
            f"тенант {name!r} нельзя бэкапить (sys / Meta). Нужен user-тенант"
        )
    if not TENANT_RE.match(name):
        raise BackupConfigError(
            f"тенант {name!r} — только буквы/цифры/_, начинается с буквы или _"
        )
    return name


def _s3_section(cfg: dict[str, Any]) -> dict[str, Any]:
    backup = _section(cfg, "backup")
    raw = backup.get("s3")
    return raw if isinstance(raw, dict) else {}


def _restore_section(cfg: dict[str, Any]) -> dict[str, Any]:
    backup = _section(cfg, "backup")
    raw = backup.get("restore")
    return raw if isinstance(raw, dict) else {}


def _env_or_yaml(s3: dict[str, Any], field: str, environ: dict[str, str]) -> str:
    env_name = S3_ENV.get(field)
    if env_name:
        env_val = (environ.get(env_name) or "").strip()
        if env_val:
            return env_val
    return _as_str(s3.get(field))


def missing_s3_fields(
    cfg: dict[str, Any], environ: dict[str, str] | None = None
) -> list[str]:
    """Имена незаполненных обязательных полей backup.s3 (без обращения к кластеру)."""
    env = environ if environ is not None else os.environ
    s3 = _s3_section(cfg)
    missing = [field for field in REQUIRED_S3_FIELDS if not _env_or_yaml(s3, field, env)]
    host = _env_or_yaml(s3, "host", env)
    region = _env_or_yaml(s3, "s3_region", env)
    if host and "amazonaws.com" in host.lower() and not region:
        missing.append("s3_region")
    return missing


def s3_missing_error_message(missing: Iterable[str]) -> str:
    fields = ", ".join(missing)
    env_hints = ", ".join(S3_ENV[f] for f in REQUIRED_S3_FIELDS)
    return (
        f"в профиле backup.s3 не заданы обязательные параметры: {fields}. "
        f"Задайте их в config/deploy.yaml (секция backup.s3) или через {env_hints}"
    )


def require_s3_profile(
    cfg: dict[str, Any], environ: dict[str, str] | None = None
) -> None:
    missing = missing_s3_fields(cfg, environ)
    if missing:
        raise BackupConfigError(s3_missing_error_message(missing))


def _check_uri_value(field: str, value: str) -> None:
    if not value:
        return
    if not URI_VALUE_RE.match(value):
        raise BackupConfigError(
            f"backup.s3.{field} содержит символы вне [A-Za-z0-9/._$+=-]. "
            "OceanBase отвергает такие значения в s3:// URI"
        )


def _normalize_prefix(prefix: str, tenant: str) -> str:
    text = prefix.replace("{tenant}", tenant).strip()
    text = text.lstrip("/")
    text = re.sub(r"/+", "/", text).rstrip("/")
    if not text:
        raise BackupConfigError("префикс S3 пустой после нормализации")
    if RESERVED_PREFIX_CHARS.search(text):
        raise BackupConfigError(
            f"префикс {text!r} не должен содержать ?, #, &, = и пробелы"
        )
    for part in text.split("/"):
        _check_uri_value("prefix", part)
    return text


def resolve_s3(
    cfg: dict[str, Any],
    tenant: str,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    env = environ if environ is not None else os.environ
    require_s3_profile(cfg, env)
    s3 = _s3_section(cfg)
    backup = _section(cfg, "backup")
    archive = backup.get("archive") if isinstance(backup.get("archive"), dict) else {}

    host = _env_or_yaml(s3, "host", env)
    bucket = _env_or_yaml(s3, "bucket", env)
    access_id = _env_or_yaml(s3, "access_id", env)
    access_key = _env_or_yaml(s3, "access_key", env)
    s3_region = _env_or_yaml(s3, "s3_region", env)
    addressing = _as_str(s3.get("addressing_model")) or "path_style"
    checksum = _as_str(s3.get("checksum_type")) or "md5"
    delete_mode = _as_str(s3.get("delete_mode")) or "delete"
    data_prefix = _as_str(s3.get("data_prefix")) or DEFAULT_DATA_PREFIX
    archive_prefix = _as_str(s3.get("archive_prefix")) or DEFAULT_ARCHIVE_PREFIX
    binding = _as_str(archive.get("binding")) or "Optional"
    piece = _as_str(archive.get("piece_switch_interval")) or "1d"

    addressing_l = addressing.lower().replace("-", "_")
    if addressing_l in {"path_style", "pathstyle"}:
        addressing = "path_style"
    elif addressing_l in {"virtual_hosted_style", "virtualhostedstyle", "virtual_hosted"}:
        addressing = "virtual_hosted_style"
    else:
        raise BackupConfigError(
            f"backup.s3.addressing_model={addressing!r} — path_style или virtual_hosted_style"
        )

    checksum_l = checksum.lower()
    if checksum_l not in {"md5", "crc32"}:
        raise BackupConfigError(
            f"backup.s3.checksum_type={checksum!r} — md5 (S3-compatible) или crc32 (AWS)"
        )
    checksum = checksum_l

    delete_l = delete_mode.lower()
    if delete_l not in {"delete", "tagging"}:
        raise BackupConfigError(
            f"backup.s3.delete_mode={delete_mode!r} — delete или tagging"
        )
    delete_mode = delete_l

    bind_l = binding.lower()
    if bind_l not in {"optional", "mandatory"}:
        raise BackupConfigError(
            f"backup.archive.binding={binding!r} — Optional или Mandatory"
        )
    binding = "Optional" if bind_l == "optional" else "Mandatory"

    piece = piece.lower()
    if not PIECE_RE.match(piece):
        raise BackupConfigError(
            f"backup.archive.piece_switch_interval={piece!r} — [1d, 7d], например 1d"
        )

    for field, value in (
        ("host", host),
        ("bucket", bucket),
        ("access_id", access_id),
        ("access_key", access_key),
        ("s3_region", s3_region),
        ("addressing_model", addressing),
        ("checksum_type", checksum),
        ("delete_mode", delete_mode),
    ):
        _check_uri_value(field, value)

    return {
        "host": host,
        "bucket": bucket,
        "access_id": access_id,
        "access_key": access_key,
        "s3_region": s3_region,
        "addressing_model": addressing,
        "checksum_type": checksum,
        "delete_mode": delete_mode,
        "data_prefix": _normalize_prefix(data_prefix, tenant),
        "archive_prefix": _normalize_prefix(archive_prefix, tenant),
        "binding": binding,
        "piece_switch_interval": piece,
    }


def build_s3_uri(s3: dict[str, str], prefix: str) -> str:
    params = [
        ("host", s3["host"]),
        ("access_id", s3["access_id"]),
        ("access_key", s3["access_key"]),
    ]
    if s3.get("s3_region"):
        params.append(("s3_region", s3["s3_region"]))
    params.append(("addressing_model", s3["addressing_model"]))
    params.append(("checksum_type", s3["checksum_type"]))
    params.append(("delete_mode", s3["delete_mode"]))
    query = "&".join(f"{k}={v}" for k, v in params)
    return f"s3://{s3['bucket']}/{prefix}?{query}"


def redact_uri(uri: str) -> str:
    return re.sub(r"(access_key=)[^&]+", r"\1***", uri, flags=re.IGNORECASE)


def data_backup_dest_sql(uri: str, tenant: str) -> str:
    return (
        f"ALTER SYSTEM SET DATA_BACKUP_DEST = {sql_literal(uri)} "
        f"TENANT = {sql_ident(tenant)}"
    )


def log_archive_dest_sql(
    uri: str, tenant: str, *, binding: str, piece_switch_interval: str
) -> str:
    dest = (
        f"LOCATION={uri} BINDING={binding} "
        f"PIECE_SWITCH_INTERVAL={piece_switch_interval}"
    )
    return (
        f"ALTER SYSTEM SET LOG_ARCHIVE_DEST = {sql_literal(dest)} "
        f"TENANT = {sql_ident(tenant)}"
    )


def archivelog_sql(tenant: str) -> str:
    return f"ALTER SYSTEM ARCHIVELOG TENANT = {sql_ident(tenant)}"


def noarchivelog_sql(tenant: str) -> str:
    return f"ALTER SYSTEM NOARCHIVELOG TENANT = {sql_ident(tenant)}"


def backup_sql(tenant: str, mode: str, *, plus_archivelog: bool = False) -> str:
    key = (mode or "").strip().lower()
    if key in {"full", "database"}:
        extra = " PLUS ARCHIVELOG" if plus_archivelog else ""
        return f"ALTER SYSTEM BACKUP TENANT = {sql_ident(tenant)}{extra}"
    if key in {"incremental", "incr", "inc"}:
        if plus_archivelog:
            raise BackupConfigError("PLUS ARCHIVELOG допустим только для полного бэкапа")
        return f"ALTER SYSTEM BACKUP INCREMENTAL TENANT = {sql_ident(tenant)}"
    raise BackupConfigError(
        f"режим бэкапа {mode!r} — укажите full или incremental"
    )


def resolve_dest_tenant(
    cfg: dict[str, Any], source: str, override: str | None = None
) -> str:
    if override and str(override).strip():
        dest = str(override).strip()
    else:
        dest = _as_str(_restore_section(cfg).get("dest_tenant"))
        if not dest:
            dest = DEFAULT_DEST_TENANT
    dest = dest.replace("{tenant}", source)
    if dest.lower() in FORBIDDEN_TENANTS or dest.upper().startswith("META"):
        raise BackupConfigError(
            f"тенант {dest!r} нельзя создавать restore (sys / Meta)"
        )
    if not TENANT_RE.match(dest):
        raise BackupConfigError(
            f"dest_tenant {dest!r} — только буквы/цифры/_, начинается с буквы или _"
        )
    # Имя dest может совпадать с source: после DROP TENANT это штатный
    # in-place restore. Живой тенант с тем же именем отсекает
    # assert_dest_absent по DBA_OB_TENANTS, не сравнение строк.
    return dest


def resolve_pool_list(cfg: dict[str, Any], override: str | None = None) -> str:
    if override and str(override).strip():
        raw = str(override).strip()
    else:
        raw = _as_str(_restore_section(cfg).get("pool_list"))
    if not raw:
        raise BackupConfigError(
            "не задан pool_list. Укажите backup.restore.pool_list или --pool. "
            "RESTORE требует существующий пустой resource pool"
        )
    pools = [p.strip() for p in raw.split(",") if p.strip()]
    if not pools:
        raise BackupConfigError("pool_list пустой")
    for pool in pools:
        if not TENANT_RE.match(pool):
            raise BackupConfigError(
                f"имя resource pool {pool!r} — только буквы/цифры/_, "
                "начинается с буквы или _"
            )
    return ",".join(pools)


def _cfg_or_override(override: str | None, yaml_val: Any) -> str:
    if override is not None and str(override).strip():
        return str(override).strip()
    return _as_str(yaml_val)


def _optional_restore_value(field: str, value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if not RESTORE_OPTION_VALUE_RE.match(text):
        raise BackupConfigError(
            f"backup.restore.{field}={text!r} содержит недопустимые символы"
        )
    return text


def resolve_restore_method(cfg: dict[str, Any], override: str | None = None) -> str:
    raw = _cfg_or_override(override, _restore_section(cfg).get("method"))
    key = (raw or DEFAULT_RESTORE_METHOD).lower()
    if key not in {"full", "quick"}:
        raise BackupConfigError(f"backup.restore.method={raw!r} — full или quick")
    return key


def resolve_until(
    cfg: dict[str, Any],
    *,
    until_time: str | None = None,
    until_scn: str | None = None,
) -> tuple[str, str]:
    restore = _restore_section(cfg)
    time_val = _cfg_or_override(until_time, restore.get("until_time"))
    scn_val = _cfg_or_override(until_scn, restore.get("until_scn"))
    if time_val and scn_val:
        raise BackupConfigError("UNTIL TIME и UNTIL SCN нельзя задавать вместе")
    if time_val and not UNTIL_TIME_RE.match(time_val):
        raise BackupConfigError(
            f"until_time={time_val!r} — ожидается YYYY-MM-DD HH:MM:SS[.fraction]"
        )
    if scn_val and not SCN_RE.match(scn_val):
        raise BackupConfigError(f"until_scn={scn_val!r} — целое SCN")
    return time_val, scn_val


def bool_from_cfg(raw: Any, default: bool = False) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def restore_option(
    *,
    pool_list: str,
    locality: str = "",
    primary_zone: str = "",
    concurrency: str = "",
    method: str = DEFAULT_RESTORE_METHOD,
) -> str:
    parts = [f"pool_list={pool_list}"]
    if locality:
        parts.append(f"locality={locality}")
    if primary_zone:
        parts.append(f"primary_zone={primary_zone}")
    if concurrency:
        parts.append(f"concurrency={concurrency}")
    parts.append(f"method={method}")
    return "&".join(parts)


def restore_sql(
    dest_tenant: str,
    data_uri: str,
    archive_uri: str,
    *,
    option: str,
    until_time: str = "",
    until_scn: str = "",
) -> str:
    from_uri = f"{data_uri},{archive_uri}"
    sql = (
        f"ALTER SYSTEM RESTORE {sql_ident(dest_tenant)} "
        f"FROM {sql_literal(from_uri)}"
    )
    if until_time and until_scn:
        raise BackupConfigError("UNTIL TIME и UNTIL SCN нельзя задавать вместе")
    if until_time:
        sql += f" UNTIL TIME = {sql_literal(until_time)}"
    elif until_scn:
        if not SCN_RE.match(until_scn):
            raise BackupConfigError(f"until_scn={until_scn!r} — целое SCN")
        sql += f" UNTIL SCN = {until_scn}"
    sql += f" WITH {sql_literal(option)}"
    return sql


def activate_standby_sql(tenant: str) -> str:
    return f"ALTER SYSTEM ACTIVATE STANDBY TENANT {sql_ident(tenant)}"


def resolve_restore_plan(
    cfg: dict[str, Any],
    *,
    tenant: str | None = None,
    dest_tenant: str | None = None,
    pool_list: str | None = None,
    locality: str | None = None,
    primary_zone: str | None = None,
    concurrency: str | None = None,
    method: str | None = None,
    until_time: str | None = None,
    until_scn: str | None = None,
    activate: bool | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Собрать параметры RESTORE. S3 и pool_list — до SQL к кластеру."""
    source = resolve_tenant(cfg, tenant)
    s3 = resolve_s3(cfg, source, environ)
    dest = resolve_dest_tenant(cfg, source, dest_tenant)
    pools = resolve_pool_list(cfg, pool_list)
    restore = _restore_section(cfg)
    loc = _optional_restore_value(
        "locality", _cfg_or_override(locality, restore.get("locality"))
    )
    zone = _optional_restore_value(
        "primary_zone", _cfg_or_override(primary_zone, restore.get("primary_zone"))
    )
    conc = _cfg_or_override(concurrency, restore.get("concurrency"))
    if conc and not SCN_RE.match(conc):
        raise BackupConfigError(
            f"backup.restore.concurrency={conc!r} — целое число"
        )
    restore_method = resolve_restore_method(cfg, method)
    time_val, scn_val = resolve_until(cfg, until_time=until_time, until_scn=until_scn)
    if activate is True:
        act = True
    elif activate is False:
        act = False
    else:
        act = bool_from_cfg(restore.get("activate"), False)
    if restore_method == "quick" and act:
        raise BackupConfigError(
            "method=quick даёт только standby; ACTIVATE STANDBY недопустим. "
            "Уберите --activate / backup.restore.activate"
        )
    option = restore_option(
        pool_list=pools,
        locality=loc,
        primary_zone=zone,
        concurrency=conc,
        method=restore_method,
    )
    data_uri = build_s3_uri(s3, s3["data_prefix"])
    archive_uri = build_s3_uri(s3, s3["archive_prefix"])
    sql = restore_sql(
        dest,
        data_uri,
        archive_uri,
        option=option,
        until_time=time_val,
        until_scn=scn_val,
    )
    return {
        "source": source,
        "dest": dest,
        "pool_list": pools,
        "locality": loc,
        "primary_zone": zone,
        "concurrency": conc,
        "method": restore_method,
        "until_time": time_val,
        "until_scn": scn_val,
        "activate": act,
        "option": option,
        "data_uri": data_uri,
        "archive_uri": archive_uri,
        "sql": sql,
        "s3": s3,
    }


def archive_status_sql(tenant: str) -> str:
    return (
        "SELECT t.TENANT_NAME, a.STATUS "
        "FROM oceanbase.CDB_OB_ARCHIVELOG a "
        "INNER JOIN oceanbase.DBA_OB_TENANTS t ON a.TENANT_ID = t.TENANT_ID "
        f"WHERE t.TENANT_NAME = {sql_literal(tenant)}"
    )


def backup_jobs_sql(tenant: str) -> str:
    return (
        "SELECT j.JOB_ID, t.TENANT_NAME, j.STATUS "
        "FROM oceanbase.CDB_OB_BACKUP_JOBS j "
        "INNER JOIN oceanbase.DBA_OB_TENANTS t ON j.TENANT_ID = t.TENANT_ID "
        f"WHERE t.TENANT_NAME = {sql_literal(tenant)} "
        "ORDER BY j.JOB_ID DESC LIMIT 8"
    )


def backup_history_sql(tenant: str) -> str:
    return (
        "SELECT j.JOB_ID, t.TENANT_NAME, j.STATUS "
        "FROM oceanbase.CDB_OB_BACKUP_JOB_HISTORY j "
        "INNER JOIN oceanbase.DBA_OB_TENANTS t ON j.TENANT_ID = t.TENANT_ID "
        f"WHERE t.TENANT_NAME = {sql_literal(tenant)} "
        "ORDER BY j.JOB_ID DESC LIMIT 8"
    )


def restore_progress_sql(dest_tenant: str) -> str:
    return (
        "SELECT JOB_ID, RESTORE_TENANT_NAME, STATUS "
        "FROM oceanbase.CDB_OB_RESTORE_PROGRESS "
        f"WHERE RESTORE_TENANT_NAME = {sql_literal(dest_tenant)} "
        "ORDER BY JOB_ID DESC LIMIT 8"
    )


def restore_history_sql(dest_tenant: str) -> str:
    return (
        "SELECT JOB_ID, RESTORE_TENANT_NAME, STATUS "
        "FROM oceanbase.CDB_OB_RESTORE_HISTORY "
        f"WHERE RESTORE_TENANT_NAME = {sql_literal(dest_tenant)} "
        "ORDER BY JOB_ID DESC LIMIT 8"
    )


def tenant_lookup_sql(name: str) -> str:
    return (
        "SELECT TENANT_NAME, TENANT_ROLE FROM oceanbase.DBA_OB_TENANTS "
        f"WHERE TENANT_NAME = {sql_literal(name)}"
    )


def resource_pool_sql(pool: str) -> str:
    return (
        "SELECT NAME, TENANT_ID FROM oceanbase.DBA_OB_RESOURCE_POOLS "
        f"WHERE NAME = {sql_literal(pool)}"
    )


def parse_status_rows(stdout: str) -> list[tuple[str, str]]:
    """Пары (id-or-name, STATUS) из mysql -N -B (пробелы/табы)."""
    rows: list[tuple[str, str]] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("tenant_name") or line.lower().startswith("job_id"):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) < 2:
            continue
        rows.append((parts[0], parts[-1].upper()))
    return rows


def latest_status(rows: list[tuple[str, str]]) -> str:
    return rows[0][1] if rows else ""


def latest_job_id(rows: list[tuple[str, str]]) -> int:
    if rows and rows[0][0].isdigit():
        return int(rows[0][0])
    return 0


def wait_cfg_seconds(cfg: dict[str, Any], key: str, default: int) -> int:
    backup = _section(cfg, "backup")
    archive = backup.get("archive") if isinstance(backup.get("archive"), dict) else {}
    restore = backup.get("restore") if isinstance(backup.get("restore"), dict) else {}
    raw = backup.get(key)
    if raw is None or str(raw).strip() == "":
        raw = restore.get(key, archive.get(key))
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError) as exc:
        raise BackupConfigError(f"backup.{key}={raw!r} — целое число секунд") from exc


def plus_archivelog_from_cfg(cfg: dict[str, Any], override: bool | None) -> bool:
    if override is not None:
        return override
    backup = _section(cfg, "backup")
    raw = backup.get("plus_archivelog")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def connect(cfg: dict[str, Any], inv: dict[str, str]) -> tuple[Any, dict[str, Any], str]:
    ob_sys = _load_ob_sys()
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")
    if int(inv.get("OBSERVER_COUNT", "0") or 0) < 1:
        raise RuntimeError("В inventory нет OBSERVER_*_IP")
    endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    password = ob_sys.connect_sys_password(endpoint, cfg, deploy_name)
    return ob_sys, endpoint, password


def run_sql_text(ob_sys: Any, endpoint: dict[str, Any], password: str, sql: str) -> str:
    proc = ob_sys.run_sql(endpoint, password, sql, ignore_error=False)
    return proc.stdout or ""


def fetch_archive_status(
    ob_sys: Any, endpoint: dict[str, Any], password: str, tenant: str
) -> str:
    out = run_sql_text(ob_sys, endpoint, password, archive_status_sql(tenant))
    return latest_status(parse_status_rows(out))


def fetch_backup_jobs(
    ob_sys: Any, endpoint: dict[str, Any], password: str, tenant: str
) -> list[tuple[str, str]]:
    active = parse_status_rows(
        run_sql_text(ob_sys, endpoint, password, backup_jobs_sql(tenant))
    )
    history = parse_status_rows(
        run_sql_text(ob_sys, endpoint, password, backup_history_sql(tenant))
    )
    return _merge_job_rows(active, history)


def _merge_job_rows(*groups: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for group in groups:
        for job_id, status in group:
            if job_id in seen:
                continue
            seen.add(job_id)
            rows.append((job_id, status))
    rows.sort(key=lambda item: int(item[0]) if item[0].isdigit() else 0, reverse=True)
    return rows


def fetch_restore_progress(
    ob_sys: Any, endpoint: dict[str, Any], password: str, dest_tenant: str
) -> list[tuple[str, str]]:
    return parse_status_rows(
        run_sql_text(ob_sys, endpoint, password, restore_progress_sql(dest_tenant))
    )


def fetch_restore_jobs(
    ob_sys: Any, endpoint: dict[str, Any], password: str, dest_tenant: str
) -> list[tuple[str, str]]:
    progress = fetch_restore_progress(ob_sys, endpoint, password, dest_tenant)
    history = parse_status_rows(
        run_sql_text(ob_sys, endpoint, password, restore_history_sql(dest_tenant))
    )
    return _merge_job_rows(progress, history)


def fetch_tenant_role(
    ob_sys: Any, endpoint: dict[str, Any], password: str, name: str
) -> str:
    out = run_sql_text(ob_sys, endpoint, password, tenant_lookup_sql(name))
    rows = parse_status_rows(out)
    return rows[0][1] if rows else ""


def fetch_pool_tenant_id(
    ob_sys: Any, endpoint: dict[str, Any], password: str, pool: str
) -> str | None:
    out = run_sql_text(ob_sys, endpoint, password, resource_pool_sql(pool))
    for raw in (out or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("name"):
            continue
        parts = re.split(r"\s+", line)
        if not parts or parts[0] != pool:
            continue
        if len(parts) == 1:
            return ""
        return parts[-1]
    return None


def assert_dest_absent(
    ob_sys: Any, endpoint: dict[str, Any], password: str, dest: str
) -> None:
    role = fetch_tenant_role(ob_sys, endpoint, password, dest)
    if role:
        raise RuntimeError(
            f"тенант {dest} уже существует (TENANT_ROLE={role}) — "
            "RESTORE создаёт новый standby, не перезаписывает"
        )


def decide_activate(dest: str, role: str, progress: list[tuple[str, str]]) -> str:
    """activate | already_primary. Иначе RuntimeError."""
    if progress:
        job_id, status = progress[0]
        raise RuntimeError(
            f"restore {dest} ещё идёт (job {job_id} STATUS={status}) — "
            "дождитесь SUCCESS, затем ./scripts/deploy.sh restore activate"
        )
    key = (role or "").upper()
    if not key:
        raise RuntimeError(
            f"тенант {dest} не найден. Сначала "
            "./scripts/deploy.sh restore run --dest-tenant "
            f"{dest}"
        )
    if key == "PRIMARY":
        return "already_primary"
    if key != "STANDBY":
        raise RuntimeError(
            f"тенант {dest} TENANT_ROLE={role} — ACTIVATE нужен STANDBY"
        )
    return "activate"


def assert_pools_free(
    ob_sys: Any, endpoint: dict[str, Any], password: str, pool_list: str
) -> None:
    for pool in [p.strip() for p in pool_list.split(",") if p.strip()]:
        tenant_id = fetch_pool_tenant_id(ob_sys, endpoint, password, pool)
        if tenant_id is None:
            raise RuntimeError(
                f"resource pool {pool} не найден. Создайте пустой pool "
                "и укажите его в backup.restore.pool_list / --pool"
            )
        if tenant_id.upper() in {"", "NULL", "NONE", "-1", "0"}:
            continue
        if tenant_id.lstrip("-").isdigit() and int(tenant_id) > 0:
            raise RuntimeError(
                f"resource pool {pool} занят TENANT_ID={tenant_id} — "
                "RESTORE нужен пустой pool"
            )


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_sec: int,
    poll_sec: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    what: str,
) -> None:
    deadline = clock() + timeout_sec
    while True:
        if predicate():
            return
        if clock() >= deadline:
            raise RuntimeError(f"таймаут {timeout_sec}s: {what}")
        sleeper(poll_sec)


def wait_for_latest_job(
    fetch: Callable[[], list[tuple[str, str]]],
    *,
    before_id: int,
    success: frozenset[str],
    failed: frozenset[str],
    timeout_sec: int,
    what: str,
    poll_sec: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> tuple[str, str]:
    """Ждать job с id > before_id в success. Печатать смену STATUS."""
    last = {"id": "", "status": "", "seen": False}

    def _done() -> bool:
        jobs = fetch()
        if not jobs:
            last["id"] = ""
            last["status"] = ""
            last["seen"] = False
            return False
        job_id, status = jobs[0]
        if job_id.isdigit() and int(job_id) <= before_id:
            return False
        st = status.upper()
        if not last["seen"] or last["id"] != job_id or last["status"] != st:
            log(f"  job {job_id} STATUS={st}")
            last["id"] = job_id
            last["status"] = st
            last["seen"] = True
        if st in failed:
            raise RuntimeError(f"{what} job {job_id} STATUS={st}")
        return st in success

    try:
        wait_until(
            _done,
            timeout_sec=timeout_sec,
            poll_sec=poll_sec,
            sleeper=sleeper,
            clock=clock,
            what=what,
        )
    except RuntimeError as exc:
        extra = ""
        if last["seen"]:
            extra = f"; последний job {last['id']} STATUS={last['status']}"
        elif "таймаут" in str(exc):
            extra = f"; {TIMEOUT_STATUS_HINT}"
        raise RuntimeError(f"{exc}{extra}") from None
    return last["id"], last["status"]


def archive_on(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    tenant: str | None = None,
    environ: dict[str, str] | None = None,
    wait: bool = True,
) -> int:
    name = resolve_tenant(cfg, tenant)
    s3 = resolve_s3(cfg, name, environ)
    uri = build_s3_uri(s3, s3["archive_prefix"])
    ob_sys, endpoint, password = connect(cfg, inv)
    sql = log_archive_dest_sql(
        uri,
        name,
        binding=s3["binding"],
        piece_switch_interval=s3["piece_switch_interval"],
    )
    print(f"LOG_ARCHIVE_DEST {name}: {redact_uri(uri)}")
    print(f"BINDING={s3['binding']} PIECE_SWITCH_INTERVAL={s3['piece_switch_interval']}")
    run_sql_text(ob_sys, endpoint, password, sql)
    status = fetch_archive_status(ob_sys, endpoint, password, name)
    if status not in {"DOING", "BEGINNING", "PREPARE"}:
        print(archivelog_sql(name))
        run_sql_text(ob_sys, endpoint, password, archivelog_sql(name))
    if wait:
        timeout = wait_cfg_seconds(cfg, "wait_doing_sec", 180)

        def _doing() -> bool:
            return fetch_archive_status(ob_sys, endpoint, password, name) == "DOING"

        wait_until(_doing, timeout_sec=timeout, what=f"архив {name} STATUS=DOING")
    status = fetch_archive_status(ob_sys, endpoint, password, name)
    print(f"архив {name}: STATUS={status or '<нет строки>'}")
    if status != "DOING":
        raise RuntimeError(f"архив {name} не DOING (сейчас {status or 'пусто'})")
    return 0


def archive_off(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    tenant: str | None = None,
    wait: bool = True,
) -> int:
    name = resolve_tenant(cfg, tenant)
    ob_sys, endpoint, password = connect(cfg, inv)
    status = fetch_archive_status(ob_sys, endpoint, password, name)
    if status in {"", "STOP"}:
        print(f"архив {name}: уже STATUS={status or 'STOP'} — пропуск NOARCHIVELOG")
        return 0
    print(noarchivelog_sql(name))
    run_sql_text(ob_sys, endpoint, password, noarchivelog_sql(name))
    if wait:
        timeout = wait_cfg_seconds(cfg, "wait_stop_sec", 180)

        def _stopped() -> bool:
            cur = fetch_archive_status(ob_sys, endpoint, password, name)
            return cur in {"", "STOP"}

        wait_until(_stopped, timeout_sec=timeout, what=f"архив {name} STATUS=STOP")
    status = fetch_archive_status(ob_sys, endpoint, password, name)
    print(f"архив {name}: STATUS={status or 'STOP'}")
    return 0


def run_backup(
    cfg: dict[str, Any],
    inv: dict[str, str],
    mode: str,
    *,
    tenant: str | None = None,
    environ: dict[str, str] | None = None,
    plus_archivelog: bool | None = None,
    wait: bool = True,
) -> int:
    name = resolve_tenant(cfg, tenant)
    s3 = resolve_s3(cfg, name, environ)
    plus = plus_archivelog_from_cfg(cfg, plus_archivelog)
    uri = build_s3_uri(s3, s3["data_prefix"])
    sql = backup_sql(name, mode, plus_archivelog=plus)
    ob_sys, endpoint, password = connect(cfg, inv)
    print(f"DATA_BACKUP_DEST {name}: {redact_uri(uri)}")
    run_sql_text(ob_sys, endpoint, password, data_backup_dest_sql(uri, name))
    status = fetch_archive_status(ob_sys, endpoint, password, name)
    if status != "DOING":
        timeout = wait_cfg_seconds(cfg, "wait_doing_sec", 180)
        if wait and status in {"BEGINNING", "PREPARE", ""}:

            def _doing() -> bool:
                return fetch_archive_status(ob_sys, endpoint, password, name) == "DOING"

            try:
                wait_until(_doing, timeout_sec=timeout, what=f"архив {name} STATUS=DOING")
            except RuntimeError:
                status = fetch_archive_status(ob_sys, endpoint, password, name)
                raise RuntimeError(
                    f"архив {name} не DOING (сейчас {status or 'пусто'}). "
                    "Сначала: ./scripts/deploy.sh archive-log on"
                ) from None
        else:
            raise RuntimeError(
                f"архив {name} не DOING (сейчас {status or 'пусто'}). "
                "Сначала: ./scripts/deploy.sh archive-log on"
            )
    before = fetch_backup_jobs(ob_sys, endpoint, password, name)
    before_id = latest_job_id(before)
    print(sql)
    run_sql_text(ob_sys, endpoint, password, sql)
    if not wait:
        print("backup job отправлен (--no-wait)")
        return 0
    timeout = wait_cfg_seconds(cfg, "wait_backup_sec", 7200)
    job_id, status = wait_for_latest_job(
        lambda: fetch_backup_jobs(ob_sys, endpoint, password, name),
        before_id=before_id,
        success=BACKUP_SUCCESS_STATUS,
        failed=BACKUP_FAILED_STATUS,
        timeout_sec=timeout,
        what=f"backup {mode} {name} COMPLETED",
    )
    print(f"backup {name} {mode}: {job_id} STATUS={status}")
    return 0


def print_restore_plan(plan: dict[str, Any]) -> None:
    print(f"источник: {plan['source']}")
    print(f"dest:     {plan['dest']} (новый standby)")
    print(f"pool_list={plan['pool_list']} method={plan['method']}")
    if plan["until_time"]:
        print(f"UNTIL TIME = {plan['until_time']}")
    if plan["until_scn"]:
        print(f"UNTIL SCN = {plan['until_scn']}")
    print(f"data:     {redact_uri(plan['data_uri'])}")
    print(f"archive:  {redact_uri(plan['archive_uri'])}")
    print(redact_uri(plan["sql"]))
    if plan["activate"]:
        print(activate_standby_sql(plan["dest"]))


def run_restore(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    tenant: str | None = None,
    dest_tenant: str | None = None,
    pool_list: str | None = None,
    locality: str | None = None,
    primary_zone: str | None = None,
    concurrency: str | None = None,
    method: str | None = None,
    until_time: str | None = None,
    until_scn: str | None = None,
    activate: bool | None = None,
    environ: dict[str, str] | None = None,
    wait: bool = True,
) -> int:
    plan = resolve_restore_plan(
        cfg,
        tenant=tenant,
        dest_tenant=dest_tenant,
        pool_list=pool_list,
        locality=locality,
        primary_zone=primary_zone,
        concurrency=concurrency,
        method=method,
        until_time=until_time,
        until_scn=until_scn,
        activate=activate,
        environ=environ,
    )
    dest = plan["dest"]
    ob_sys, endpoint, password = connect(cfg, inv)
    assert_dest_absent(ob_sys, endpoint, password, dest)
    assert_pools_free(ob_sys, endpoint, password, plan["pool_list"])
    print_restore_plan(plan)
    before = fetch_restore_jobs(ob_sys, endpoint, password, dest)
    before_id = latest_job_id(before)
    run_sql_text(ob_sys, endpoint, password, plan["sql"])
    if not wait:
        print("restore job отправлен (--no-wait)")
        return 0
    timeout = wait_cfg_seconds(cfg, "wait_restore_sec", 7200)
    job_id, status = wait_for_latest_job(
        lambda: fetch_restore_jobs(ob_sys, endpoint, password, dest),
        before_id=before_id,
        success=RESTORE_SUCCESS_STATUS,
        failed=RESTORE_FAILED_STATUS,
        timeout_sec=timeout,
        what=f"restore {dest} RESTORE_SUCCESS",
    )
    print(f"restore {dest}: {job_id} STATUS={status}")
    if plan["activate"]:
        sql = activate_standby_sql(dest)
        print(sql)
        run_sql_text(ob_sys, endpoint, password, sql)
        role = fetch_tenant_role(ob_sys, endpoint, password, dest)
        print(f"тенант {dest}: TENANT_ROLE={role or '<нет>'}")
    else:
        role = fetch_tenant_role(ob_sys, endpoint, password, dest)
        print(
            f"тенант {dest}: TENANT_ROLE={role or 'STANDBY'}. "
            "Клиентам как primary: ./scripts/deploy.sh restore activate "
            f"--dest-tenant {dest}"
        )
    return 0


def run_activate(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    tenant: str | None = None,
    dest_tenant: str | None = None,
) -> int:
    source = resolve_tenant(cfg, tenant)
    dest = resolve_dest_tenant(cfg, source, dest_tenant)
    ob_sys, endpoint, password = connect(cfg, inv)
    role = fetch_tenant_role(ob_sys, endpoint, password, dest)
    progress = fetch_restore_progress(ob_sys, endpoint, password, dest)
    action = decide_activate(dest, role, progress)
    if action == "already_primary":
        print(f"тенант {dest}: уже PRIMARY — ACTIVATE не нужен")
        return 0
    sql = activate_standby_sql(dest)
    print(sql)
    run_sql_text(ob_sys, endpoint, password, sql)
    role = fetch_tenant_role(ob_sys, endpoint, password, dest)
    print(f"тенант {dest}: TENANT_ROLE={role or '<нет>'}")
    if (role or "").upper() != "PRIMARY":
        raise RuntimeError(
            f"после ACTIVATE тенант {dest} не PRIMARY (сейчас {role or 'пусто'})"
        )
    return 0


def show_restore(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    tenant: str | None = None,
    dest_tenant: str | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    source = resolve_tenant(cfg, tenant)
    dest = resolve_dest_tenant(cfg, source, dest_tenant)
    print(f"источник: {source}")
    print(f"dest:     {dest}")
    try:
        require_s3_profile(cfg, environ)
        s3 = resolve_s3(cfg, source, environ)
        print(f"data:     {redact_uri(build_s3_uri(s3, s3['data_prefix']))}")
        print(f"archive:  {redact_uri(build_s3_uri(s3, s3['archive_prefix']))}")
    except BackupConfigError as exc:
        print(f"S3 профиль: {exc}")
    ob_sys, endpoint, password = connect(cfg, inv)
    role = fetch_tenant_role(ob_sys, endpoint, password, dest)
    print(f"DBA_OB_TENANTS {dest}: {role or '<нет>'}")
    jobs = fetch_restore_jobs(ob_sys, endpoint, password, dest)
    if not jobs:
        print("CDB_OB_RESTORE_PROGRESS/HISTORY: пусто")
    else:
        print("restore jobs (свежие):")
        for job_id, status in jobs:
            print(f"  {job_id} {status}")
    return 0


def show_status(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    tenant: str | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    name = resolve_tenant(cfg, tenant)
    print(f"тенант: {name}")
    try:
        require_s3_profile(cfg, environ)
        s3 = resolve_s3(cfg, name, environ)
        print(f"data:    {redact_uri(build_s3_uri(s3, s3['data_prefix']))}")
        print(f"archive: {redact_uri(build_s3_uri(s3, s3['archive_prefix']))}")
        print(
            f"archive opts: BINDING={s3['binding']} "
            f"PIECE_SWITCH_INTERVAL={s3['piece_switch_interval']}"
        )
    except BackupConfigError as exc:
        print(f"S3 профиль: {exc}")
    ob_sys, endpoint, password = connect(cfg, inv)
    arch = fetch_archive_status(ob_sys, endpoint, password, name)
    print(f"CDB_OB_ARCHIVELOG.STATUS={arch or '<нет>'}")
    jobs = fetch_backup_jobs(ob_sys, endpoint, password, name)
    if not jobs:
        print("CDB_OB_BACKUP_JOBS/HISTORY: пусто")
    else:
        print("backup jobs (активные + history):")
        for job_id, status in jobs:
            print(f"  {job_id} {status}")
    return 0


def _load_cfg(args: argparse.Namespace) -> dict[str, Any]:
    ob_sys = _load_ob_sys()
    path = Path(args.config)
    if not path.exists():
        raise BackupConfigError(f"нет файла профиля {path}")
    return ob_sys.load_yaml(path)


def _load_io(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str]]:
    cfg = _load_cfg(args)
    inv_path = Path(args.inventory)
    if not inv_path.exists():
        raise RuntimeError(f"Нет {inv_path} — сначала ./scripts/deploy.sh deploy")
    ob_sys = _load_ob_sys()
    inv = ob_sys.load_inventory(inv_path)
    return cfg, inv


def cmd_backup(args: argparse.Namespace) -> None:
    cfg = _load_cfg(args)
    resolve_tenant(cfg, args.tenant)
    require_s3_profile(cfg)
    cfg, inv = _load_io(args)
    plus_override = True if getattr(args, "plus_archivelog", False) else None
    code = run_backup(
        cfg,
        inv,
        args.mode,
        tenant=args.tenant,
        plus_archivelog=plus_override,
        wait=not args.no_wait,
    )
    if code:
        sys.exit(code)


def cmd_archive(args: argparse.Namespace) -> None:
    cfg = _load_cfg(args)
    resolve_tenant(cfg, args.tenant)
    if args.action == "on":
        require_s3_profile(cfg)
        cfg, inv = _load_io(args)
        archive_on(cfg, inv, tenant=args.tenant, wait=not args.no_wait)
        return
    cfg, inv = _load_io(args)
    archive_off(cfg, inv, tenant=args.tenant, wait=not args.no_wait)


def cmd_show(args: argparse.Namespace) -> None:
    cfg, inv = _load_io(args)
    show_status(cfg, inv, tenant=args.tenant)


def cmd_validate(args: argparse.Namespace) -> None:
    cfg = _load_cfg(args)
    tenant = resolve_tenant(cfg, args.tenant)
    s3 = resolve_s3(cfg, tenant)
    print(f"тенант: {tenant}")
    print(f"data:    {redact_uri(build_s3_uri(s3, s3['data_prefix']))}")
    print(f"archive: {redact_uri(build_s3_uri(s3, s3['archive_prefix']))}")
    print("профиль backup.s3 полный")


def _restore_plan_from_args(cfg: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    activate_override = True if getattr(args, "activate", False) else None
    return resolve_restore_plan(
        cfg,
        tenant=getattr(args, "tenant", None),
        dest_tenant=getattr(args, "dest_tenant", None),
        pool_list=getattr(args, "pool", None),
        locality=getattr(args, "locality", None),
        primary_zone=getattr(args, "primary_zone", None),
        concurrency=getattr(args, "concurrency", None),
        method=getattr(args, "method", None),
        until_time=getattr(args, "until_time", None),
        until_scn=getattr(args, "until_scn", None),
        activate=activate_override,
    )


def cmd_restore(args: argparse.Namespace) -> None:
    cfg = _load_cfg(args)
    resolve_tenant(cfg, args.tenant)
    action = getattr(args, "action", "run") or "run"
    if action == "show":
        cfg, inv = _load_io(args)
        show_restore(cfg, inv, tenant=args.tenant, dest_tenant=getattr(args, "dest_tenant", None))
        return
    if action == "activate":
        cfg, inv = _load_io(args)
        code = run_activate(
            cfg,
            inv,
            tenant=args.tenant,
            dest_tenant=getattr(args, "dest_tenant", None),
        )
        if code:
            sys.exit(code)
        return
    require_s3_profile(cfg)
    plan = _restore_plan_from_args(cfg, args)
    if action == "validate":
        print_restore_plan(plan)
        print("профиль restore полный")
        return
    cfg, inv = _load_io(args)
    code = run_restore(
        cfg,
        inv,
        tenant=args.tenant,
        dest_tenant=getattr(args, "dest_tenant", None),
        pool_list=getattr(args, "pool", None),
        locality=getattr(args, "locality", None),
        primary_zone=getattr(args, "primary_zone", None),
        concurrency=getattr(args, "concurrency", None),
        method=getattr(args, "method", None),
        until_time=getattr(args, "until_time", None),
        until_scn=getattr(args, "until_scn", None),
        activate=True if getattr(args, "activate", False) else None,
        wait=not getattr(args, "no_wait", False),
    )
    if code:
        sys.exit(code)


def _add_io_args(parser: argparse.ArgumentParser, *, with_defaults: bool) -> None:
    if with_defaults:
        parser.add_argument("--config", default=str(REPO_ROOT / "config" / "deploy.yaml"))
        parser.add_argument(
            "--inventory", default=str(REPO_ROOT / "generated" / "inventory.env")
        )
        parser.add_argument(
            "--tenant", default=None, help="перекрыть backup.tenant / tenant.tenant_name"
        )
        return
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--inventory", default=argparse.SUPPRESS)
    parser.add_argument("--tenant", default=argparse.SUPPRESS)


def _add_restore_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "action",
        nargs="?",
        choices=("run", "show", "validate", "activate"),
        default="run",
        help="run — RESTORE; activate — ACTIVATE STANDBY; show — CDB_OB_RESTORE_*; validate — профиль без SQL",
    )
    parser.add_argument(
        "--dest-tenant",
        default=None,
        help="имя нового тенанта (по умолчанию backup.restore.dest_tenant или {tenant}_restore); может совпадать с исходным, если того уже нет",
    )
    parser.add_argument(
        "--pool",
        default=None,
        dest="pool",
        help="backup.restore.pool_list: существующий пустой resource pool",
    )
    parser.add_argument("--locality", default=None)
    parser.add_argument("--primary-zone", default=None, dest="primary_zone")
    parser.add_argument("--concurrency", default=None)
    parser.add_argument("--method", choices=("full", "quick"), default=None)
    parser.add_argument("--until-time", default=None, dest="until_time")
    parser.add_argument("--until-scn", default=None, dest="until_scn")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="после RESTORE_SUCCESS в том же run: ALTER SYSTEM ACTIVATE STANDBY TENANT "
        "(отдельный шаг: restore activate)",
    )
    parser.add_argument("--no-wait", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_io_args(parser, with_defaults=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="проверить профиль S3 без SQL")
    _add_io_args(p_val, with_defaults=False)
    p_val.set_defaults(func=cmd_validate)

    p_show = sub.add_parser("show", help="статус архива и backup jobs")
    _add_io_args(p_show, with_defaults=False)
    p_show.set_defaults(func=cmd_show)

    p_backup = sub.add_parser("backup", help="полный или инкрементальный data backup")
    _add_io_args(p_backup, with_defaults=False)
    p_backup.add_argument("mode", choices=("full", "incremental"))
    p_backup.add_argument(
        "--plus-archivelog",
        action="store_true",
        help="BACKUP … PLUS ARCHIVELOG (только full)",
    )
    p_backup.add_argument("--no-wait", action="store_true")
    p_backup.set_defaults(func=cmd_backup)

    p_arch = sub.add_parser("archive", help="включить или выключить ARCHIVELOG")
    _add_io_args(p_arch, with_defaults=False)
    p_arch.add_argument("action", choices=("on", "off"))
    p_arch.add_argument("--no-wait", action="store_true")
    p_arch.set_defaults(func=cmd_archive)

    p_restore = sub.add_parser(
        "restore", help="RESTORE в новый standby-тенант из того же S3 dest"
    )
    _add_io_args(p_restore, with_defaults=False)
    _add_restore_args(p_restore)
    p_restore.set_defaults(func=cmd_restore)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except BackupConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
