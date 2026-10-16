#!/usr/bin/env python3
"""Smoke-test a huntbase-io/steampipe-sqlite release before anything uses it.

    huntbase/smoke_test.py <asset-dir> [--plugins huntbase/plugins.txt] [--build rN]
                           [--report smoke-test.json] [--markdown smoke-test.md]

<asset-dir> holds a release's assets: steampipe_sqlite_<plugin>.linux_amd64.tar.gz
for every plugin, manifest.json and SHA256SUMS (`gh release download <tag>`,
or the release workflow's dist/ before it publishes). Checks, all of which must
pass:

  1. SHA256SUMS: every listed file is present and matches; every archive and
     manifest.json is listed.
  2. manifest.json: one entry per archive, all from the same build; with
     --plugins, exactly the plugins and versions pinned there; with --build,
     that build (e.g. r2).
  3. Every extension loads in sqlite3 (each in its own process: they are Go
     c-shared libraries) and huntbase_build_<plugin>() returns its manifest
     entry, whose fixes include every --require-fix (default: refilter).
  4. IN lists and join-driven quals return every row, using the release's own
     terraform extension against local .tf files (no network or credentials).
     Upstream's terraform v1.2.0 build fails this check (hb-backend#2573).

Exit status 0 when everything passes, 1 otherwise. The report lists each check.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ARCHIVE_SUFFIX = ".linux_amd64.tar.gz"
ARCHIVE_PREFIX = "steampipe_sqlite_"

# Runs in a child process: load one extension and print its build info.
LOAD_SNIPPET = r"""
import sqlite3, sys
so, name = sys.argv[1:]
conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
conn.load_extension(so[: -len(".so")])
print(conn.execute(f"select huntbase_build_{name}()").fetchone()[0])
"""

# Runs in a child process: IN-list and join checks against the terraform extension.
IN_LIST_SNIPPET = r"""
import json, sqlite3, sys
so, tf_dir, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
conn.load_extension(so[: -len(".so")])
conn.execute("select steampipe_configure_terraform(?)",
             (f'configuration_file_paths = ["{tf_dir}/*.tf"]',)).fetchall()
paths = [f"{tf_dir}/f{i:02d}.tf" for i in range(count)]
names = [f"r{i:02d}" for i in range(count)]
marks = ",".join("?" * count)
checks = [
    ("IN list on a key column (terraform_resource.path)",
     f"select name from terraform_resource where path in ({marks}) order by name",
     paths, [[n] for n in names]),
    ("join-driven qual (inner cursor filtered once per outer row)",
     "select j.value, t.name from json_each(?) j "
     "left join terraform_resource t on t.path = j.value order by j.value",
     [json.dumps(paths)], [[p, n] for p, n in zip(paths, names)]),
]
results = []
for name, sql, params, want in checks:
    ok, got = True, None
    # The upstream bug is racy: repeat each query.
    for _ in range(5):
        got = [list(r) for r in conn.execute(sql, params).fetchall()]
        if got != want:
            ok = False
            break
    results.append({"check": name, "ok": ok, "rows": len(got), "want_rows": len(want),
                    **({} if ok else {"got": got, "want": want})})
print(json.dumps(results))
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_pins(path: Path) -> dict[str, dict[str, str]]:
    """plugins.txt -> {name: {"org": org, "version": version}}."""
    pins = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        ref, _, version = line.rpartition("@")
        org, _, name = ref.rpartition("/")
        pins[name] = {"org": org or "turbot", "version": version}
    return pins


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, check: str, ok: bool, detail: str = "", **extra) -> None:
        self.checks.append({"check": check, "ok": ok, "detail": detail, **extra})
        print(f"{'ok  ' if ok else 'FAIL'} {check}{': ' + detail if detail else ''}", flush=True)

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)


def check_sums(assets: Path, report: Report) -> None:
    sums_file = assets / "SHA256SUMS"
    if not sums_file.is_file():
        report.add("SHA256SUMS present", False, f"no {sums_file}")
        return
    listed = {}
    for line in sums_file.read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            listed[name.lstrip("*")] = digest
    bad = []
    for name, digest in sorted(listed.items()):
        path = assets / name
        if not path.is_file():
            bad.append(f"{name}: missing")
        elif sha256(path) != digest:
            bad.append(f"{name}: checksum mismatch")
    unlisted = sorted(
        p.name for p in assets.iterdir()
        if (p.name.endswith(ARCHIVE_SUFFIX) or p.name == "manifest.json") and p.name not in listed
    )
    bad += [f"{n}: not in SHA256SUMS" for n in unlisted]
    report.add("SHA256SUMS", not bad, "; ".join(bad) or f"{len(listed)} files verified",
               files=len(listed))


def check_manifest(assets: Path, pins: dict | None, build: str | None, report: Report) -> dict:
    try:
        plugins = json.loads((assets / "manifest.json").read_text())["plugins"]
    except (OSError, ValueError, KeyError) as e:
        report.add("manifest.json", False, f"unreadable: {e}")
        return {}
    archives = {
        p.name[len(ARCHIVE_PREFIX):-len(ARCHIVE_SUFFIX)]
        for p in assets.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}")
    }
    problems = []
    if set(plugins) != archives:
        problems.append(f"manifest without archive: {sorted(set(plugins) - archives)}; "
                        f"archive without manifest entry: {sorted(archives - set(plugins))}")
    builds = {e.get("build") for e in plugins.values()}
    if len(builds) != 1:
        problems.append(f"mixed builds {sorted(map(str, builds))}")
    if build and builds != {build}:
        problems.append(f"build {sorted(map(str, builds))}, want {build}")
    if pins is not None:
        for name in sorted(set(pins) | set(plugins)):
            want, got = pins.get(name), plugins.get(name)
            if want is None:
                problems.append(f"{name}: in manifest, not pinned")
            elif got is None:
                problems.append(f"{name}: pinned, not in manifest")
            elif (got.get("plugin_org"), got.get("plugin_version")) != (want["org"], want["version"]):
                problems.append(f"{name}: manifest {got.get('plugin_org')}@{got.get('plugin_version')}, "
                                f"pinned {want['org']}@{want['version']}")
    report.add("manifest.json", not problems, "; ".join(problems) or
               f"{len(plugins)} plugins, build {next(iter(builds))}" +
               (", matches plugins.txt" if pins is not None else ""),
               plugins=len(plugins))
    return plugins


