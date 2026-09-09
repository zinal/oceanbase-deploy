#!/usr/bin/env python3
"""Версия oceanbase-ce для OBD и проверка пакетов в зеркалах.

`obd cluster deploy` не принимает `-V`: версия задаётся в YAML компонента
(`oceanbase-ce.version`). All-in-One по умолчанию отключает remote, поэтому
пустой `oceanbase.version` ставит latest из local — после All-in-One 4.6
это 4.6.0, а не 5.0.1.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

OCEANBASE_CE = "oceanbase-ce"
DEFAULT_NEW_CLUSTER_VERSION = "5.0.1.0"
ALL_IN_ONE_INSTALLER = (
    "https://obbusiness-private.oss-cn-shanghai.aliyuncs.com/"
    "download-center/opensource/oceanbase-all-in-one/installer.sh"
)
ALL_IN_ONE_501_NOTES = "https://www.oceanbase.com/product/oceanbase-all-in-one-rn/releaseNote#V5.0.x"
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_PLUGIN_DIR_RE = re.compile(r"^\d+(?:\.\d+)*$")

PLUGIN_SEARCH_PATHS = (
    Path.home() / ".obd" / "plugins" / OCEANBASE_CE,
    Path.home() / ".oceanbase-all-in-one" / "obd" / "usr" / "obd" / "plugins" / OCEANBASE_CE,
    Path("/usr/obd/plugins") / OCEANBASE_CE,
)

YUM_STABLE = "https://mirrors.oceanbase.com/community/stable"
GITHUB_CE_RELEASE = "https://github.com/oceanbase/oceanbase/releases/download/v5.0.1_CE"
# RPM на GitHub / yum (не путать с номером в release notes 100000012026072916).
RPM_SPECS: dict[str, dict[str, Any]] = {
    "5.0.1.0": {
        "release": "100000042026072912",
        "packages": ("oceanbase-ce", "oceanbase-ce-libs"),
        "ob_deploy": "ob-deploy-4.6.0-16",
    },
}


def normalize_ob_version(value: Any) -> str:
    """Вернуть канонический номер (4 части: 5.0.1 → 5.0.1.0) или пустую строку."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "null":
        return ""
    match = _VERSION_RE.match(text)
    if not match:
        return text
    parts = match.group(1).split(".")
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts)


def version_matches(requested: str, available: str) -> bool:
    """5.0.1 совпадает с 5.0.1.0; 5.0.1 не совпадает с 5.0.10."""
    req = normalize_ob_version(requested)
    av = normalize_ob_version(available)
    if not req or not av:
        return False
    if av == req:
        return True
    if av.startswith(req + ".") or req.startswith(av + "."):
        return True
    req_raw = _VERSION_RE.match(str(requested).strip())
    av_raw = _VERSION_RE.match(str(available).strip())
    if req_raw and av_raw:
        r, a = req_raw.group(1), av_raw.group(1)
        if a == r or a.startswith(r + ".") or r.startswith(a + "."):
            return True
    return False


def yaml_truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text or text == "null":
        return default
    return text in {"1", "true", "yes", "on"}


def requested_oceanbase_version(cfg: dict[str, Any] | None) -> str:
    ob = (cfg or {}).get("oceanbase") or {}
    if not isinstance(ob, dict):
        return ""
    return normalize_ob_version(ob.get("version"))


def enable_remote_mirror(cfg: dict[str, Any] | None) -> bool:
    ob = (cfg or {}).get("oceanbase") or {}
    if not isinstance(ob, dict):
        return True
    return yaml_truthy(ob.get("enable_remote_mirror"), default=True)


