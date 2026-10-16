#!/usr/bin/env python3
"""Weekly check for newer steampipe plugins and upstream steampipe-sqlite changes.

    huntbase/update_check.py [--write] [--pr-body pr.md] [--summary summary.md] [--json out.json]

For every plugins.txt entry (name@version -> turbot/steampipe-plugin-<name>,
org/name@version -> <org>/steampipe-plugin-<name>) finds the newest stable
GitHub release (falling back to the newest vX.Y.Z tag), and for each plugin
with a newer one: its steampipe-plugin-sdk major before and after (SDK v5 -> v6
is flagged) and the tables added and removed (docs/tables/*.md).

Also compares upstream turbot/steampipe-sqlite (develop and main) with this
fork's huntbase branch and checks whether upstream now makes a fresh row
stream per xFilter (our fix/fresh-stream-per-filter).

With --write, rewrites huntbase/plugins.txt with the new versions and bumps
huntbase/BUILD_VERSION to the next release number. Prints changed=true|false
(also to $GITHUB_OUTPUT when set). Uses $GH_TOKEN / $GITHUB_TOKEN if present.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORK = "huntbase-io/steampipe-sqlite"
UPSTREAM = "turbot/steampipe-sqlite"
SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


class NotFound(Exception):
    pass


def api(path: str, raw: bool = False):
    url = path if path.startswith("https://") else f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "huntbase-update-check", **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https hosts
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise
    return body.decode() if raw else json.loads(body)


def semver(tag: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(tag)
    return tuple(map(int, m.groups())) if m else None


def read_specs(path: Path) -> list[dict]:
    specs = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        ref, _, version = line.rpartition("@")
        org, _, name = ref.rpartition("/")
        org = org or "turbot"
        specs.append({"spec": line, "ref": ref, "org": org, "name": name, "version": version,
                      "repo": f"{org}/steampipe-plugin-{name}"})
    return specs


def latest_version(repo: str) -> tuple[str, str]:
    """(tag, source) of the newest stable release, else newest vX.Y.Z tag."""
    try:
        tag = api(f"repos/{repo}/releases/latest")["tag_name"]
        if semver(tag):
            return tag, "release"
    except NotFound:
        pass
    tags = [t["name"] for t in api(f"repos/{repo}/tags?per_page=100") if semver(t["name"])]
    if not tags:
        raise NotFound(f"{repo}: no releases or vX.Y.Z tags")
    return max(tags, key=semver), "tag"


def sdk_major(repo: str, ref: str) -> str:
    try:
        gomod = api(f"https://raw.githubusercontent.com/{repo}/{ref}/go.mod", raw=True)
    except NotFound:
        return "?"
    m = re.search(r"turbot/steampipe-plugin-sdk/(v\d+)", gomod)
    return m.group(1) if m else "?"


def tables(repo: str, ref: str) -> set[str] | None:
    try:
        entries = api(f"repos/{repo}/contents/docs/tables?ref={ref}")
    except NotFound:
        return None
    return {e["name"][:-3] for e in entries if e["name"].endswith(".md")}


def check_plugin(spec: dict) -> dict:
    out = dict(spec)
    try:
        latest, source = latest_version(spec["repo"])
    except Exception as e:  # noqa: BLE001 - report it, don't stop the run
        return {**out, "error": f"{type(e).__name__}: {e}"}
    out.update(latest=latest, source=source)
    cur, new = semver(spec["version"]), semver(latest)
    if not (cur and new and new > cur):
        return {**out, "update": False}
    old_tables, new_tables = tables(spec["repo"], spec["version"]), tables(spec["repo"], latest)
    out.update(
        update=True,
        sdk_old=sdk_major(spec["repo"], spec["version"]),
        sdk_new=sdk_major(spec["repo"], latest),
        removed=sorted(old_tables - new_tables) if old_tables is not None and new_tables is not None else None,
        added=sorted(new_tables - old_tables) if old_tables is not None and new_tables is not None else None,
        major_bump=new[0] > cur[0],
    )
    return out


def upstream_status() -> dict:
    """Upstream commits we don't have, and whether upstream has the fix."""
    status = {}
    for branch in ("develop", "main"):
        try:
            cmp = api(f"repos/{FORK}/compare/huntbase...{UPSTREAM.split('/')[0]}:{branch}")
            status[branch] = {
                "ahead_by": cmp["ahead_by"],
                "commits": [{"sha": c["sha"][:8], "title": c["commit"]["message"].splitlines()[0],
                             "url": c["html_url"]} for c in cmp["commits"][-20:]],
                "compare_url": cmp["html_url"],
            }
        except Exception as e:  # noqa: BLE001
            status[branch] = {"error": f"{type(e).__name__}: {e}"}
        try:
            src = api(f"https://raw.githubusercontent.com/{UPSTREAM}/{branch}/cursor.go", raw=True)
            m = re.search(r"func \(p \*PluginCursor\) Filter\(.*?\n}\n", src, re.S)
            status[branch]["fix_landed"] = bool(m and "NewLocalPluginStream" in m.group(0))
        except Exception as e:  # noqa: BLE001
            status[branch]["fix_landed"] = None
            status[branch]["fix_error"] = str(e)
    try:
        prs = api(f"search/issues?q=repo:{UPSTREAM}+is:pr+xFilter+stream")["items"]
        status["related_prs"] = [{"number": p["number"], "title": p["title"], "state": p["state"],
                                  "url": p["html_url"]} for p in prs[:5]]
    except Exception:  # noqa: BLE001 - search is best effort
        status["related_prs"] = []
    return status