def extract(archive: Path, name: str, into: Path) -> Path:
    so_name = f"{ARCHIVE_PREFIX}{name}.so"
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if [m.name for m in members] != [so_name] or not members[0].isfile():
            raise ValueError(f"expected exactly {so_name}, got {[m.name for m in members]}")
        tar.extract(members[0], into, filter="data")
    return into / so_name


def check_one(assets: Path, work: Path, name: str, entry: dict, fixes: list[str]) -> dict:
    out = {"plugin": name, "version": entry.get("plugin_version"), "sdk": entry.get("sdk")}
    try:
        so = extract(assets / f"{ARCHIVE_PREFIX}{name}{ARCHIVE_SUFFIX}", name, work / name)
        proc = subprocess.run([sys.executable, "-I", "-c", LOAD_SNIPPET, str(so), name],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            return {**out, "ok": False, "error": (proc.stderr.strip().splitlines() or ["load failed"])[-1]}
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        if got != entry:
            return {**out, "ok": False, "error": f"huntbase_build_{name}() = {got}, manifest has {entry}"}
        missing = [f for f in fixes if f not in got.get("fixes", [])]
        if missing:
            return {**out, "ok": False, "error": f"fixes {got.get('fixes')} lack {missing}"}
        return {**out, "ok": True, "fixes": got["fixes"]}
    except Exception as e:  # noqa: BLE001 - report every failure, keep going
        return {**out, "ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        for f in (work / name).glob("*.so"):
            f.unlink()


def check_loads(assets: Path, plugins: dict, fixes: list[str], jobs: int, report: Report) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            results = list(pool.map(lambda kv: check_one(assets, work, kv[0], kv[1], fixes),
                                    sorted(plugins.items())))
    failed = [r for r in results if not r["ok"]]
    for r in failed:
        print(f"     {r['plugin']}: {r['error']}")
    report.add(f"load + huntbase_build_<plugin>() (fixes include {', '.join(fixes)})", not failed,
               f"{len(results) - len(failed)}/{len(results)} extensions ok"
               + (f"; failed: {', '.join(r['plugin'] for r in failed)}" if failed else ""),
               extensions=results)


def check_in_list(assets: Path, plugins: dict, report: Report, count: int = 12) -> None:
    title = "IN list / join quals return every row (terraform, local files)"
    if "terraform" not in plugins:
        report.add(title, False, "terraform is not in this release; the IN-list check needs it")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tf_dir = tmp_path / "tf"
        tf_dir.mkdir()
        for i in range(count):
            (tf_dir / f"f{i:02d}.tf").write_text(f'resource "null_resource" "r{i:02d}" {{}}\n')
        try:
            so = extract(assets / f"{ARCHIVE_PREFIX}terraform{ARCHIVE_SUFFIX}", "terraform", tmp_path)
            proc = subprocess.run([sys.executable, "-I", "-c", IN_LIST_SNIPPET, str(so), str(tf_dir), str(count)],
                                  capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr.strip().splitlines() or ["failed"])[-1])
            results = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as e:  # noqa: BLE001
            report.add(title, False, f"{type(e).__name__}: {e}")
            return
    detail = "; ".join(f"{r['check']}: {r['rows']}/{r['want_rows']} rows" for r in results)
    report.add(title, all(r["ok"] for r in results), detail, queries=results)


def markdown(report: Report, label: str) -> str:
    lines = [f"### Smoke test: {label}", "", "| | Check | Result |", "|---|---|---|"]
    for c in report.checks:
        lines.append(f"| {'✅' if c['ok'] else '❌'} | {c['check']} | {c['detail']} |")
    failed = [e for c in report.checks for e in c.get("extensions", []) if not e["ok"]]
    if failed:
        lines += ["", "Failed extensions:", ""] + [f"- `{e['plugin']}`: {e['error']}" for e in failed]
    lines += ["", f"**{'PASS' if report.ok else 'FAIL'}**"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("assets", type=Path)
    ap.add_argument("--plugins", type=Path, help="plugins.txt the release must match")
    ap.add_argument("--build", help="expected build, e.g. r2")
    ap.add_argument("--require-fix", action="append", dest="fixes",
                    help="fix every extension must report (repeatable; default: refilter)")
    ap.add_argument("--label", default="", help="release name for the report")
    ap.add_argument("--report", type=Path, help="write the JSON report here")
    ap.add_argument("--markdown", type=Path, help="write a Markdown summary here")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 2))
    args = ap.parse_args()
    fixes = args.fixes or ["refilter"]
    assets = args.assets.resolve()

    report = Report()
    check_sums(assets, report)
    pins = read_pins(args.plugins) if args.plugins else None
    plugins = check_manifest(assets, pins, args.build, report)
    if plugins:
        check_loads(assets, plugins, fixes, args.jobs, report)
        check_in_list(assets, plugins, report)

    label = args.label or assets.name
    if args.report:
        args.report.write_text(json.dumps({"release": label, "ok": report.ok, "checks": report.checks}, indent=2) + "\n")
    if args.markdown:
        args.markdown.write_text(markdown(report, label))
    print("PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
