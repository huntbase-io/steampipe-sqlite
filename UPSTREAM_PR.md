# Upstream PR draft (not opened yet)

Target: `turbot/steampipe-sqlite` `develop`, from branch `fix/fresh-stream-per-filter`
(two commits: the fix, then the test). The `huntbase*` branches and files are ours and are not part of the PR.

---

**Title:** Fix rows dropped when SQLite filters one cursor more than once (IN lists, joins)

## Problem
SQLite calls `xFilter` again on the **same** virtual-table cursor:
- once for every value of an `IN (...)` list on a constrained column, and
- once for every outer row when the table is the inner side of a join (for example a required key column supplied by another table).

`PluginCursor` creates one `anywhere.LocalPluginStream` in `NewPluginCursor` and reuses it for every `Filter()`. Each execute ends its rows with **two** end-of-rows markers: `Plugin.execute` sends a nil row (needed for local streams), then `PluginServer.CallExecuteAsync` sends another nil. An error closes the stream's `done` channel for good. So the second `Filter` reads the first execute's leftover nil and returns no rows, and its own rows (and nils) spill into the `Filter` after it. The query succeeds with rows missing. Because SQLite re-checks an `IN` constraint against the whole list, rows can also come back under the wrong value.

## Repro
Uses the chaos plugin (no network or credentials):
```sh
make build plugin=chaos plugin_version=v2.0.3 plugin_github_url=github.com/turbot/steampipe-plugin-chaos
python3 - <<'PY'
import sqlite3
c = sqlite3.connect(":memory:"); c.enable_load_extension(True)
c.load_extension("./steampipe_sqlite_chaos")
print(c.execute("select id from chaos_transforms where id in (1, 2, 3)").fetchall())
print(c.execute("select j.value, t.id from json_each('[1,2,3]') j left join chaos_transforms t on t.id = j.value").fetchall())
PY
```
Before the fix: `[(1,)]` or `[(1,), (3,)]` (it is racy), and `[(1, 1), (2, None), (3, None)]`.
After the fix: `[(1,), (2,), (3,)]` and `[(1, 1), (2, 2), (3, 3)]`.

The same happens with real plugins: `whois_domain where domain in ('google.com','github.com','huntbase.io')` returned 1–2 rows, `ipinfo_ip where ip in (...)` likewise, and `jenkins_job join jenkins_build on b.job_full_name = j.full_name` returned the builds of the first job only.

## Fix
In `Filter()`, cancel the previous execute and give the cursor a new stream with its own cancellable context before calling `CallExecuteAsync`. `NewPluginCursor` now also derives the stream's context from the cancel function it stores. Before, `cursorCancel` cancelled a context nothing used, so `Close()` never stopped an in-flight execute. The diff is about 15 lines in `cursor.go`, with no API changes, and it applies unchanged to the SDK v5 tree.

## Test
`scripts/test_refilter.py` loads the chaos extension that the existing `Acceptance Tests` workflow builds, and checks three things:
- an IN list on a key column returns all three rows;
- a join that re-filters the inner cursor per outer row returns all three matches;
- 30 filters on one cursor return 30 rows.

Each check runs five times because the old failure is a race. The new workflow step runs it after "Check binary is created". It fails on the old `cursor.go` and passes with the fix.
