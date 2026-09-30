#!/usr/bin/env python3
"""Команды расширения кластера без OCP и OBD."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from ob_expand import (  # noqa: E402
    ExpandError,
    ObserverStart,
    ObproxyStart,
    bootstrap_sql,
    obproxy_rs_list,
    parse_ip,
    remote_observer_script,
    rootservice_list,
    server_endpoint,
    status_is_active,
)


RS = ("2001:db8:1::11", "2001:db8:1::12", "2001:db8:1::13")


def observer(**kwargs) -> ObserverStart:
    base = dict(
        ip="2001:db8:1::21",
        zone="zone1",
        roots=RS,
        appname="obcluster",
        cluster_id=1,
        home_path="/home/obadmin/observer",
        data_dir="/data/1",
        redo_dir="/data/log1",
    )
    base.update(kwargs)
    return ObserverStart(**base)


class ExpandAddressTest(unittest.TestCase):
    def test_ipv6_sql_endpoint_is_bracketed(self):
        self.assertEqual(server_endpoint("2001:db8:1::21", 2882), "[2001:db8:1::21]:2882")
        self.assertEqual(server_endpoint("[2001:db8:1::21]", 2882), "[2001:db8:1::21]:2882")

    def test_full_form_is_compressed_in_the_endpoint(self):
        endpoint = server_endpoint("2001:0db8:0001:0000:0000:0000:0000:0021", 2882)
        self.assertEqual(endpoint, "[2001:db8:1::21]:2882")

    def test_ipv4_stays_unbracketed(self):
        self.assertEqual(server_endpoint("10.0.0.8", 2882), "10.0.0.8:2882")

    def test_link_local_and_zone_index_are_rejected(self):
        with self.assertRaises(ExpandError):
            parse_ip("fe80::1")
        with self.assertRaises(ExpandError):
            parse_ip("2001:db8:1::21%eth0")

    def test_rootservice_list_uses_rpc_and_sql_ports(self):
        listed = rootservice_list(list(RS), 2882, 2881)
        self.assertEqual(
            listed,
            "[2001:db8:1::11]:2882:2881;[2001:db8:1::12]:2882:2881;[2001:db8:1::13]:2882:2881",
        )

    def test_obproxy_list_uses_sql_port_only(self):
        self.assertEqual(
            obproxy_rs_list(list(RS), 2881),
            "[2001:db8:1::11]:2881;[2001:db8:1::12]:2881;[2001:db8:1::13]:2881",
        )


class ExpandCommandTest(unittest.TestCase):
    def test_observer_flag_has_no_argument_and_opt_sets_use_ipv6(self):
        argv = observer().argv()
        self.assertIn("-6", argv)
        six = argv.index("-6")
        self.assertNotEqual(argv[six + 1], "True")
        self.assertNotEqual(argv[six + 1], "true")
        opt = argv[argv.index("-o") + 1]
        self.assertIn("use_ipv6=true", opt)
        rs = argv[argv.index("-r") + 1]
        self.assertNotIn("::21", rs)
        self.assertTrue(rs.startswith("["))

    def test_new_node_is_removed_from_its_own_rs_list(self):
        argv = observer(roots=RS + ("2001:db8:1::21",)).argv()
        rs = argv[argv.index("-r") + 1]
        self.assertNotIn("::21", rs)

    def test_ipv4_observer_does_not_pass_ipv6_mode(self):
        argv = observer(ip="10.1.0.8", roots=("10.1.0.1", "10.1.0.2", "10.1.0.3")).argv()
        self.assertNotIn("-6", argv)
        opt = argv[argv.index("-o") + 1]
        self.assertNotIn("use_ipv6", opt)
        self.assertEqual(argv[argv.index("-r") + 1], "10.1.0.1:2882:2881;10.1.0.2:2882:2881;10.1.0.3:2882:2881")

    def test_add_server_sql_quotes_ipv6(self):
        sql = observer().add_server_sql()
        self.assertIn("ALTER SYSTEM ADD SERVER '[2001:db8:1::21]:2882' ZONE 'zone1'", sql)
        self.assertIn("ob_query_timeout = 3600000000", sql)

    def test_rs_cannot_be_only_the_new_node(self):
        with self.assertRaises(ExpandError):
            observer(roots=("2001:db8:1::21",)).argv()

    def test_remote_script_is_valid_bash_and_wipes_only_named_dirs(self):
        script = remote_observer_script(observer(wipe=True))
        self.assertIn('find "$DATA_DIR"', script)
        self.assertNotIn("pkill", script)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    def test_obproxy_binds_ipv6_and_haproxy_line_is_bracketed(self):
        spec = ObproxyStart(
            ip="2001:db8:1::31",
            roots=RS,
            appname="obcluster",
            home_path="/home/obadmin/obproxy",
        )
        argv = spec.argv()
        self.assertEqual(argv[argv.index("--rs_list") + 1].split(";")[0], "[2001:db8:1::11]:2881")
        self.assertIn("local_bound_ipv6_ip=2001:db8:1::31", argv[-1])
        self.assertEqual(spec.haproxy_server_line("obp4"), "server obp4 [2001:db8:1::31]:2883 check")

    def test_status_matches_compressed_and_full_form(self):
        rows = "2001:0db8:0001:0000:0000:0000:0000:0021\tACTIVE\n10.0.0.1 ACTIVE\n"
        self.assertTrue(status_is_active(rows, "2001:db8:1::21"))
        self.assertFalse(status_is_active("2001:db8:1::21\tINACTIVE\n", "2001:db8:1::21"))


class ExpandCliTest(unittest.TestCase):
    def test_print_mode_does_not_invoke_ssh(self):
        script = ROOT / "scripts" / "21-expand-observer.sh"
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--print",
                "--ip",
                "2001:db8:1::21",
                "--zone",
                "zone2",
                "--rs",
                "2001:db8:1::11,2001:db8:1::12",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("-6", proc.stdout)
        self.assertIn("use_ipv6=true", proc.stdout)
        self.assertIn("ADD SERVER '[2001:db8:1::21]:2882' ZONE 'zone2'", proc.stdout)
        self.assertNotIn("obd ", proc.stdout)
        proxy = ROOT / "scripts" / "22-expand-obproxy.sh"
        printed = subprocess.run(
            ["bash", str(proxy), "--ip", "2001:db8:1::31", "--rs", "2001:db8:1::11", "--name", "obp4"],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("local_bound_ipv6_ip=2001:db8:1::31", printed.stdout)
        self.assertIn("server obp4 [2001:db8:1::31]:2883 check", printed.stdout)


class BootstrapTest(unittest.TestCase):
    def _start(self, ip: str, zone: str, **kwargs) -> ObserverStart:
        base = dict(
            ip=ip,
            zone=zone,
            roots=("2001:db8:1::11", "2001:db8:1::12", "2001:db8:1::13"),
            appname="obcluster",
            cluster_id=1,
            home_path="/home/obadmin/observer",
            data_dir="/data/1",
            redo_dir="/data/log1",
            bootstrap=True,
        )
        base.update(kwargs)
        return ObserverStart(**base)

    def test_bootstrap_rs_includes_every_node(self):
        argv = self._start("2001:db8:1::12", "zone2").argv()
        rs = argv[argv.index("-r") + 1]
        self.assertEqual(
            rs,
            "[2001:db8:1::11]:2882:2881;[2001:db8:1::12]:2882:2881;[2001:db8:1::13]:2882:2881",
        )
        self.assertIn("-6", argv)
        self.assertIn("use_ipv6=true", argv[argv.index("-o") + 1])

    def test_bootstrap_sql_uses_rpc_port_and_brackets(self):
        sql = bootstrap_sql(
            [
                ("zone1", "2001:db8:1::11"),
                ("zone2", "2001:db8:1::12"),
                ("zone3", "2001:db8:1::13"),
            ]
        )
        self.assertIn(
            "ALTER SYSTEM BOOTSTRAP "
            "ZONE 'zone1' SERVER '[2001:db8:1::11]:2882', "
            "ZONE 'zone2' SERVER '[2001:db8:1::12]:2882', "
            "ZONE 'zone3' SERVER '[2001:db8:1::13]:2882'",
            sql,
        )

    def test_bootstrap_rejects_mixed_family_and_duplicates(self):
        with self.assertRaises(ExpandError):
            bootstrap_sql([("zone1", "10.0.0.1"), ("zone2", "2001:db8:1::12"), ("zone3", "2001:db8:1::13")])
        with self.assertRaises(ExpandError):
            bootstrap_sql(
                [("zone1", "2001:db8:1::11"), ("zone1", "2001:db8:1::12"), ("zone3", "2001:db8:1::13")]
            )
        with self.assertRaises(ExpandError):
            self._start("2001:db8:1::11", "zone1", roots=("2001:db8:1::11", "10.0.0.2", "10.0.0.3")).argv()

    def test_print_shows_three_starts_and_bootstrap_sql(self):
        script = ROOT / "scripts" / "23-bootstrap-cluster.sh"
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--zone1",
                "2001:db8:1::11",
                "--zone2",
                "2001:db8:1::12",
                "--zone3",
                "2001:db8:1::13",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.stdout.count("не выполнен"), 3)
        self.assertIn("ALTER SYSTEM BOOTSTRAP", proc.stdout)
        self.assertIn("-z zone1", proc.stdout)
        self.assertIn("-z zone2", proc.stdout)
        self.assertIn("-z zone3", proc.stdout)
        self.assertNotIn("obd ", proc.stdout)
        self.assertNotIn("ADD SERVER", proc.stdout)


if __name__ == "__main__":
    unittest.main()