def load_deploy_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required: pip install pyyaml")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def parse_obd_table_rows(text: str) -> list[dict[str, str]]:
    """Разобрать ASCII-таблицы `obd mirror list` / `obd mirror list local`."""
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not any(cells):
            continue
        joined = "".join(cells)
        if set(joined) <= set("-+ "):
            continue
        lower = [c.lower() for c in cells]
        if "name" in lower and "version" in lower:
            headers = lower
            continue
        if "sectionname" in lower or (lower and lower[0] == "sectionname"):
            headers = lower
            continue
        if headers is None:
            continue
        if cells[0].lower() in {"name", "sectionname"}:
            continue
        row: dict[str, str] = {}
        for idx, header in enumerate(headers):
            if idx < len(cells):
                row[header] = cells[idx]
        rows.append(row)
    return rows


def package_versions(rows: Iterable[dict[str, str]], component: str = OCEANBASE_CE) -> list[str]:
    found: list[str] = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if name != component:
            continue
        version = (row.get("version") or "").strip()
        if version:
            found.append(version)
    return found


def has_package(
    rows: Iterable[dict[str, str]],
    requested: str,
    *,
    component: str = OCEANBASE_CE,
) -> bool:
    req = normalize_ob_version(requested)
    if not req:
        return True
    return any(version_matches(req, ver) for ver in package_versions(rows, component))


def remote_mirrors_enabled(rows: Iterable[dict[str, str]]) -> bool:
    for row in rows:
        kind = (row.get("type") or "").strip().lower()
        if kind != "remote":
            continue
        enabled = (row.get("enabled") or "").strip().lower()
        if enabled in {"true", "yes", "1"}:
            return True
    return False


_EL_RE = re.compile(r"\.el(\d+)\b")


def local_rpm_platforms(
    rows: Iterable[dict[str, str]],
    *,
    component: str = OCEANBASE_CE,
) -> list[tuple[str, str]]:
    """Семейство RPM из local: release `….el7` и arch `x86_64`.

    All-in-One на Ubuntu всё равно кладёт el7, поэтому нельзя брать el8 с хоста.
    """
    seen: list[tuple[str, str]] = []
    for row in rows:
        if (row.get("name") or "").strip() != component:
            continue
        match = _EL_RE.search(row.get("release") or "")
        el = match.group(1) if match else ""
        arch = (row.get("arch") or "").strip()
        if not el or not arch:
            continue
        pair = (el, arch)
        if pair not in seen:
            seen.append(pair)
    return seen


def resolve_rpm_platforms(
    rows: Iterable[dict[str, str]],
    *,
    fallback_arch: str = "x86_64",
) -> list[tuple[str, str]]:
    found = local_rpm_platforms(rows)
    if found:
        return found
    # Пустой local: All-in-One чаще el7, yum/GitHub есть и el7, и el8.
    return [("7", fallback_arch), ("8", fallback_arch)]