def next_build() -> str:
    current = int((HERE / "BUILD_VERSION").read_text().strip().lstrip("r"))
    published = [int(m.group(1)) for r in api(f"repos/{FORK}/releases?per_page=100")
                 if (m := re.fullmatch(r"huntbase-r(\d+)", r["tag_name"]))]
    return f"r{max([current, *published]) + 1}"


def rewrite_plugins(path: Path, bumps: dict[str, str]) -> None:
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line in bumps:
            raw = raw.replace(line, bumps[line])
        lines.append(raw)
    path.write_text("\n".join(lines) + "\n")


def flags(r: dict) -> list[str]:
    f = []
    if r["sdk_old"] != r["sdk_new"]:
        f.append(f"⚠️ SDK {r['sdk_old']} → {r['sdk_new']}")
    if r.get("major_bump"):
        f.append("⚠️ major version")
    if r["removed"]:
        f.append(f"⚠️ removed tables: {', '.join(f'`{t}`' for t in r['removed'])}")
    if r["removed"] is None:
        f.append("tables unknown (no docs/tables)")
    if r["added"]:
        f.append(f"{len(r['added'])} new table{'s' * (len(r['added']) != 1)}")
    return f


def report(results: list[dict], upstream: dict, build: str | None) -> str:
    updates = [r for r in results if r.get("update")]
    errors = [r for r in results if r.get("error")]
    out = []
    if updates:
        out += [f"Bumps {len(updates)} of {len(results)} plugins in `huntbase/plugins.txt`"
                + (f" and `huntbase/BUILD_VERSION` to `{build}`" if build else "") + ".",
                "Merging this PR builds, smoke-tests and publishes release "
                f"`huntbase-{build or 'r?'}`; hb-backend's steampipe-rollout then stages it for staging.", "",
                "| Plugin | Old → new | SDK | Release notes | Flags |", "|---|---|---|---|---|"]
        for r in updates:
            sdk = r["sdk_new"] if r["sdk_old"] == r["sdk_new"] else f"**{r['sdk_old']} → {r['sdk_new']}**"
            notes = (f"[{r['latest']}](https://github.com/{r['repo']}/releases/tag/{r['latest']})"
                     if r["source"] == "release" else f"[tag {r['latest']}](https://github.com/{r['repo']}/tree/{r['latest']})")
            notes += f" · [diff](https://github.com/{r['repo']}/compare/{r['version']}...{r['latest']})"
            out.append(f"| `{r['ref']}` | {r['version']} → {r['latest']} | {sdk} | {notes} | {'; '.join(flags(r)) or ''} |")
        v5to6 = [r["ref"] for r in updates if r["sdk_old"] == "v5" and r["sdk_new"] == "v6"]
        removed = [r["ref"] for r in updates if r["removed"]]
        out += [""]
        if v5to6:
            out.append(f"**SDK v5 → v6:** {', '.join(f'`{n}`' for n in v5to6)}. build.sh switches these to the SDK v6 "
                       "tree (this branch) automatically; check the plugin's release notes for config or behaviour changes.")
        if removed:
            out.append(f"**Tables removed:** {', '.join(f'`{n}`' for n in removed)}. Check hb-init-data surfaces and "
                       "templates that query them before merging.")
    else:
        out.append(f"All {len(results)} plugins are on their latest release.")
    if errors:
        out += ["", "Couldn't check: " + "; ".join(f"`{r['ref']}` ({r['error']})" for r in errors)]

    out += ["", "### Upstream turbot/steampipe-sqlite", ""]
    for branch in ("develop", "main"):
        s = upstream.get(branch, {})
        if "error" in s:
            out.append(f"- `{branch}`: couldn't compare ({s['error']})")
            continue
        landed = {True: "**yes**, upstream now makes a fresh stream per Filter: consider dropping our cursor fix",
                  False: "no", None: "unknown"}[s.get("fix_landed")]
        out.append(f"- `{branch}`: {s['ahead_by']} commit{'s' * (s['ahead_by'] != 1)} we don't have "
                   f"([compare]({s['compare_url']})). Fresh-stream fix upstream: {landed}.")
        for c in s["commits"][-10:]:
            out.append(f"  - [{c['sha']}]({c['url']}) {c['title']}")
    for p in upstream.get("related_prs", []):
        out.append(f"- Upstream PR [#{p['number']}]({p['url']}) ({p['state']}): {p['title']}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plugins", type=Path, default=HERE / "plugins.txt")
    ap.add_argument("--write", action="store_true", help="rewrite plugins.txt and BUILD_VERSION")
    ap.add_argument("--pr-body", type=Path)
    ap.add_argument("--summary", type=Path, help="Markdown report (written even when nothing changed)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    specs = read_specs(args.plugins)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(check_plugin, specs))
    upstream = upstream_status()
    updates = [r for r in results if r.get("update")]
    for r in results:
        state = r.get("error") or (f"{r['version']} -> {r['latest']} ({'; '.join(flags(r))})" if r.get("update")
                                   else f"{r['version']} (latest {r['latest']})")
        print(f"{r['ref']:28} {state}")
    for branch in ("develop", "main"):
        if upstream.get(branch, {}).get("fix_landed"):
            print(f"::warning::upstream {UPSTREAM} {branch} now has a fresh-stream-per-Filter fix")

    build = next_build() if updates else None
    if args.write and updates:
        rewrite_plugins(args.plugins, {r["spec"]: f"{r['ref']}@{r['latest']}" for r in updates})
        (HERE / "BUILD_VERSION").write_text(build + "\n")
    body = report(results, upstream, build)
    if args.summary:
        args.summary.write_text("## Plugin update check\n\n" + body)
    if args.pr_body and updates:
        args.pr_body.write_text(body + "\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n")
    if args.json:
        args.json.write_text(json.dumps({"results": results, "upstream": upstream, "build": build}, indent=2))

    changed = "true" if updates else "false"
    print(f"changed={changed}" + (f" build={build}" if build else ""))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={changed}\nbuild={build or ''}\nupdates={len(updates)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
