#!/usr/bin/env python3
"""Серверный снимок OceanBase для точки TPC-C (Phase 0.4).

Требования:
https://github.com/zinal/portable-tpcc/blob/main/docs/oceanbase-efficiency-improvement-plan.md

Синхронный snapshot без паролей, connection string, params_value и литералов
SQL-параметров. query_sql обрезается до sql_head.

    ./scripts/deploy.sh snapshot collect --label w45k06
    ./scripts/deploy.sh snapshot list
    ./scripts/deploy.sh snapshot dump-sql
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = Path(__file__).resolve().parent

# Таблицы TPC-C. SHOW CREATE TABLE подтверждает partitions / tablegroup / FK.
TPCC_TABLES: tuple[str, ...] = (
    "warehouse",
    "district",
    "customer",
    "history",
    "new_order",
    "oorder",
    "order_line",
    "stock",
    "item",
)

# Клиентские коды из плана + внутренние OB-эквиваленты.
LOCK_OR_SERIAL_RET_CODES = "1205, 6235, -6235, -6210, -4012, 4012, 600"

PLAN_DOC = (
    "https://github.com/zinal/portable-tpcc/blob/main/"
    "docs/oceanbase-efficiency-improvement-plan.md"
)


@dataclass(frozen=True)
class SnapshotQuery:
    query_id: str
    title: str
    topic: str
    scope: str
    required: bool
    sqls: tuple[str, ...]
    note: str = ""


def _load_ob_sys() -> Any:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_tenant_create() -> Any:
    path = LIB_DIR / "tenant-create.py"
    spec = importlib.util.spec_from_file_location("tenant_create", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sql_identifier(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"Недопустимый идентификатор SQL: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def pred_tenant_id(tenant_name: str | None, column: str = "tenant_id") -> str:
    if tenant_name:
        return (
            f"{column} = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS "
            f"WHERE tenant_name = {sql_literal(tenant_name)})"
        )
    return (
        f"{column} IN (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS "
        "WHERE tenant_type = 'USER')"
    )


def pred_tenant_name(tenant_name: str | None, column: str = "tenant_name") -> str:
    if tenant_name:
        return f"{column} = {sql_literal(tenant_name)}"
    return f"{column} NOT IN ('sys')"


def pred_processlist_tenant(tenant_name: str | None) -> str:
    if tenant_name:
        return f"(tenant = {sql_literal(tenant_name)} OR `user` LIKE {sql_literal(tenant_name + '%')})"
    return "tenant NOT IN ('sys')"


def compact_sql(sql: str) -> str:
    lines = [line.strip() for line in sql.strip().splitlines() if line.strip()]
    return " ".join(lines)


def pretty_sql(sql: str) -> str:
    """Разбить однострочный SELECT по клаузам верхнего уровня (не внутри скобок)."""
    text = compact_sql(sql)
    markers = (
        " UNION ALL ",
        " LEFT JOIN ",
        " JOIN ",
        " FROM ",
        " WHERE ",
        " GROUP BY ",
        " HAVING ",
        " ORDER BY ",
        " LIMIT ",
    )
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            out.append(ch)
            i += 1
            continue
        if depth == 0:
            rest = text[i:]
            matched = False
            for marker in markers:
                if rest.startswith(marker):
                    out.append("\n" + marker.strip() + " ")
                    i += len(marker)
                    matched = True
                    break
            if matched:
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Каталог запросов Phase 0.4
# ---------------------------------------------------------------------------

def snapshot_queries(tenant_name: str | None, database: str) -> list[SnapshotQuery]:
    tid = pred_tenant_id(tenant_name)
    db_lit = sql_literal(database)
    db_id = sql_identifier(database)
    queries: list[SnapshotQuery] = [
        SnapshotQuery(
            query_id="cluster-version",
            title="Версия и audit",
            topic="meta",
            scope="sys",
            required=True,
            sqls=(
                "SELECT 'version' AS name, @@version_comment AS value "
                "UNION ALL SELECT 'version_compile', @@version "
                "UNION ALL SELECT name, value FROM oceanbase.GV$OB_PARAMETERS "
                "WHERE name IN ('min_observer_version', 'enable_sql_audit', "
                "'ob_enable_sql_audit') GROUP BY name, value",
                "SHOW VARIABLES LIKE 'version%'",
            ),
            note="Пустой GV$OB_SQL_AUDIT при enable_sql_audit=false.",
        ),
        SnapshotQuery(
            query_id="servers",
            title="Состав кластера (DBA_OB_SERVERS)",
            topic="meta",
            scope="sys",
            required=True,
            sqls=(
                "SELECT svr_ip, svr_port, zone, status, start_service_time, "
                "stop_time, with_rootserver FROM oceanbase.DBA_OB_SERVERS "
                "ORDER BY zone, svr_ip",
            ),
        ),
        SnapshotQuery(
            query_id="tenants",
            title="Тенанты и PRIMARY_ZONE",
            topic="partition_leaders",
            scope="sys",
            required=True,
            sqls=(
                "SELECT tenant_id, tenant_name, tenant_type, status, "
                "primary_zone, locality, compatibility_mode "
                "FROM oceanbase.DBA_OB_TENANTS ORDER BY tenant_id",
            ),
        ),
        SnapshotQuery(
            query_id="sql-audit-by-id",
            title="GV$OB_SQL_AUDIT: sql_id / plan_id / server / ret_code / event",
            topic="sql_audit",
            scope="sys",
            required=True,
            sqls=(
                "SELECT sql_id, plan_id, svr_ip, ret_code, event, "
                "COUNT(*) AS executions, "
                "SUM(CASE WHEN ret_code <> 0 THEN 1 ELSE 0 END) AS errors, "
                "ROUND(AVG(elapsed_time)) AS avg_elapsed_us, "
                "ROUND(AVG(queue_time)) AS avg_queue_us, "
                "ROUND(AVG(execute_time)) AS avg_execute_us, "
                "SUM(return_rows) AS return_rows, "
                "SUM(affected_rows) AS affected_rows "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                "GROUP BY sql_id, plan_id, svr_ip, ret_code, event "
                "ORDER BY executions DESC LIMIT 200",
                "SELECT sql_id, plan_id, svr_ip, ret_code, "
                "COUNT(*) AS executions, "
                "SUM(CASE WHEN ret_code <> 0 THEN 1 ELSE 0 END) AS errors, "
                "ROUND(AVG(elapsed_time)) AS avg_elapsed_us, "
                "ROUND(AVG(queue_time)) AS avg_queue_us "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                "GROUP BY sql_id, plan_id, svr_ip, ret_code "
                "ORDER BY executions DESC LIMIT 200",
                "SELECT sql_id, plan_id, svr_ip, ret_code, event, "
                "COUNT(*) AS executions "
                "FROM gv$sql_audit "
                f"WHERE is_inner_sql = 0 AND {tid} "
                "GROUP BY sql_id, plan_id, svr_ip, ret_code, event "
                "ORDER BY executions DESC LIMIT 200",
            ),
            note="Не выбираем полный query_sql и bind-параметры — только агрегаты.",
        ),
        SnapshotQuery(
            query_id="sql-audit-heads",
            title="Топ SQL (обрезанный sql_head, без параметров)",
            topic="sql_audit",
            scope="sys",
            required=True,
            sqls=(
                "SELECT sql_id, LEFT(query_sql, 120) AS sql_head, "
                "COUNT(*) AS executions, "
                f"SUM(CASE WHEN ret_code IN ({LOCK_OR_SERIAL_RET_CODES}) "
                "THEN 1 ELSE 0 END) AS lock_or_serial, "
                "ROUND(AVG(elapsed_time)) AS avg_elapsed_us, "
                "MAX(elapsed_time) AS max_elapsed_us "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                "GROUP BY sql_id, LEFT(query_sql, 120) "
                "ORDER BY executions DESC LIMIT 80",
            ),
        ),
        SnapshotQuery(
            query_id="sql-audit-errors",
            title="1205 / 6235 и соседние ret_code по sql_id и sql_head",
            topic="lock_waits",
            scope="sys",
            required=True,
            sqls=(
                "SELECT ret_code, sql_id, plan_id, svr_ip, "
                "LEFT(query_sql, 80) AS sql_head, COUNT(*) AS n, "
                "ROUND(AVG(elapsed_time)) AS avg_elapsed_us "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                f"AND ret_code IN ({LOCK_OR_SERIAL_RET_CODES}) "
                "GROUP BY ret_code, sql_id, plan_id, svr_ip, LEFT(query_sql, 80) "
                "ORDER BY n DESC LIMIT 100",
                "SELECT ret_code, sql_id, plan_id, svr_ip, table_name, "
                "LEFT(query_sql, 80) AS sql_head, COUNT(*) AS n "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                f"AND ret_code IN ({LOCK_OR_SERIAL_RET_CODES}) "
                "GROUP BY ret_code, sql_id, plan_id, svr_ip, table_name, "
                "LEFT(query_sql, 80) ORDER BY n DESC LIMIT 100",
            ),
            note="Класс блокирующей строки — в lock-waits rowkey, не в params.",
        ),
        SnapshotQuery(
            query_id="local-remote-dist",
            title="Local / remote / distributed (plan_type) по observer",
            topic="local_remote_dist",
            scope="sys",
            required=True,
            sqls=(
                "SELECT svr_ip, "
                "SUM(plan_type = 1) AS local_plan, "
                "SUM(plan_type = 2) AS remote_plan, "
                "SUM(plan_type = 3) AS dist_plan, "
                "SUM(partition_hit = 0) AS part_miss, "
                "SUM(partition_hit = 1) AS part_hit, "
                "COUNT(*) AS stmts "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                "GROUP BY svr_ip ORDER BY stmts DESC",
            ),
        ),
        SnapshotQuery(
            query_id="local-remote-dist-by-sql",
            title="Local / remote / dist по sql_id (ожидаемая доля remote New-Order/Payment)",
            topic="local_remote_dist",
            scope="sys",
            required=True,
            sqls=(
                "SELECT sql_id, LEFT(query_sql, 80) AS sql_head, "
                "SUM(plan_type = 1) AS local_plan, "
                "SUM(plan_type = 2) AS remote_plan, "
                "SUM(plan_type = 3) AS dist_plan, "
                "COUNT(*) AS stmts "
                "FROM oceanbase.GV$OB_SQL_AUDIT "
                f"WHERE is_inner_sql = 0 AND {tid} "
                "GROUP BY sql_id, LEFT(query_sql, 80) "
                "ORDER BY dist_plan DESC, remote_plan DESC, stmts DESC LIMIT 80",
            ),
        ),
        SnapshotQuery(
            query_id="lock-waits",
            title="GV$OB_LOCK_WAIT_STAT: waiter / holder / rowkey",
            topic="lock_waits",
            scope="sys",
            required=True,
            sqls=(
                "SELECT w.tenant_id, w.svr_ip, w.table_id, w.tablet_id, "
                "LEFT(w.rowkey, 128) AS rowkey, w.session_id AS waiter_sid, "
                "w.block_session_id AS holder_sid, w.holder_tx_id, "
                "w.waiter_tx_id, w.lock_mode, w.type, w.try_lock_times, "
                "w.time_after_recv "
                "FROM oceanbase.GV$OB_LOCK_WAIT_STAT w "
                f"WHERE {pred_tenant_id(tenant_name, 'w.tenant_id')} "
                "ORDER BY w.time_after_recv DESC LIMIT 200",
                "SELECT tenant_id, svr_ip, table_id, tablet_id, "
                "LEFT(rowkey, 128) AS rowkey, session_id, block_session_id, "
                "try_lock_times, time_after_recv "
                "FROM oceanbase.__all_virtual_lock_wait_stat "
                f"WHERE {tid} "
                "ORDER BY time_after_recv DESC LIMIT 200",
            ),
        ),
        SnapshotQuery(
            query_id="lock-waits-with-sql",
            title="Lock wait + waiter SQL из processlist (обрезанный info)",
            topic="lock_waits",
            scope="sys",
            required=False,
            sqls=(
                "SELECT w.svr_ip, w.session_id AS waiter_sid, "
                "w.block_session_id AS holder_sid, w.holder_tx_id, "
                "w.waiter_tx_id, LEFT(w.rowkey, 128) AS rowkey, "
                "w.table_id, w.tablet_id, w.time_after_recv, "
                "LEFT(p.info, 80) AS waiter_sql_head "
                "FROM oceanbase.GV$OB_LOCK_WAIT_STAT w "
                "LEFT JOIN oceanbase.GV$OB_PROCESSLIST p "
                "ON p.id = w.session_id AND p.svr_ip = w.svr_ip "
                f"WHERE {pred_tenant_id(tenant_name, 'w.tenant_id')} "
                "ORDER BY w.time_after_recv DESC LIMIT 200",
            ),
        ),
        SnapshotQuery(
            query_id="plan-cache-stat",
            title="Plan cache hit / miss по observer",
            topic="plan_cache",
            scope="sys",
            required=True,
            sqls=(
                "SELECT tenant_id, svr_ip, access_count, hit_count, "
                "(access_count - hit_count) AS miss_count, "
                "ROUND(100 * hit_count / NULLIF(access_count, 0), 2) AS hit_pct, "
                "mem_used, mem_hold "
                "FROM oceanbase.GV$OB_PLAN_CACHE_STAT "
                f"WHERE {tid} ORDER BY svr_ip",
                "SELECT tenant_id, svr_ip, access_count, hit_count, "
                "(access_count - hit_count) AS miss_count "
                "FROM oceanbase.GV$PLAN_CACHE_STAT "
                f"WHERE {tid} ORDER BY svr_ip",
            ),
        ),
        SnapshotQuery(
            query_id="plan-cache-plans",
            title="Планы в кэше (first_load_time / schema_version — смена плана)",
            topic="plan_cache",
            scope="sys",
            required=True,
            sqls=(
                "SELECT svr_ip, sql_id, plan_id, type, hit_count, executions, "
                "avg_exe_usec, first_load_time, last_active_time, schema_version "
                "FROM oceanbase.GV$OB_PLAN_CACHE_PLAN_STAT "
                f"WHERE {tid} ORDER BY executions DESC LIMIT 100",
                "SELECT svr_ip, sql_id, plan_id, type, hit_count, executions "
                "FROM oceanbase.GV$PLAN_CACHE_PLAN_STAT "
                f"WHERE {tid} ORDER BY executions DESC LIMIT 100",
            ),
        ),
        SnapshotQuery(
            query_id="plan-changes",
            title="sql_id с несколькими plan_id (смена плана)",
            topic="plan_cache",
            scope="sys",
            required=False,
            sqls=(
                "SELECT sql_id, COUNT(DISTINCT plan_id) AS plans, "
                "COUNT(DISTINCT svr_ip) AS servers, SUM(executions) AS executions "
                "FROM oceanbase.GV$OB_PLAN_CACHE_PLAN_STAT "
                f"WHERE {tid} GROUP BY sql_id "
                "HAVING COUNT(DISTINCT plan_id) > 1 "
                "ORDER BY plans DESC, executions DESC LIMIT 50",
            ),
        ),
        SnapshotQuery(
            query_id="units",
            title="Unit'ы тенанта: CPU / memory / max_session_num / observer",
            topic="tenant_resources",
            scope="sys",
            required=True,
            sqls=(
                "SELECT t.tenant_name, u.unit_id, u.svr_ip, u.zone, u.status, "
                "u.min_cpu, u.max_cpu, u.memory_size, u.max_memory, "
                "u.max_session_num "
                "FROM oceanbase.DBA_OB_UNITS u "
                "JOIN oceanbase.DBA_OB_TENANTS t ON u.tenant_id = t.tenant_id "
                f"WHERE {pred_tenant_id(tenant_name, 'u.tenant_id')} "
                "ORDER BY u.zone, u.svr_ip",
                "SELECT tenant_id, unit_id, svr_ip, zone, min_cpu, max_cpu, "
                "memory_size FROM oceanbase.GV$OB_UNITS "
                f"WHERE {tid} ORDER BY svr_ip",
            ),
        ),
        SnapshotQuery(
            query_id="tablet-leaders",
            title="Лидеры tablet по observer",
            topic="partition_leaders",
            scope="sys",
            required=True,
            sqls=(
                "SELECT svr_ip, COUNT(*) AS tablet_leaders "
                "FROM oceanbase.DBA_OB_TABLE_LOCATIONS "
                f"WHERE role = 'LEADER' AND {tid} "
                "GROUP BY svr_ip ORDER BY tablet_leaders DESC",
                "SELECT svr_ip, COUNT(*) AS tablet_leaders "
                "FROM oceanbase.CDB_OB_TABLE_LOCATIONS "
                f"WHERE role = 'LEADER' AND {tid} "
                "GROUP BY svr_ip ORDER BY tablet_leaders DESC",
            ),
        ),
        SnapshotQuery(
            query_id="tablet-leaders-by-table",
            title="Лидеры tablet по таблице и observer",
            topic="partition_leaders",
            scope="sys",
            required=True,
            sqls=(
                "SELECT database_name, table_name, svr_ip, COUNT(*) AS tablet_leaders "
                "FROM oceanbase.DBA_OB_TABLE_LOCATIONS "
                f"WHERE role = 'LEADER' AND {tid} "
                "GROUP BY database_name, table_name, svr_ip "
                "ORDER BY table_name, tablet_leaders DESC",
                "SELECT database_name, table_name, svr_ip, COUNT(*) AS tablet_leaders "
                "FROM oceanbase.CDB_OB_TABLE_LOCATIONS "
                f"WHERE role = 'LEADER' AND {tid} "
                "GROUP BY database_name, table_name, svr_ip "
                "ORDER BY table_name, tablet_leaders DESC",
            ),
        ),
        SnapshotQuery(
            query_id="sessions",
            title="Сессии тенанта по observer и command",
            topic="tenant_resources",
            scope="sys",
            required=True,
            sqls=(
                "SELECT tenant, svr_ip, command, COUNT(*) AS sessions "
                "FROM oceanbase.GV$OB_PROCESSLIST "
                f"WHERE {pred_processlist_tenant(tenant_name)} "
                "GROUP BY tenant, svr_ip, command ORDER BY sessions DESC",
                "SELECT svr_ip, command, COUNT(*) AS sessions "
                "FROM gv$ob_processlist "
                f"WHERE {pred_processlist_tenant(tenant_name)} "
                "GROUP BY svr_ip, command ORDER BY sessions DESC",
            ),
        ),
        SnapshotQuery(
            query_id="memory",
            title="Память тенанта (GV$OB_MEMORY)",
            topic="tenant_resources",
            scope="sys",
            required=True,
            sqls=(
                "SELECT tenant_id, svr_ip, ctx_name, hold, used, `limit` "
                "FROM oceanbase.GV$OB_MEMORY "
                f"WHERE {tid} ORDER BY hold DESC LIMIT 80",
                "SELECT tenant_id, svr_ip, ctx_name, hold, used "
                "FROM oceanbase.GV$OB_TENANT_MEMORY "
                f"WHERE {tid} ORDER BY hold DESC LIMIT 80",
            ),
        ),
        SnapshotQuery(
            query_id="memstore-freeze",
            title="Memstore / minor freeze",
            topic="tenant_resources",
            scope="sys",
            required=True,
            sqls=(
                "SELECT tenant_id, svr_ip, active_span, freeze_trigger, "
                "mem_limit, freeze_cnt "
                "FROM oceanbase.GV$OB_MEMSTORE "
                f"WHERE {tid} ORDER BY svr_ip",
                "SELECT tenant_id, svr_ip, memstore_used, memstore_limit, "
                "freeze_trigger, freeze_cnt "
                "FROM oceanbase.GV$OB_MEMSTORE "
                f"WHERE {tid} ORDER BY svr_ip",
                "SELECT * FROM oceanbase.GV$OB_MEMSTORE "
                f"WHERE {tid} LIMIT 80",
            ),
        ),
        SnapshotQuery(
            query_id="sysstat",
            title="CPU / RPC / IO / lock / throttle / freeze (GV$SYSSTAT)",
            topic="tenant_resources",
            scope="sys",
            required=True,
            sqls=(
                "SELECT tenant_id, svr_ip, name, value "
                "FROM oceanbase.GV$SYSSTAT "
                f"WHERE {tid} AND ("
                "name IN ("
                "'cpu usage', 'memory usage', "
                "'sql execute count', 'trans commit count', 'trans rollback count', "
                "'rpc packet in', 'rpc packet out', "
                "'rpc packet in bytes', 'rpc packet out bytes', "
                "'io read bytes', 'io write bytes', "
                "'memstore used', 'memstore limit', 'clog disk used'"
                ") OR name LIKE '%throttle%' OR name LIKE '%freeze%' "
                "OR name LIKE '%lock wait%' OR name LIKE '%rpc%' "
                "OR name LIKE '%cpu%') "
                "ORDER BY name, svr_ip",
                "SELECT tenant_id, svr_ip, name, value "
                "FROM oceanbase.GV$OB_SYSSTAT "
                f"WHERE {tid} AND ("
                "name LIKE '%cpu%' OR name LIKE '%rpc%' OR name LIKE '%throttle%' "
                "OR name LIKE '%freeze%' OR name LIKE '%lock%' OR name LIKE '%memstore%'"
                ") ORDER BY name, svr_ip LIMIT 400",
            ),
        ),
        SnapshotQuery(
            query_id="compaction",
            title="Major compaction / throttling",
            topic="tenant_resources",
            scope="sys",
            required=False,
            sqls=(
                "SELECT tenant_id, frozen_scn, status, start_time, "
                "last_finish_time, is_error "
                "FROM oceanbase.CDB_OB_MAJOR_COMPACTION "
                f"WHERE {tid}",
                "SELECT tenant_id, frozen_scn, status, start_time, last_finish_time "
                "FROM oceanbase.DBA_OB_MAJOR_COMPACTION "
                f"WHERE {tid}",
                "SELECT tenant_id, svr_ip, type, status, compaction_scn, progress "
                "FROM oceanbase.GV$OB_COMPACTION_PROGRESS "
                f"WHERE {tid} LIMIT 200",
            ),
        ),
        SnapshotQuery(
            query_id="log-stat",
            title="Лог-стримы (роль лидера / follower) — bandwidth в sysstat",
            topic="tenant_resources",
            scope="sys",
            required=False,
            sqls=(
                "SELECT svr_ip, role, COUNT(*) AS streams "
                "FROM oceanbase.GV$OB_LOG_STAT "
                "GROUP BY svr_ip, role ORDER BY svr_ip, role",
                "SELECT svr_ip, role, COUNT(*) AS streams "
                "FROM gv$ob_log_stat "
                "GROUP BY svr_ip, role ORDER BY svr_ip, role",
            ),
        ),
        SnapshotQuery(
            query_id="tenant-timeouts",
            title="Эффективные OLTP timeout тенанта (не bulk query_timeout профиля)",
            topic="timeouts",
            scope="tenant",
            required=True,
            sqls=(
                "SHOW VARIABLES WHERE Variable_name IN ("
                "'ob_query_timeout', 'ob_trx_timeout', 'ob_trx_idle_timeout', "
                "'ob_trx_lock_timeout', 'innodb_lock_wait_timeout', "
                "'ob_enable_sql_audit', 'autocommit', 'ob_read_consistency')",
            ),
            note="План §3.6: worker не берёт database.options.query_timeout.",
        ),
        SnapshotQuery(
            query_id="schema-tables",
            title="Список таблиц TPC-C database",
            topic="schema",
            scope="tenant",
            required=True,
            sqls=(
                "SELECT table_name, table_type, engine, table_rows, "
                "create_options FROM information_schema.tables "
                f"WHERE table_schema = {db_lit} ORDER BY table_name",
            ),
        ),
        SnapshotQuery(
            query_id="schema-partitions",
            title="HASH partitions / binding (information_schema.partitions)",
            topic="schema",
            scope="tenant",
            required=True,
            sqls=(
                "SELECT table_name, partition_method, subpartition_method, "
                "partition_expression, COUNT(*) AS partition_count "
                "FROM information_schema.partitions "
                f"WHERE table_schema = {db_lit} "
                "GROUP BY table_name, partition_method, subpartition_method, "
                "partition_expression ORDER BY table_name",
            ),
        ),
        SnapshotQuery(
            query_id="schema-tablegroups",
            title="Tablegroup / sharding (binding HASH)",
            topic="schema",
            scope="tenant",
            required=False,
            sqls=(
                "SELECT tablegroup_name, sharding, tablegroup_id "
                "FROM oceanbase.DBA_OB_TABLEGROUPS",
                f"SELECT table_name, tablegroup_name FROM oceanbase.DBA_OB_TABLES "
                f"WHERE database_name = {db_lit} OR table_schema = {db_lit}",
            ),
        ),
        SnapshotQuery(
            query_id="schema-indexes",
            title="Индексы TPC-C",
            topic="schema",
            scope="tenant",
            required=False,
            sqls=(
                "SELECT table_name, index_name, non_unique, seq_in_index, "
                "column_name FROM information_schema.statistics "
                f"WHERE table_schema = {db_lit} "
                "ORDER BY table_name, index_name, seq_in_index",
            ),
        ),
        SnapshotQuery(
            query_id="schema-foreign-keys",
            title="FOREIGN KEY (в baseline плана должны быть выключены)",
            topic="schema",
            scope="tenant",
            required=False,
            sqls=(
                "SELECT table_name, constraint_name, constraint_type "
                "FROM information_schema.table_constraints "
                f"WHERE table_schema = {db_lit} "
                "AND constraint_type = 'FOREIGN KEY' "
                "ORDER BY table_name, constraint_name",
            ),
        ),
    ]
    for table in TPCC_TABLES:
        qualified = f"{db_id}.{sql_identifier(table)}"
        queries.append(
            SnapshotQuery(
                query_id=f"create-{table}",
                title=f"SHOW CREATE TABLE {database}.{table}",
                topic="schema",
                scope="tenant",
                required=False,
                sqls=(f"SHOW CREATE TABLE {qualified}",),
            )
        )
    return queries


REQUIRED_TOPICS: tuple[str, ...] = (
    "sql_audit",
    "lock_waits",
    "plan_cache",
    "local_remote_dist",
    "partition_leaders",
    "tenant_resources",
    "schema",
    "timeouts",
)


def queries_by_id(
    tenant_name: str | None,
    database: str,
) -> dict[str, SnapshotQuery]:
    return {q.query_id: q for q in snapshot_queries(tenant_name, database)}


def filter_queries(
    queries: list[SnapshotQuery],
    only: set[str] | None,
    skip_schema: bool,
) -> list[SnapshotQuery]:
    out: list[SnapshotQuery] = []
    for query in queries:
        if skip_schema and query.topic == "schema":
            continue
        if only is None:
            out.append(query)
            continue
        if query.query_id in only or query.topic in only:
            out.append(query)
    if only and not out:
        known = ", ".join(q.query_id for q in queries)
        raise ValueError(f"--only не совпал ни с одним запросом. Известны: {known}")
    return out


def assert_catalog_safe(queries: list[SnapshotQuery] | None = None) -> None:
    queries = queries or snapshot_queries("tpcc", "tpcc")
    joined = "\n".join("\n".join(q.sqls) for q in queries)
    lowered = joined.lower()
    for banned in ("params_value", "password", "secret", "access_key"):
        if banned in lowered:
            raise AssertionError(f"В SQL снимка запрещено {banned!r}")
    topics = {q.topic for q in queries}
    missing = [t for t in REQUIRED_TOPICS if t not in topics]
    if missing:
        raise AssertionError(f"В каталоге нет тем плана: {missing}")
    audit = next(q for q in queries if q.query_id == "sql-audit-by-id")
    assert "GROUP BY sql_id, plan_id, svr_ip, ret_code, event" in audit.sqls[0]
    assert "GV$OB_SQL_AUDIT" in audit.sqls[0]
    locks = next(q for q in queries if q.query_id == "lock-waits")
    assert "GV$OB_LOCK_WAIT_STAT" in locks.sqls[0]
    assert "rowkey" in locks.sqls[0]
    assert "holder_tx_id" in locks.sqls[0]
    plans = next(q for q in queries if q.query_id == "plan-cache-stat")
    assert "hit_count" in plans.sqls[0]
    dist = next(q for q in queries if q.query_id == "local-remote-dist")
    assert "plan_type = 1" in dist.sqls[0] and "plan_type = 3" in dist.sqls[0]
    leaders = next(q for q in queries if q.query_id == "tablet-leaders")
    assert "DBA_OB_TABLE_LOCATIONS" in leaders.sqls[0]
    units = next(q for q in queries if q.query_id == "units")
    assert "DBA_OB_UNITS" in units.sqls[0]
    sysstat = next(q for q in queries if q.query_id == "sysstat")
    blob = sysstat.sqls[0].lower()
    for token in ("cpu", "rpc", "throttle", "freeze"):
        if token not in blob:
            raise AssertionError(f"sysstat не покрывает {token}")
    creates = [q.query_id for q in queries if q.query_id.startswith("create-")]
    for table in TPCC_TABLES:
        if f"create-{table}" not in creates:
            raise AssertionError(f"нет SHOW CREATE TABLE для {table}")


def render_sql_pack(tenant_name: str = "tpcc", database: str = "tpcc") -> str:
    lines = [
        "-- Серверный снимок OceanBase 5.0.x для точки TPC-C (Phase 0.4).",
        f"-- План: {PLAN_DOC}",
        "-- Запускать как root@sys (observer:2881) для запросов scope=sys",
        f"-- и как root@{tenant_name} для scope=tenant (USE {database}).",
        "-- Не включает bind-параметры / пароли / connection string.",
        "-- Подставьте tenant/database при другом имени.",
        "-- Перегенерация: python3 scripts/lib/ob_snapshot.py dump-sql --output docs/sql/tpcc-server-snapshot-501.sql",
        "",
    ]
    for query in snapshot_queries(tenant_name, database):
        lines.append(f"-- ===== {query.query_id}: {query.title} [{query.scope}] =====")
        if query.note:
            lines.append(f"-- {query.note}")
        lines.append(pretty_sql(query.sqls[0]) + ";")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_sql_pack_path() -> Path:
    return REPO_ROOT / "docs" / "sql" / "tpcc-server-snapshot-501.sql"


# ---------------------------------------------------------------------------
# Выполнение
# ---------------------------------------------------------------------------

def run_snapshot_sql(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
    sql: str,
    *,
    timeout: int = 90,
) -> Any:
    cmd = ob_sys._client_bin() + [
        f"-h{endpoint['ip']}",
        f"-P{endpoint['port']}",
        f"-u{endpoint['user']}",
        "--connect-timeout=15",
        "-B",
        "-e",
        sql,
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=timeout,
    )


def try_query(
    runner: Callable[..., Any],
    endpoint: dict[str, Any],
    password: str,
    query: SnapshotQuery,
    timeout: int,
) -> tuple[str, str, str, int]:
    """Возвращает (status, sql_used, stdout, elapsed_ms). status=ok|error."""
    errors: list[str] = []
    for sql in query.sqls:
        started = time.monotonic()
        try:
            proc = runner(endpoint, password, sql, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — хотим текст любой ошибки клиента
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode == 0:
            return "ok", sql, proc.stdout or "", elapsed_ms
        err = (proc.stderr or proc.stdout or "").strip()
        errors.append(err or f"exit {proc.returncode}")
    return "error", query.sqls[0], "\n".join(errors), 0


def count_data_rows(tsv: str) -> int:
    rows = [line for line in tsv.splitlines() if line.strip()]
    if len(rows) <= 1:
        return 0
    return len(rows) - 1


def snapshot_dirname(label: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in (label or "snapshot"))
    safe = safe.strip("-") or "snapshot"
    return f"{stamp}_{safe}"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def connect_sys(
    ob_sys: Any,
    cfg: dict[str, Any],
    inv: dict[str, str],
    via: str,
) -> tuple[dict[str, Any], str]:
    if via == "obproxy":
        rows = ob_sys.inventory_ips(inv, "OBPROXY")
        if not rows:
            via = "observer"
        else:
            _idx, ip, name = rows[0]
            cluster = ob_sys.cfg_str(cfg, "oceanbase.cluster_name", "obcluster")
            endpoint = {
                "ip": ip,
                "port": ob_sys.cfg_int(cfg, "oceanbase.ports.obproxy", 2883),
                "user": f"root@sys#{cluster}",
                "via": "obproxy",
                "name": name,
            }
            password = ob_sys.connect_sys_password(endpoint, cfg, inv.get("DEPLOY_NAME", ""))
            return endpoint, password
    endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    password = ob_sys.connect_sys_password(endpoint, cfg, inv.get("DEPLOY_NAME", ""))
    return endpoint, password


def connect_tenant(
    tenant_mod: Any,
    ob_sys: Any,
    cfg: dict[str, Any],
    inv: dict[str, str],
    tenant_name: str,
    password: str,
) -> dict[str, Any]:
    endpoint = tenant_mod.build_tenant_endpoint(ob_sys, cfg, inv, tenant_name)
    proc = ob_sys.run_sql(endpoint, password, "SELECT 1", ignore_error=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Нет SQL к тенанту {tenant_name} через "
            f"{endpoint['ip']}:{endpoint['port']} "
            f"({(proc.stderr or proc.stdout or '').strip()})"
        )
    return endpoint


def collect_snapshot(
    *,
    cfg: dict[str, Any],
    inv: dict[str, str],
    out_dir: Path,
    label: str,
    tenant_name: str | None,
    database: str,
    only: set[str] | None,
    skip_schema: bool,
    via: str,
    timeout: int,
    runner: Callable[..., Any] | None = None,
    ob_sys: Any | None = None,
    tenant_mod: Any | None = None,
    sys_endpoint: dict[str, Any] | None = None,
    sys_password: str = "",
    tenant_endpoint: dict[str, Any] | None = None,
    tenant_password: str = "",
) -> dict[str, Any]:
    ob_sys = ob_sys or _load_ob_sys()
    tenant_mod = tenant_mod or _load_tenant_create()
    queries = filter_queries(snapshot_queries(tenant_name, database), only, skip_schema)
    if sys_endpoint is None:
        sys_endpoint, sys_password = connect_sys(ob_sys, cfg, inv, via)
    if runner is None:
        def runner(endpoint: dict[str, Any], password: str, sql: str, timeout: int = 90) -> Any:
            return run_snapshot_sql(ob_sys, endpoint, password, sql, timeout=timeout)

    tenant_needed = any(q.scope == "tenant" for q in queries)
    tenant_error = ""
    if tenant_needed and tenant_endpoint is None and tenant_name:
        try:
            tenant_password = tenant_password or str(
                (cfg.get("tenant") or {}).get("root_password") or ""
            )
            tenant_endpoint = connect_tenant(
                tenant_mod, ob_sys, cfg, inv, tenant_name, tenant_password
            )
        except Exception as exc:  # noqa: BLE001
            tenant_error = str(exc)
            tenant_endpoint = None

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    summary: list[str] = [
        f"OceanBase TPC-C server snapshot  label={label}",
        f"captured_at={(datetime.now(timezone.utc).isoformat())}",
        f"tenant={tenant_name or '*'} database={database}",
        f"sys={sys_endpoint.get('ip')}:{sys_endpoint.get('port')} "
        f"via={sys_endpoint.get('via')} user={sys_endpoint.get('user')}",
        f"plan={PLAN_DOC}",
        "",
    ]
    failed_required = 0
    for index, query in enumerate(queries, start=1):
        prefix = f"{index:02d}-{query.query_id}"
        if query.scope == "tenant":
            if tenant_endpoint is None:
                status, sql_used, stdout, elapsed_ms = (
                    "skipped",
                    query.sqls[0],
                    tenant_error or "нет подключения к тенанту",
                    0,
                )
            else:
                status, sql_used, stdout, elapsed_ms = try_query(
                    runner, tenant_endpoint, tenant_password, query, timeout
                )
        else:
            status, sql_used, stdout, elapsed_ms = try_query(
                runner, sys_endpoint, sys_password, query, timeout
            )
        write_text(out_dir / f"{prefix}.sql", compact_sql(sql_used) + "\n")
        rows = 0
        if status == "ok":
            write_text(out_dir / f"{prefix}.tsv", stdout)
            rows = count_data_rows(stdout)
            body = stdout.rstrip() or "(пусто)"
        else:
            write_text(out_dir / f"{prefix}.err", stdout + "\n")
            body = stdout.rstrip() or status
            if query.required and status == "error":
                failed_required += 1
        summary.append(f"===== {query.query_id}: {query.title} [{status} {elapsed_ms}ms] =====")
        if query.note:
            summary.append(f"# {query.note}")
        summary.append(body)
        summary.append("")
        results.append(
            {
                "id": query.query_id,
                "topic": query.topic,
                "scope": query.scope,
                "required": query.required,
                "status": status,
                "rows": rows,
                "elapsed_ms": elapsed_ms,
            }
        )

    warnings: list[str] = []
    if tenant_error:
        warnings.append(f"tenant SQL недоступен: {tenant_error}")
    audit = next((r for r in results if r["id"] == "sql-audit-by-id"), None)
    if audit and audit["status"] == "ok" and audit["rows"] == 0:
        warnings.append(
            "GV$OB_SQL_AUDIT пуст — проверьте enable_sql_audit / ob_enable_sql_audit "
            "и что нагрузка шла в этот тенант"
        )
    manifest = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "cluster": (cfg.get("oceanbase") or {}).get("cluster_name"),
        "deploy_name": inv.get("DEPLOY_NAME"),
        "tenant": tenant_name,
        "database": database,
        "sys_endpoint": {
            "ip": sys_endpoint.get("ip"),
            "port": sys_endpoint.get("port"),
            "via": sys_endpoint.get("via"),
            "user": sys_endpoint.get("user"),
        },
        "plan": PLAN_DOC,
        "queries": results,
        "warnings": warnings,
        "failed_required": failed_required,
    }
    if warnings:
        summary.append("===== warnings =====")
        summary.extend(warnings)
        summary.append("")
    write_text(out_dir / "SUMMARY.txt", "\n".join(summary).rstrip() + "\n")
    write_text(out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def resolve_tenant(cfg: dict[str, Any], override: str | None) -> tuple[str, str]:
    tenant_mod = _load_tenant_create()
    resolved = tenant_mod.resolve_tenant_cfg(cfg)
    tenant_name = (override or resolved["tenant_name"]).strip()
    database = resolved["database"]
    return tenant_name, database


def cmd_list(args: argparse.Namespace) -> None:
    tenant = args.tenant or "tpcc"
    database = args.database or "tpcc"
    queries = snapshot_queries(tenant, database)
    print(f"{'id':<28} {'topic':<20} {'scope':<7} req  title")
    for query in queries:
        req = "yes" if query.required else "no"
        print(f"{query.query_id:<28} {query.topic:<20} {query.scope:<7} {req:<3}  {query.title}")
    print()
    print(f"{len(queries)} запросов, темы плана: {', '.join(REQUIRED_TOPICS)}")


def cmd_dump_sql(args: argparse.Namespace) -> None:
    tenant = args.tenant or "tpcc"
    database = args.database or "tpcc"
    text = render_sql_pack(tenant, database)
    if args.output:
        path = Path(args.output)
        write_text(path, text)
        print(path)
        return
    sys.stdout.write(text)


def cmd_print_sql(args: argparse.Namespace) -> None:
    tenant = args.tenant or "tpcc"
    database = args.database or "tpcc"
    found = queries_by_id(tenant, database).get(args.query_id)
    if found is None:
        raise RuntimeError(f"Нет запроса {args.query_id!r}. Смотрите: snapshot list")
    for index, sql in enumerate(found.sqls):
        if index:
            print(f"-- fallback {index}")
        print(compact_sql(sql) + ";")
        print()


def cmd_collect(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    if not inv:
        raise RuntimeError(f"Пустой inventory: {args.inventory}")
    tenant_name, database = resolve_tenant(cfg, args.tenant)
    if args.database:
        database = args.database
    if args.all_tenants:
        tenant_name = None
    only = {item.strip() for part in (args.only or []) for item in part.split(",") if item.strip()}
    label = args.label or "snapshot"
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = REPO_ROOT / "generated" / "snapshots" / snapshot_dirname(label)
    manifest = collect_snapshot(
        cfg=cfg,
        inv=inv,
        out_dir=out_dir,
        label=label,
        tenant_name=tenant_name,
        database=database,
        only=only or None,
        skip_schema=args.skip_schema,
        via=args.via,
        timeout=args.timeout,
        ob_sys=ob_sys,
    )
    print(f"snapshot: {out_dir}")
    print(f"SUMMARY:  {out_dir / 'SUMMARY.txt'}")
    for warning in manifest.get("warnings") or []:
        print(f"WARN: {warning}", file=sys.stderr)
    if manifest.get("failed_required"):
        raise RuntimeError(
            f"Не выполнились обязательные запросы: {manifest['failed_required']}. "
            "Смотрите *.err в каталоге снимка"
        )


def cmd_self_test(_args: argparse.Namespace) -> None:
    assert_catalog_safe()
    pack = render_sql_pack()
    assert "GV$OB_SQL_AUDIT" in pack
    assert "GV$OB_LOCK_WAIT_STAT" in pack
    sql_blob = "\n".join("\n".join(q.sqls) for q in snapshot_queries("tpcc", "tpcc"))
    assert "params_value" not in sql_blob.lower()
    assert "password" not in sql_blob.lower()
    assert snapshot_dirname("w45k06", datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)).endswith(
        "_w45k06"
    )
    queries = snapshot_queries("tpcc", "tpcc")
    subset = filter_queries(queries, {"sql_audit"}, skip_schema=True)
    assert subset and all(q.topic == "sql_audit" for q in subset)
    print("self-test ok")


def _add_io_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "deploy.yaml"))
    parser.add_argument(
        "--inventory", default=str(REPO_ROOT / "generated" / "inventory.env")
    )
    parser.add_argument("--tenant", default=None, help="Имя тенанта (по умолчанию tenant.tenant_name)")
    parser.add_argument("--database", default=None, help="TPC-C database (по умолчанию tenant.database)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Каталог запросов снимка")
    p_list.add_argument("--tenant", default="tpcc")
    p_list.add_argument("--database", default="tpcc")
    p_list.set_defaults(func=cmd_list)

    p_dump = sub.add_parser("dump-sql", help="Выгрузить SQL-пакет")
    p_dump.add_argument("--tenant", default="tpcc")
    p_dump.add_argument("--database", default="tpcc")
    p_dump.add_argument(
        "--output",
        default=None,
        help="Файл (по умолчанию stdout). Для репозитория: docs/sql/tpcc-server-snapshot-501.sql",
    )
    p_dump.set_defaults(func=cmd_dump_sql)

    p_print = sub.add_parser("print-sql", help="Печать одного запроса")
    p_print.add_argument("query_id")
    p_print.add_argument("--tenant", default="tpcc")
    p_print.add_argument("--database", default="tpcc")
    p_print.set_defaults(func=cmd_print_sql)

    p_collect = sub.add_parser("collect", help="Снять snapshot с живого кластера")
    _add_io_args(p_collect)
    p_collect.add_argument("--label", default="snapshot", help="Метка точки (w45k06, after-run, …)")
    p_collect.add_argument("--out-dir", default=None)
    p_collect.add_argument(
        "--only",
        action="append",
        default=None,
        help="id или topic через запятую (можно повторять флаг)",
    )
    p_collect.add_argument("--skip-schema", action="store_true")
    p_collect.add_argument("--all-tenants", action="store_true")
    p_collect.add_argument("--via", choices=("observer", "obproxy"), default="observer")
    p_collect.add_argument("--timeout", type=int, default=90)
    p_collect.set_defaults(func=cmd_collect)

    p_test = sub.add_parser("self-test", help="Локальные проверки без кластера")
    p_test.set_defaults(func=cmd_self_test)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        text = str(exc).strip() or type(exc).__name__
        print(f"ERROR: {text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
