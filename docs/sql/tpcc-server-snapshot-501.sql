-- Серверный снимок OceanBase 5.0.x для точки TPC-C (Phase 0.4).
-- План: https://github.com/zinal/portable-tpcc/blob/main/docs/oceanbase-efficiency-improvement-plan.md
-- Запускать как root@sys (observer:2881) для запросов scope=sys
-- и как root@tpcc для scope=tenant (USE tpcc).
-- Не включает bind-параметры / пароли / connection string.
-- Подставьте tenant/database при другом имени.
-- sql_audit: is_executor_rpc=0 и окно request_time 900s (0 = весь буфер).
-- Перегенерация: python3 scripts/lib/ob_snapshot.py dump-sql --output docs/sql/tpcc-server-snapshot-501.sql

-- ===== cluster-version: Версия и audit [sys] =====
-- Пустой GV$OB_SQL_AUDIT при enable_sql_audit=false.
SELECT 'version' AS name, @@version_comment AS value
UNION ALL SELECT 'version_compile', @@version
UNION ALL SELECT name, value
FROM oceanbase.GV$OB_PARAMETERS
WHERE name IN ('min_observer_version', 'enable_sql_audit', 'ob_enable_sql_audit')
GROUP BY name, value;

-- ===== servers: Состав кластера (DBA_OB_SERVERS) [sys] =====
SELECT svr_ip, svr_port, zone, status, start_service_time, stop_time, with_rootserver
FROM oceanbase.DBA_OB_SERVERS
ORDER BY zone, svr_ip;

-- ===== tenants: Тенанты и PRIMARY_ZONE [sys] =====
SELECT tenant_id, tenant_name, tenant_type, status, primary_zone, locality, compatibility_mode
FROM oceanbase.DBA_OB_TENANTS
ORDER BY tenant_id;

-- ===== sql-audit-by-id: GV$OB_SQL_AUDIT: sql_id / plan_id / server / ret_code / event [sys] =====
-- Агрегаты без query_sql/params. Окно request_time 900s (0 = весь буфер); is_executor_rpc=0.
SELECT sql_id, plan_id, svr_ip, ret_code, event, COUNT(*) AS executions, SUM(CASE WHEN ret_code <> 0 THEN 1 ELSE 0 END) AS errors, ROUND(AVG(elapsed_time)) AS avg_elapsed_us, ROUND(AVG(queue_time)) AS avg_queue_us, ROUND(AVG(execute_time)) AS avg_execute_us, SUM(return_rows) AS return_rows, SUM(affected_rows) AS affected_rows
FROM oceanbase.GV$OB_SQL_AUDIT
WHERE is_inner_sql = 0 AND is_executor_rpc = 0 AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') AND request_time > (time_to_usec(now()) - 900000000)
GROUP BY sql_id, plan_id, svr_ip, ret_code, event
ORDER BY executions DESC
LIMIT 200;

-- ===== sql-audit-heads: Топ SQL (обрезанный sql_head, без параметров) [sys] =====
SELECT sql_id, MIN(LEFT(query_sql, 120)) AS sql_head, COUNT(*) AS executions, SUM(CASE WHEN ret_code IN (1205, 6235, -6235, -6210, -4012, 4012, 600) THEN 1 ELSE 0 END) AS lock_or_serial, ROUND(AVG(elapsed_time)) AS avg_elapsed_us, MAX(elapsed_time) AS max_elapsed_us
FROM oceanbase.GV$OB_SQL_AUDIT
WHERE is_inner_sql = 0 AND is_executor_rpc = 0 AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') AND request_time > (time_to_usec(now()) - 900000000)
GROUP BY sql_id
ORDER BY executions DESC
LIMIT 80;

-- ===== sql-audit-errors: 1205 / 6235 и соседние ret_code по sql_id и sql_head [sys] =====
-- Класс блокирующей строки — в lock-waits rowkey, не в params.
SELECT ret_code, sql_id, plan_id, svr_ip, MIN(LEFT(query_sql, 80)) AS sql_head, COUNT(*) AS n, ROUND(AVG(elapsed_time)) AS avg_elapsed_us
FROM oceanbase.GV$OB_SQL_AUDIT
WHERE is_inner_sql = 0 AND is_executor_rpc = 0 AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') AND request_time > (time_to_usec(now()) - 900000000) AND ret_code IN (1205, 6235, -6235, -6210, -4012, 4012, 600)
GROUP BY ret_code, sql_id, plan_id, svr_ip
ORDER BY n DESC
LIMIT 100;

-- ===== local-remote-dist: Local / remote / distributed (plan_type) по observer [sys] =====
SELECT svr_ip, SUM(plan_type = 1) AS local_plan, SUM(plan_type = 2) AS remote_plan, SUM(plan_type = 3) AS dist_plan, SUM(partition_hit = 0) AS part_miss, SUM(partition_hit = 1) AS part_hit, COUNT(*) AS stmts
FROM oceanbase.GV$OB_SQL_AUDIT
WHERE is_inner_sql = 0 AND is_executor_rpc = 0 AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') AND request_time > (time_to_usec(now()) - 900000000)
GROUP BY svr_ip
ORDER BY stmts DESC;