def plugin_versions_in(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    versions: list[str] = []
    for child in path.iterdir():
        if child.is_dir() and _PLUGIN_DIR_RE.match(child.name):
            versions.append(child.name)
    return versions


def obd_plugin_roots() -> list[Path]:
    roots = list(PLUGIN_SEARCH_PATHS)
    obd_bin = shutil.which("obd")
    if obd_bin:
        parent = Path(obd_bin).resolve().parent
        for cand in (
            parent.parent / "obd" / "plugins" / OCEANBASE_CE,
            parent.parent / "usr" / "obd" / "plugins" / OCEANBASE_CE,
            parent / "plugins" / OCEANBASE_CE,
        ):
            if cand not in roots:
                roots.append(cand)
    return roots


def discover_plugin_versions(extra_roots: Iterable[Path] | None = None) -> list[str]:
    seen: list[str] = []
    roots = obd_plugin_roots()
    if extra_roots:
        roots.extend(extra_roots)
    for path in roots:
        for ver in plugin_versions_in(path):
            if ver not in seen:
                seen.append(ver)
    return seen


def preferred_plugin_dest() -> Path:
    for path in obd_plugin_roots():
        if path.is_dir() and os.access(path, os.W_OK):
            return path
    dest = Path.home() / ".obd" / "plugins" / OCEANBASE_CE
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def rpm_spec_for(version: str) -> dict[str, Any] | None:
    return RPM_SPECS.get(normalize_ob_version(version))


def yum_rpm_url(package: str, version: str, release: str, el: str, arch: str) -> str:
    filename = f"{package}-{version}-{release}.el{el}.{arch}.rpm"
    return f"{YUM_STABLE}/el/{el}/{arch}/{filename}"


def github_rpm_url(package: str, version: str, release: str, el: str, arch: str) -> str:
    filename = f"{package}-{version}-{release}.el{el}.{arch}.rpm"
    return f"{GITHUB_CE_RELEASE}/{filename}"


def package_rpm_urls(version: str, *, el: str = "8", arch: str = "x86_64") -> list[str]:
    spec = rpm_spec_for(version)
    if not spec:
        return []
    ver = normalize_ob_version(version)
    release = str(spec["release"])
    urls: list[str] = []
    for package in spec["packages"]:
        urls.append(yum_rpm_url(str(package), ver, release, el, arch))
        urls.append(github_rpm_url(str(package), ver, release, el, arch))
    return urls


def ob_deploy_rpm_url(*, el: str = "8", arch: str = "x86_64", version: str = "") -> str:
    spec = rpm_spec_for(version or DEFAULT_NEW_CLUSTER_VERSION)
    if not spec:
        return ""
    name = str(spec["ob_deploy"])
    return f"{YUM_STABLE}/el/{el}/{arch}/{name}.el{el}.{arch}.rpm"


def plugin_covers_version(plugin_versions: Iterable[str], requested: str) -> bool:
    """OBD берёт наибольший плагин ≤ версии пакета. Для 5.x нужен плагин 5.x."""
    req = normalize_ob_version(requested)
    if not req:
        return True
    req_major = req.split(".")[0]
    plugins = [normalize_ob_version(p) for p in plugin_versions if normalize_ob_version(p)]
    if not plugins:
        return False
    return any(p.split(".")[0] == req_major for p in plugins)


def missing_package_hint(requested: str) -> str:
    ver = normalize_ob_version(requested) or DEFAULT_NEW_CLUSTER_VERSION
    return f"""Не удалось получить {OCEANBASE_CE} {ver} в зеркалах OBD.

`./scripts/deploy.sh obd-mirror` должен сам:
  1. включить remote (`obd mirror enable remote && obd mirror update`);
  2. скачать RPM с mirrors.oceanbase.com / GitHub и сделать `obd mirror clone`;
  3. при отсутствии плагина 5.x — `obd update` и, если нужно, плагины из ob-deploy RPM.

Проверьте доступ с инсталляционного хоста к:
  {YUM_STABLE}
  {GITHUB_CE_RELEASE}

Если хост офлайн: скачайте All-in-One 5.0.1 ({ALL_IN_ONE_501_NOTES}) и:
  tar -xzf oceanbase-all-in-one-5.0.1_*.tar.gz
  cd oceanbase-all-in-one/bin && ./install.sh
  source ~/.oceanbase-all-in-one/bin/env.sh
  obd mirror clone ../rpms/*.rpm
"""


def _print(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def cmd_requested(args: argparse.Namespace) -> int:
    cfg = load_deploy_config(Path(args.config)) if args.config else {}
    _print(requested_oceanbase_version(cfg))
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    _print(normalize_ob_version(args.value))
    return 0


def cmd_has_package(args: argparse.Namespace) -> int:
    text = Path(args.mirror_file).read_text(encoding="utf-8") if args.mirror_file else sys.stdin.read()
    rows = parse_obd_table_rows(text)
    ok = has_package(rows, args.version, component=args.component)
    _print("yes" if ok else "no")
    return 0 if ok else 1


def cmd_remote_enabled(args: argparse.Namespace) -> int:
    text = Path(args.mirror_file).read_text(encoding="utf-8") if args.mirror_file else sys.stdin.read()
    rows = parse_obd_table_rows(text)
    ok = remote_mirrors_enabled(rows)
    _print("yes" if ok else "no")
    return 0 if ok else 1


def cmd_list_versions(args: argparse.Namespace) -> int:
    text = Path(args.mirror_file).read_text(encoding="utf-8") if args.mirror_file else sys.stdin.read()
    rows = parse_obd_table_rows(text)
    versions = package_versions(rows, args.component)
    _print(" ".join(versions))
    return 0


def cmd_plugin_covers(args: argparse.Namespace) -> int:
    plugins = discover_plugin_versions()
    if args.plugin_dir:
        plugins = plugin_versions_in(Path(args.plugin_dir))
    if not plugins:
        _print("unknown")
        return 2
    ok = plugin_covers_version(plugins, args.version)
    _print("yes" if ok else "no")
    return 0 if ok else 1


def cmd_hint(args: argparse.Namespace) -> int:
    _print(missing_package_hint(args.version))
    return 0


def cmd_rpm_urls(args: argparse.Namespace) -> int:
    urls = package_rpm_urls(args.version, el=args.el, arch=args.arch)
    if not urls:
        return 1
    for url in urls:
        _print(url)
    return 0


def cmd_ob_deploy_url(args: argparse.Namespace) -> int:
    url = ob_deploy_rpm_url(el=args.el, arch=args.arch, version=args.version)
    if not url:
        return 1
    _print(url)
    return 0


def cmd_plugin_dest(args: argparse.Namespace) -> int:
    _print(str(preferred_plugin_dest()))
    return 0


def cmd_rpm_platform(args: argparse.Namespace) -> int:
    text = Path(args.mirror_file).read_text(encoding="utf-8") if args.mirror_file else sys.stdin.read()
    rows = parse_obd_table_rows(text)
    for el, arch in resolve_rpm_platforms(rows, fallback_arch=args.arch):
        _print(f"{el} {arch}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    req = sub.add_parser("requested", help="oceanbase.version из deploy.yaml")
    req.add_argument("--config", required=True)
    req.set_defaults(func=cmd_requested)

    norm = sub.add_parser("normalize")
    norm.add_argument("value")
    norm.set_defaults(func=cmd_normalize)

    hasp = sub.add_parser("has-package")
    hasp.add_argument("--version", required=True)
    hasp.add_argument("--component", default=OCEANBASE_CE)
    hasp.add_argument("--mirror-file")
    hasp.set_defaults(func=cmd_has_package)

    rem = sub.add_parser("remote-enabled")
    rem.add_argument("--mirror-file")
    rem.set_defaults(func=cmd_remote_enabled)

    lst = sub.add_parser("list-versions")
    lst.add_argument("--component", default=OCEANBASE_CE)
    lst.add_argument("--mirror-file")
    lst.set_defaults(func=cmd_list_versions)

    plug = sub.add_parser("plugin-covers")
    plug.add_argument("--version", required=True)
    plug.add_argument("--plugin-dir")
    plug.set_defaults(func=cmd_plugin_covers)

    hint = sub.add_parser("hint")
    hint.add_argument("--version", default=DEFAULT_NEW_CLUSTER_VERSION)
    hint.set_defaults(func=cmd_hint)

    rpms = sub.add_parser("rpm-urls")
    rpms.add_argument("--version", default=DEFAULT_NEW_CLUSTER_VERSION)
    rpms.add_argument("--el", default="8")
    rpms.add_argument("--arch", default="x86_64")
    rpms.set_defaults(func=cmd_rpm_urls)

    dep = sub.add_parser("ob-deploy-url")
    dep.add_argument("--version", default=DEFAULT_NEW_CLUSTER_VERSION)
    dep.add_argument("--el", default="8")
    dep.add_argument("--arch", default="x86_64")
    dep.set_defaults(func=cmd_ob_deploy_url)

    pdest = sub.add_parser("plugin-dest")
    pdest.set_defaults(func=cmd_plugin_dest)

    plat = sub.add_parser("rpm-platform", help="el/arch из obd mirror list local")
    plat.add_argument("--mirror-file")
    plat.add_argument("--arch", default="x86_64")
    plat.set_defaults(func=cmd_rpm_platform)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
