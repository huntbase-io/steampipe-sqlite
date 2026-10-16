#!/usr/bin/env python3
"""Regression test: several xFilter calls on one cursor must each return
their own rows.

SQLite filters one virtual-table cursor again for every value of an IN (...)
list on a constrained column and for every outer row on the inner side of a
join. Uses the chaos plugin (no network or credentials):

    make build plugin=chaos plugin_version=v2.0.3 plugin_github_url=github.com/turbot/steampipe-plugin-chaos
    python3 scripts/test_refilter.py ./steampipe_sqlite_chaos.so
"""

import json
import sqlite3
import sys


def main(so_path: str) -> int:
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    conn.load_extension(so_path[: -len(".so")] if so_path.endswith(".so") else so_path)

    checks = [
        (
            "IN list on a key column",
            "select id from chaos_transforms where id in (1, 2, 3) order by id",
            [(1,), (2,), (3,)],
        ),
        (
            "join-driven qual (inner cursor filtered once per outer row)",
            "select j.value, t.id from json_each('[1, 2, 3]') j "
            "left join chaos_transforms t on t.id = j.value order by j.value",
            [(1, 1), (2, 2), (3, 3)],
        ),
        (
            "many filters on one cursor",
            "select count(t.id) from json_each(?) j "
            "left join chaos_transforms t on t.id = j.value",
            [(30,)],
        ),
    ]
    failed = 0
    # The bug is a race between leftover stream markers and the next execute,
    # so run each check a few times.
    for name, sql, want in checks:
        params = (json.dumps([i % 5 + 1 for i in range(30)]),) if "?" in sql else ()
        for attempt in range(5):
            got = conn.execute(sql, params).fetchall()
            if got != want:
                print(f"FAIL {name} (attempt {attempt + 1}): got {got}, want {want}")
                failed += 1
                break
        else:
            print(f"ok   {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