-- ===== local-remote-dist-by-sql: Local / remote / dist по sql_id (ожидаемая доля remote New-Order/Payment) [sys] =====
SELECT sql_id, MIN(LEFT(query_sql, 80)) AS sql_head, SUM(plan_type = 1) AS local_plan, SUM(plan_type = 2) AS remote_plan, SUM(plan_type = 3) AS dist_plan, COUNT(*) AS stmts
FROM oceanbase.GV$OB_SQL_AUDIT
WHERE is_inner_sql = 0 AND is_executor_rpc = 0 AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') AND request_time > (time_to_usec(now()) - 900000000)
GROUP BY sql_id
ORDER BY dist_plan DESC, remote_plan DESC, stmts DESC
LIMIT 80;

-- ===== lock-waits: Lock wait: waiter / holder / rowkey [sys] =====
-- 5.0.x: нет GV$OB_LOCK_WAIT_STAT и колонки table_id; используется __all_virtual_lock_wait_stat / GV$OB_LOCKS.
SELECT tenant_id, svr_ip, tablet_id, LEFT(rowkey, 128) AS rowkey, session_id AS waiter_sid, block_session_id AS holder_sid, holder_tx_id, waiter_tx_id, lock_mode, type, try_lock_times, time_after_recv
FROM oceanbase.__all_virtual_lock_wait_stat
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY time_after_recv DESC
LIMIT 200;

-- ===== lock-waits-with-sql: Lock wait + waiter SQL из processlist (обрезанный info) [sys] =====
SELECT w.svr_ip, w.session_id AS waiter_sid, w.block_session_id AS holder_sid, LEFT(w.rowkey, 128) AS rowkey, w.tablet_id, w.time_after_recv, LEFT(p.info, 80) AS waiter_sql_head
FROM oceanbase.__all_virtual_lock_wait_stat w
LEFT JOIN oceanbase.GV$OB_PROCESSLIST p ON p.id = w.session_id AND p.svr_ip = w.svr_ip
WHERE w.tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY w.time_after_recv DESC
LIMIT 200;

-- ===== plan-cache-stat: Plan cache hit / miss по observer [sys] =====
SELECT tenant_id, svr_ip, access_count, hit_count, (access_count - hit_count) AS miss_count, ROUND(100 * hit_count / NULLIF(access_count, 0), 2) AS hit_pct, mem_used, mem_hold
FROM oceanbase.GV$OB_PLAN_CACHE_STAT
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY svr_ip;

-- ===== plan-cache-plans: Планы в кэше (first_load_time / schema_version — смена плана) [sys] =====
SELECT svr_ip, sql_id, plan_id, type, hit_count, executions, avg_exe_usec, first_load_time, last_active_time, schema_version
FROM oceanbase.GV$OB_PLAN_CACHE_PLAN_STAT
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY executions DESC
LIMIT 100;

-- ===== plan-changes: sql_id с несколькими plan_id (смена плана) [sys] =====
SELECT sql_id, COUNT(DISTINCT plan_id) AS plans, COUNT(DISTINCT svr_ip) AS servers, SUM(executions) AS executions
FROM oceanbase.GV$OB_PLAN_CACHE_PLAN_STAT
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
GROUP BY sql_id
HAVING COUNT(DISTINCT plan_id) > 1
ORDER BY plans DESC, executions DESC
LIMIT 50;

-- ===== units: Unit'ы тенанта: CPU / memory / max_session_num / observer [sys] =====
SELECT t.tenant_name, u.unit_id, u.svr_ip, u.zone, u.status, u.min_cpu, u.max_cpu, u.memory_size, u.max_memory, u.max_session_num
FROM oceanbase.DBA_OB_UNITS u
JOIN oceanbase.DBA_OB_TENANTS t ON u.tenant_id = t.tenant_id
WHERE u.tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY u.zone, u.svr_ip;

-- ===== tablet-leaders: Лидеры tablet по observer [sys] =====
SELECT svr_ip, COUNT(*) AS tablet_leaders
FROM oceanbase.DBA_OB_TABLE_LOCATIONS
WHERE role = 'LEADER' AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
GROUP BY svr_ip
ORDER BY tablet_leaders DESC;

-- ===== tablet-leaders-by-table: Лидеры tablet по таблице и observer [sys] =====
SELECT database_name, table_name, svr_ip, COUNT(*) AS tablet_leaders
FROM oceanbase.DBA_OB_TABLE_LOCATIONS
WHERE role = 'LEADER' AND tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
GROUP BY database_name, table_name, svr_ip
ORDER BY table_name, tablet_leaders DESC;

-- ===== sessions: Сессии тенанта по observer и command [sys] =====
SELECT tenant, svr_ip, command, COUNT(*) AS sessions
FROM oceanbase.GV$OB_PROCESSLIST
WHERE (tenant = 'tpcc' OR `user` LIKE 'tpcc%')
GROUP BY tenant, svr_ip, command
ORDER BY sessions DESC;

