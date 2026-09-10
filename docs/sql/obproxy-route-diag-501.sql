-- Диагностика «прибитых» сессий ODP на OceanBase 5.0.1.
-- Запускать как root@<user_tenant> (у вас root@tpcc) через obproxy:2883
-- и отдельно SHOW PROXYCONFIG с того же порта.
-- Подробности: docs/obproxy-session-routing.md

-- 1) Сессии: где сидят и чем заняты (Sleep после PREPARE ≠ горячий SQL)
SELECT svr_ip, command, COUNT(*) AS sess
FROM gv$ob_processlist
WHERE user LIKE 'tpcc%' OR info LIKE 'INSERT%'
GROUP BY svr_ip, command
ORDER BY sess DESC;

-- 2) Куда реально ходит SQL (5.0: GV$OB_SQL_AUDIT)
-- request_type: 5 = Prepare, 6 = Execute
-- plan_type:    1 = local, 2 = remote, 3 = distributed
-- partition_hit: 0 = координатор не попал в локальную партицию
SELECT svr_ip,
       SUM(request_type = 5) AS prepares,
       SUM(request_type = 6) AS executes,
       SUM(plan_type = 1)    AS local_plan,
       SUM(plan_type = 2)    AS remote_plan,
       SUM(plan_type = 3)    AS dist_plan,
       SUM(partition_hit = 0) AS part_miss,
       COUNT(*) AS stmts
FROM gv$ob_sql_audit
WHERE is_inner_sql = 0
GROUP BY svr_ip
ORDER BY stmts DESC;

-- 3) Что именно приземляется на горячий узел (подставьте IP)
SELECT LEFT(query_sql, 80) AS sql_head,
       request_type, plan_type, partition_hit, COUNT(*) AS n
FROM gv$ob_sql_audit
WHERE is_inner_sql = 0
  AND svr_ip = '10.130.0.11'
GROUP BY LEFT(query_sql, 80), request_type, plan_type, partition_hit
ORDER BY n DESC
LIMIT 30;

-- 4) Unit / tablet-лидеры (холодный хвост)
SELECT t.tenant_name, u.svr_ip, COUNT(*) AS units
FROM oceanbase.DBA_OB_UNITS u
JOIN oceanbase.DBA_OB_TENANTS t ON u.tenant_id = t.tenant_id
WHERE t.tenant_type = 'USER'
GROUP BY t.tenant_name, u.svr_ip
ORDER BY units, u.svr_ip;

SELECT svr_ip, COUNT(*) AS tablet_leaders
FROM oceanbase.DBA_OB_TABLE_LOCATIONS
WHERE role = 'LEADER'
GROUP BY svr_ip
ORDER BY tablet_leaders DESC;

-- 5) Тенант
SELECT tenant_name, primary_zone, locality
FROM oceanbase.DBA_OB_TENANTS
WHERE tenant_type = 'USER';
