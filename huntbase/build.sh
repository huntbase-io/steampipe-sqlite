#!/usr/bin/env bash
# Build one steampipe_sqlite_<plugin>.so (linux/amd64) from this fork.
#
#   huntbase/build.sh <plugin-spec> <out-dir>
#
# <plugin-spec> is a huntbase/plugins.txt entry: name@version (turbot) or
# org/name@version. A plugin must be linked against the extension source for
# its own steampipe-plugin-sdk major version:
#   SDK v6 -> this branch (huntbase, from upstream develop)       $SRC_V6 (default: repo root)
#   SDK v5 -> branch huntbase-sdk-v5 (same commits on the last v5 upstream commit)  $SRC_V5
# Both carry the cursor fix and huntbase_build_<plugin>().
#
# This is what upstream's `make build` does, plus one step it lacks (upstream
# turbot/steampipe-sqlite#122, still open): the plugin's own go.mod `replace`
# directives are carried over. Without them duo, reddit, urlscan and vercel
# don't compile.
#
# Output in <out-dir>:
#   steampipe_sqlite_<name>.linux_amd64.tar.gz   (same layout as upstream's assets)
#   steampipe_sqlite_<name>.json                 (build info)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_V6="${SRC_V6:-$(cd "$HERE/.." && pwd)}"
SRC_V5="${SRC_V5:-}"

spec="${1:?usage: build.sh <plugin-spec> <out-dir>}"
out_dir="$(mkdir -p "${2:?usage: build.sh <plugin-spec> <out-dir>}" && cd "$2" && pwd)"
build_version="$(tr -d '[:space:]' < "$HERE/BUILD_VERSION")"

[[ "$spec" == *@* ]] || { echo "error: '$spec' has no @version" >&2; exit 4; }
plugin_ref="${spec%@*}"
version="${spec##*@}"
org="turbot"
name="$plugin_ref"
if [[ "$plugin_ref" == */* ]]; then
  org="${plugin_ref%%/*}"
  name="${plugin_ref#*/}"
fi
repo="github.com/${org}/steampipe-plugin-${name}"

gomod="$(curl -fsSL "https://raw.githubusercontent.com/${org}/steampipe-plugin-${name}/${version}/go.mod")"
sdk="$(grep -oE 'turbot/steampipe-plugin-sdk/v[0-9]+' <<<"$gomod" | head -1 | sed 's#.*/##')"
case "$sdk" in
  v6) src_tree="$SRC_V6" ;;
  v5) src_tree="${SRC_V5:?SDK v5 plugin: set SRC_V5 to a checkout of branch huntbase-sdk-v5}" ;;
  *) echo "error: ${repo}@${version}: unsupported SDK major '${sdk:-none}'" >&2; exit 1 ;;
esac
src_ref="$(git -C "$src_tree" rev-parse --short=8 HEAD)"
echo "Building ${repo}@${version} (SDK ${sdk}) from $(git -C "$src_tree" rev-parse --abbrev-ref HEAD)@${src_ref}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
rsync -a --exclude='.git' --exclude='work' --exclude='*.so' "$src_tree/" "$work/src/"
src="$work/src"

info="$(python3 - "$build_version" "$name" "$version" "$sdk" "$src_ref" "$org" <<'PY'
import json, sys
build, plugin, version, sdk, ref, org = sys.argv[1:]
print(json.dumps({
    "build": build,
    "plugin": plugin,
    "plugin_org": org,
    "plugin_version": version,
    "sdk": sdk,
    "steampipe_sqlite": f"huntbase-io/steampipe-sqlite@{ref}",
    "fixes": ["refilter"],
}, separators=(",", ":")))
PY
)"
sed -i "s|__HUNTBASE_BUILD_INFO__|${info}|" "$src/huntbase_build.go"

(
  cd "$src"
  go run generate/generator.go templates . "$name" "$version" "$repo"
  printf '%s\n' "$gomod" > "$work/plugin.go.mod"
  go mod edit -json "$work/plugin.go.mod" | python3 -c '
import json, sys
for r in json.load(sys.stdin).get("Replace") or []:
    old, new = r["Old"], r["New"]
    if new["Path"].startswith((".", "/")):
        continue  # local path replace: nothing to fetch
    lhs = old["Path"] + ("@" + old["Version"] if old.get("Version") else "")
    rhs = new["Path"] + ("@" + new["Version"] if new.get("Version") else "")
    print(f"-replace={lhs}={rhs}")
' | while read -r replace; do
    echo "go mod edit $replace"
    go mod edit "$replace"
  done
  major="$(grep -oE '^v[0-9]+' <<<"$version")"
  if [[ "$major" == "v0" || "$major" == "v1" ]]; then
    go get "${repo}@${version}"
  else
    go get "${repo}/${major}@${version}"
  fi
  go mod tidy
  make -f out/Makefile build
)

so="$src/steampipe_sqlite_${name}.so"
[[ -f "$so" ]] || { echo "error: build produced no ${so##*/}" >&2; exit 1; }

# Smoke test: the extension loads and reports this build.
python3 -I - "$so" "$name" "$info" <<'PY'
import sqlite3, sys
so, name, info = sys.argv[1:]
conn = sqlite3.connect(":memory:")
conn.enable_load_extension(True)
conn.load_extension(so[: -len(".so")])
got = conn.execute(f"select huntbase_build_{name}()").fetchone()[0]
assert got == info, (got, info)
print(f"smoke ok: {name} reports {got}")
PY

tar -C "$src" -czf "$out_dir/steampipe_sqlite_${name}.linux_amd64.tar.gz" "steampipe_sqlite_${name}.so"
printf '%s\n' "$info" > "$out_dir/steampipe_sqlite_${name}.json"
echo "Wrote $out_dir/steampipe_sqlite_${name}.linux_amd64.tar.gz"