-- ===== memory: Память тенанта (GV$OB_MEMORY) [sys] =====
SELECT tenant_id, svr_ip, ctx_name, mod_name, hold, used
FROM oceanbase.GV$OB_MEMORY
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY hold DESC
LIMIT 80;

-- ===== memstore-freeze: Memstore / minor freeze [sys] =====
SELECT tenant_id, svr_ip, active_span, freeze_trigger, mem_limit, freeze_cnt
FROM oceanbase.GV$OB_MEMSTORE
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')
ORDER BY svr_ip;

-- ===== sysstat: CPU / RPC / IO / lock / throttle / freeze (GV$SYSSTAT) [sys] =====
SELECT con_id AS tenant_id, svr_ip, name, value
FROM oceanbase.GV$SYSSTAT
WHERE con_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') AND (name IN ('cpu usage', 'memory usage', 'sql execute count', 'trans commit count', 'trans rollback count', 'rpc packet in', 'rpc packet out', 'rpc packet in bytes', 'rpc packet out bytes', 'io read bytes', 'io write bytes', 'memstore used', 'memstore limit', 'clog disk used') OR name LIKE '%throttle%' OR name LIKE '%freeze%' OR name LIKE '%lock wait%' OR name LIKE '%rpc%' OR name LIKE '%cpu%')
ORDER BY name, svr_ip;

-- ===== compaction: Major compaction / throttling [sys] =====
SELECT tenant_id, frozen_scn, status, start_time, last_finish_time, is_error
FROM oceanbase.CDB_OB_MAJOR_COMPACTION
WHERE tenant_id = (SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc');

-- ===== log-stat: Лог-стримы (роль лидера / follower) — bandwidth в sysstat [sys] =====
SELECT svr_ip, role, COUNT(*) AS streams
FROM oceanbase.GV$OB_LOG_STAT
GROUP BY svr_ip, role
ORDER BY svr_ip, role;

-- ===== tenant-timeouts: Эффективные OLTP timeout тенанта (не bulk query_timeout профиля) [tenant] =====
-- План §3.6: worker не берёт database.options.query_timeout.
SHOW VARIABLES
WHERE Variable_name IN ('ob_query_timeout', 'ob_trx_timeout', 'ob_trx_idle_timeout', 'ob_trx_lock_timeout', 'innodb_lock_wait_timeout', 'ob_enable_sql_audit', 'autocommit', 'ob_read_consistency');

-- ===== schema-tables: Список таблиц TPC-C database [tenant] =====
SELECT table_name, table_type, engine, table_rows, create_options
FROM information_schema.tables
WHERE table_schema = 'tpcc'
ORDER BY table_name;

-- ===== schema-partitions: HASH partitions / binding (information_schema.partitions) [tenant] =====
SELECT table_name, partition_method, subpartition_method, partition_expression, COUNT(*) AS partition_count
FROM information_schema.partitions
WHERE table_schema = 'tpcc'
GROUP BY table_name, partition_method, subpartition_method, partition_expression
ORDER BY table_name;

-- ===== schema-tablegroups: Tablegroup / sharding (binding HASH) [tenant] =====
SELECT tablegroup_name, sharding, scope
FROM oceanbase.DBA_OB_TABLEGROUPS;

-- ===== schema-indexes: Индексы TPC-C [tenant] =====
SELECT table_name, index_name, non_unique, seq_in_index, column_name
FROM information_schema.statistics
WHERE table_schema = 'tpcc'
ORDER BY table_name, index_name, seq_in_index;

-- ===== schema-foreign-keys: FOREIGN KEY (в baseline плана должны быть выключены) [tenant] =====
SELECT table_name, constraint_name, constraint_type
FROM information_schema.table_constraints
WHERE table_schema = 'tpcc' AND constraint_type = 'FOREIGN KEY'
ORDER BY table_name, constraint_name;

-- ===== create-warehouse: SHOW CREATE TABLE tpcc.warehouse [tenant] =====
SHOW CREATE TABLE `tpcc`.`warehouse`;

-- ===== create-district: SHOW CREATE TABLE tpcc.district [tenant] =====
SHOW CREATE TABLE `tpcc`.`district`;

-- ===== create-customer: SHOW CREATE TABLE tpcc.customer [tenant] =====
SHOW CREATE TABLE `tpcc`.`customer`;

-- ===== create-history: SHOW CREATE TABLE tpcc.history [tenant] =====
SHOW CREATE TABLE `tpcc`.`history`;

-- ===== create-new_order: SHOW CREATE TABLE tpcc.new_order [tenant] =====
SHOW CREATE TABLE `tpcc`.`new_order`;

-- ===== create-oorder: SHOW CREATE TABLE tpcc.oorder [tenant] =====
SHOW CREATE TABLE `tpcc`.`oorder`;

-- ===== create-order_line: SHOW CREATE TABLE tpcc.order_line [tenant] =====
SHOW CREATE TABLE `tpcc`.`order_line`;

-- ===== create-stock: SHOW CREATE TABLE tpcc.stock [tenant] =====
SHOW CREATE TABLE `tpcc`.`stock`;

-- ===== create-item: SHOW CREATE TABLE tpcc.item [tenant] =====
SHOW CREATE TABLE `tpcc`.`item`;
