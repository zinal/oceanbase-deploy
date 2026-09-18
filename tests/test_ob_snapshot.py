#!/usr/bin/env python3
"""Тесты серверного снимка TPC-C (без кластера)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location(
    "ob_snapshot", ROOT / "scripts" / "lib" / "ob_snapshot.py"
)
snap = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["ob_snapshot"] = snap
spec.loader.exec_module(snap)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_catalog_covers_phase_04() -> None:
    snap.assert_catalog_safe()
    queries = snap.snapshot_queries("tpcc", "tpcc")
    by_id = {q.query_id: q for q in queries}
    assert "sql-audit-by-id" in by_id
    assert "lock-waits" in by_id
    assert "plan-cache-stat" in by_id
    assert "local-remote-dist" in by_id
    assert "tablet-leaders" in by_id
    assert "units" in by_id
    assert "sessions" in by_id
    assert "sysstat" in by_id
    assert "memstore-freeze" in by_id
    assert "tenant-timeouts" in by_id
    for table in snap.TPCC_TABLES:
        assert f"create-{table}" in by_id
    blob = "\n".join("\n".join(q.sqls) for q in queries).lower()
    assert "params_value" not in blob
    assert "password" not in blob
    assert "access_key" not in blob
    assert "group by sql_id, plan_id, svr_ip, ret_code, event" in blob
    assert "gv$ob_lock_wait_stat" in blob
    assert "holder_tx_id" in blob
    assert "rowkey" in blob
    assert "hit_count" in blob
    assert "plan_type = 1" in blob
    assert "plan_type = 2" in blob
    assert "plan_type = 3" in blob
    assert "dba_ob_table_locations" in blob
    assert "dba_ob_units" in blob
    assert "throttle" in blob
    assert "freeze" in blob
    assert "ob_query_timeout" in blob
    assert "show create table" in blob
    assert "time_to_usec" in blob
    assert "is_executor_rpc = 0" in blob
    assert "con_id" in blob
    assert "dba_ob_tablegroup_tables" in blob
    assert "mod_name" in blob


def test_pretty_sql_keeps_subquery() -> None:
    sql = (
        "SELECT sql_id FROM oceanbase.GV$OB_SQL_AUDIT "
        "WHERE is_inner_sql = 0 AND tenant_id = "
        "(SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc') "
        "GROUP BY sql_id ORDER BY sql_id LIMIT 10"
    )
    pretty = snap.pretty_sql(sql)
    assert "\nFROM oceanbase.GV$OB_SQL_AUDIT" in pretty
    assert "\nWHERE is_inner_sql" in pretty
    assert "(SELECT tenant_id FROM oceanbase.DBA_OB_TENANTS WHERE tenant_name = 'tpcc')" in pretty
    assert pretty.count("\nFROM ") == 1


def test_sql_pack_matches_docs() -> None:
    pack = snap.render_sql_pack("tpcc", "tpcc")
    path = ROOT / "docs" / "sql" / "tpcc-server-snapshot-501.sql"
    assert path.is_file()
    disk = path.read_text(encoding="utf-8")
    assert disk == pack
    assert "GV$OB_SQL_AUDIT" in disk
    assert "__all_virtual_lock_wait_stat" in disk
    assert "SHOW CREATE TABLE `tpcc`.`warehouse`" in disk
    assert "params_value" not in disk.lower()


def test_tenant_predicate_and_filter() -> None:
    named = snap.pred_tenant_id("tpcc")
    assert "tenant_name = 'tpcc'" in named
    all_user = snap.pred_tenant_id(None)
    assert "tenant_type = 'USER'" in all_user
    quoted = snap.sql_literal("o'brien")
    assert quoted == "'o''brien'"
    assert "tenant_name = 'tpcc'" in snap.pred_tenant_name("tpcc")
    windowed = snap.pred_sql_audit("tpcc", 900)
    assert "is_executor_rpc = 0" in windowed
    assert "time_to_usec(now()) - 900000000" in windowed
    assert "time_to_usec" not in snap.pred_sql_audit("tpcc", 0)
    queries = snap.snapshot_queries("tpcc", "tpcc")
    only_audit = snap.filter_queries(queries, {"sql_audit"}, skip_schema=True)
    assert only_audit and all(q.topic == "sql_audit" for q in only_audit)
    no_schema = snap.filter_queries(queries, None, skip_schema=True)
    assert all(q.topic != "schema" for q in no_schema)
    try:
        snap.filter_queries(queries, {"no-such-query"}, skip_schema=False)
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass


def test_collect_writes_artifacts(tmp_path: Path | None = None) -> None:
    out = (tmp_path or (ROOT / "generated" / "snapshots" / "test-ob-snapshot"))
    if tmp_path is None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        out = Path(tmp.name) / "snap"
    else:
        tmp = None
    calls: list[str] = []

    def runner(endpoint, password, sql, timeout=90):  # noqa: ANN001
        calls.append(sql)
        assert password != "should-not-leak-into-sql"
        if "GV$OB_SQL_AUDIT" in sql and "GROUP BY sql_id, plan_id" in sql:
            return FakeProc(0, "sql_id\tplan_id\tsvr_ip\tret_code\tevent\texecutions\n1\t2\t10.0.0.1\t0\tNULL\t9\n")
        if "NO_SUCH_VIEW_FORCE_FALLBACK" in sql:
            return FakeProc(1, "", "unknown")
        return FakeProc(0, "name\tvalue\nok\t1\n")

    cfg = {
        "oceanbase": {"cluster_name": "obcluster", "ports": {"mysql": 2881, "obproxy": 2883}},
        "tenant": {"tenant_name": "tpcc", "database": "tpcc", "root_password": "secret"},
    }
    inv = {"DEPLOY_NAME": "ob-yc-prod", "OBSERVER_COUNT": "1", "OBSERVER_1_IP": "10.0.0.1"}
    sys_ep = {"ip": "10.0.0.1", "port": 2881, "user": "root", "via": "observer"}
    tenant_ep = {"ip": "10.0.0.1", "port": 2883, "user": "root@tpcc#obcluster", "via": "obproxy"}
    manifest = snap.collect_snapshot(
        cfg=cfg,
        inv=inv,
        out_dir=out,
        label="w45k06",
        tenant_name="tpcc",
        database="tpcc",
        only={"sql-audit-by-id", "lock-waits", "tenant-timeouts"},
        skip_schema=True,
        via="observer",
        timeout=5,
        runner=runner,
        ob_sys=SimpleNamespace(),
        tenant_mod=SimpleNamespace(),
        sys_endpoint=sys_ep,
        sys_password="sys-pw",
        tenant_endpoint=tenant_ep,
        tenant_password="tenant-pw",
    )
    assert (out / "manifest.json").is_file()
    assert (out / "SUMMARY.txt").is_file()
    data = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    dumped = json.dumps(data)
    assert data["label"] == "w45k06"
    assert "sys-pw" not in dumped
    assert "tenant-pw" not in dumped
    assert "secret" not in dumped
    assert data["sys_endpoint"]["ip"] == "10.0.0.1"
    statuses = {row["id"]: row["status"] for row in data["queries"]}
    assert statuses["sql-audit-by-id"] == "ok"
    assert statuses["lock-waits"] == "ok"
    assert statuses["tenant-timeouts"] == "ok"
    audit_tsv = next(out.glob("*sql-audit-by-id.tsv")).read_text(encoding="utf-8")
    assert "executions" in audit_tsv
    assert not list(out.glob("*.err"))
    assert calls
    if tmp is not None:
        tmp.cleanup()
    return manifest


def test_try_query_fallback() -> None:
    query = snap.SnapshotQuery(
        query_id="demo",
        title="demo",
        topic="sql_audit",
        scope="sys",
        required=True,
        sqls=("SELECT 1 FROM missing_view", "SELECT 2 FROM ok_view"),
    )

    def runner(endpoint, password, sql, timeout=90):  # noqa: ANN001
        if "missing_view" in sql:
            return FakeProc(1, "", "table not found")
        return FakeProc(0, "x\n1\n")

    status, sql_used, stdout, elapsed_ms = snap.try_query(
        runner, {"ip": "10.0.0.1"}, "pw", query, 5
    )
    assert status == "ok"
    assert "ok_view" in sql_used
    assert "1" in stdout
    assert elapsed_ms >= 0


def test_sql_with_session_timeout() -> None:
    wrapped = snap.sql_with_session_timeout("SELECT 1 FROM dual", 90)
    assert "SET SESSION ob_query_timeout = 90000000" in wrapped
    assert "SET SESSION ob_trx_timeout = 90000000" in wrapped
    assert wrapped.strip().endswith("SELECT 1 FROM dual;")
    assert snap.sql_with_session_timeout("SELECT 1;", 0).startswith(
        "SET SESSION ob_query_timeout = 1000000;"
    )


def test_try_query_stops_on_timeout() -> None:
    calls: list[str] = []
    query = snap.SnapshotQuery(
        query_id="demo",
        title="demo",
        topic="sql_audit",
        scope="sys",
        required=True,
        sqls=("SELECT 1 FROM gv$hang", "SELECT 2 FROM fallback"),
    )

    def runner(endpoint, password, sql, timeout=90):  # noqa: ANN001
        calls.append(sql)
        raise subprocess.TimeoutExpired(cmd="obclient", timeout=timeout)

    status, sql_used, stdout, elapsed_ms = snap.try_query(
        runner, {"ip": "10.0.0.1"}, "pw", query, 7
    )
    assert status == "error"
    assert "gv$hang" in sql_used
    assert stdout == "timeout after 7s"
    assert elapsed_ms >= 0
    assert calls == ["SELECT 1 FROM gv$hang"]


def test_run_snapshot_sql_closes_stdin() -> None:
    reader = [
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); sys.stdout.write('eof=' + str(len(data)))",
    ]
    ob_sys = SimpleNamespace(_client_bin=lambda: reader)
    proc = snap.run_snapshot_sql(
        ob_sys,
        {"ip": "127.0.0.1", "port": 2881, "user": "root"},
        "pw",
        "SELECT 1",
        timeout=5,
    )
    assert proc.returncode == 0
    assert "eof=0" in (proc.stdout or "")
    assert "SET SESSION ob_query_timeout" in " ".join(proc.args)


def test_run_snapshot_sql_enforces_timeout() -> None:
    hung = [
        sys.executable,
        "-c",
        "import time, sys; time.sleep(30); sys.stdout.write('late\\n')",
    ]
    ob_sys = SimpleNamespace(_client_bin=lambda: hung)
    started = time.monotonic()
    try:
        snap.run_snapshot_sql(
            ob_sys,
            {"ip": "127.0.0.1", "port": 2881, "user": "root"},
            "",
            "SELECT 1",
            timeout=1,
        )
        raise AssertionError("ожидали TimeoutExpired")
    except subprocess.TimeoutExpired:
        pass
    elapsed = time.monotonic() - started
    assert elapsed < 10


def test_wrapper_and_deploy_sh() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "18-ob-snapshot.sh" in deploy
    assert "snapshot)" in deploy
    wrapper = ROOT / "scripts" / "18-ob-snapshot.sh"
    assert wrapper.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "ob_snapshot.py" in text
    out = subprocess.run(
        ["bash", str(wrapper), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "collect" in out.stdout
    assert "GV$OB_SQL_AUDIT" in out.stdout or "sql_audit" in out.stdout
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lib" / "ob_snapshot.py"), "self-test"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "self-test ok" in proc.stdout
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "18-ob-snapshot.sh" in readme
    assert "snapshot" in readme
    docs = (ROOT / "docs" / "tpcc-server-snapshot.md").read_text(encoding="utf-8")
    assert "Phase 0.4" in docs
    assert "deploy.sh snapshot collect" in docs


def test_self_test() -> None:
    snap.cmd_self_test(SimpleNamespace())


if __name__ == "__main__":
    test_catalog_covers_phase_04()
    test_pretty_sql_keeps_subquery()
    test_sql_pack_matches_docs()
    test_tenant_predicate_and_filter()
    test_collect_writes_artifacts()
    test_try_query_fallback()
    test_sql_with_session_timeout()
    test_try_query_stops_on_timeout()
    test_run_snapshot_sql_closes_stdin()
    test_run_snapshot_sql_enforces_timeout()
    test_wrapper_and_deploy_sh()
    test_self_test()
    print("ok")
