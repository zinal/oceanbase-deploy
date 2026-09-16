#!/usr/bin/env python3
"""Физический бэкап user-тенанта и архив clog на S3-совместимое хранилище.

Параметры dest — секция `backup` в config/deploy.yaml (профиль деплоя).
Ключи можно подставить переменными OB_BACKUP_S3_*. Нет обязательного поля —
сразу ошибка, без SQL к кластеру.

Официально: сначала ARCHIVELOG (STATUS=DOING), потом BACKUP.
https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000006615585
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
RESERVED_PREFIX_CHARS = re.compile(r"[?#&=\s]")


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


def wait_cfg_seconds(cfg: dict[str, Any], key: str, default: int) -> int:
    backup = _section(cfg, "backup")
    archive = backup.get("archive") if isinstance(backup.get("archive"), dict) else {}
    raw = backup.get(key, archive.get(key))
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
    out = run_sql_text(ob_sys, endpoint, password, backup_jobs_sql(tenant))
    return parse_status_rows(out)


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
    before_id = int(before[0][0]) if before and before[0][0].isdigit() else 0
    print(sql)
    run_sql_text(ob_sys, endpoint, password, sql)
    if not wait:
        print("backup job отправлен (--no-wait)")
        return 0
    timeout = wait_cfg_seconds(cfg, "wait_backup_sec", 7200)

    def _done() -> bool:
        jobs = fetch_backup_jobs(ob_sys, endpoint, password, name)
        if not jobs:
            return False
        job_id, status = jobs[0]
        if job_id.isdigit() and int(job_id) < before_id:
            return False
        st = status.upper()
        if st in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"backup job {job_id} STATUS={st}")
        return st == "COMPLETED"

    wait_until(_done, timeout_sec=timeout, what=f"backup {mode} {name} COMPLETED")
    jobs = fetch_backup_jobs(ob_sys, endpoint, password, name)
    label = f"{jobs[0][0]} STATUS={jobs[0][1]}" if jobs else "нет строк в CDB_OB_BACKUP_JOBS"
    print(f"backup {name} {mode}: {label}")
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
        print("CDB_OB_BACKUP_JOBS: пусто")
    else:
        print("CDB_OB_BACKUP_JOBS (свежие):")
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
