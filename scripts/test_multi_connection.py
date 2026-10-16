#!/usr/bin/env python3
"""Several connections of one plugin in one SQLite session.

Uses the chaos plugin (no network or credentials). Its `regions` config decides
what chaos_regions and chaos_multi_region return, and chaos_cache_check returns
a random unique_col per fetch, so a cached result is recognisable.

    make build plugin=chaos plugin_version=v2.0.3 plugin_github_url=github.com/turbot/steampipe-plugin-chaos
    python3 scripts/test_multi_connection.py ./steampipe_sqlite_chaos.so
"""

import json
import sqlite3
import sys

EAST = 'regions = ["us-east-1", "us-east-2"]'
WEST = 'regions = ["us-west-1"]'

failed = 0


def check(name, got, want):
    global failed
    if got == want:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
        failed += 1


def check_error(name, conn, sql, params, want_substring):
    global failed
    try:
        conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        if want_substring in str(e):
            print(f"ok   {name}")
        else:
            print(f"FAIL {name}: error {e!r} does not mention {want_substring!r}")
            failed += 1
        return
    print(f"FAIL {name}: no error")
    failed += 1


def rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def main(so_path: str) -> int:
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    conn.load_extension(so_path[: -len(".so")] if so_path.endswith(".so") else so_path)

    check("only the default connection at load", rows(conn, "select steampipe_connections_chaos()"), [('["chaos"]',)])

    rows(conn, "select steampipe_configure_chaos('east', ?)", (EAST,))
    rows(conn, "select steampipe_configure_chaos('west', ?)", (WEST,))
    check(
        "steampipe_connections_chaos() lists the named connections",
        json.loads(rows(conn, "select steampipe_connections_chaos()")[0][0]),
        ["chaos", "east", "west"],
    )

    # declared tables: one in main, one in an attached database
    conn.execute("create virtual table east_regions using chaos_regions(connection='east')")
    conn.execute("attach database ':memory:' as db2")
    conn.execute("create virtual table db2.chaos_regions using chaos_regions(connection=\"west\")")

    check(
        "two connections, one query (main + attached db)",
        rows(
            conn,
            "select 'east' c, region_name from east_regions "
            "union all select 'west', region_name from db2.chaos_regions "
            "order by 1, 2",
        ),
        [("east", "us-east-1"), ("east", "us-east-2"), ("west", "us-west-1")],
    )
    check(
        "join across connections",
        rows(
            conn,
            "select count(*) from east_regions e cross join db2.chaos_regions w",
        ),
        [(2,)],
    )

    # the eponymous table is the default connection: still unconfigured (no regions)
    check("default (eponymous) table unchanged", rows(conn, "select count(*) from main.chaos_regions"), [(0,)])
    rows(conn, "select steampipe_configure_chaos(?)", ('regions = ["us-central-1"]',))
    check(
        "configuring the default connection only changes the default table",
        rows(
            conn,
            "select 'default', region_name from main.chaos_regions "
            "union all select 'east', region_name from east_regions "
            "union all select 'west', region_name from db2.chaos_regions order by 1, 2",
        ),
        [("default", "us-central-1"), ("east", "us-east-1"), ("east", "us-east-2"), ("west", "us-west-1")],
    )

    # IN lists and join-driven quals, per connection (refilter regression)
    conn.execute("create virtual table east_mr using chaos_multi_region(connection='east')")
    conn.execute("create virtual table west_mr using chaos_multi_region(connection='west')")
    ids = ["0_us-east-1", "1_us-east-2", "2_us-west-1", "3_us-east-1"]
    in_list = "(" + ",".join(f"'{i}'" for i in ids) + ")"
    want_east = [("0_us-east-1",), ("1_us-east-2",), ("3_us-east-1",)]
    want_west = [("2_us-west-1",)]
    want_joined = [("east", "0_us-east-1"), ("east", "1_us-east-2"), ("east", "3_us-east-1"), ("west", "2_us-west-1")]
    # the refilter bug was a race: run each a few times and keep the first miss
    for _ in range(5):
        east = rows(conn, f"select id from east_mr where id in {in_list} order by id")
        west = rows(conn, f"select id from west_mr where id in {in_list} order by id")
        joined = rows(
            conn,
            "select 'east', m.id from json_each(?1) j join east_mr m on m.id = j.value "
            "union all select 'west', m.id from json_each(?1) j join west_mr m on m.id = j.value "
            "order by 1, 2",
            (json.dumps(ids),),
        )
        if (east, west, joined) != (want_east, want_west, want_joined):
            break
    check("IN list on a key column, east", east, want_east)
    check("IN list on a key column, west", west, want_west)
    check("join-driven quals, both connections in one query", joined, want_joined)
    conn.execute("create virtual table east_t using chaos_transforms(connection='east')")
    conn.execute("create virtual table db2.west_t using chaos_transforms(connection='west')")
    check(
        "30 filters on one cursor of each connection",
        rows(
            conn,
            "select (select count(t.id) from json_each(?1) j left join east_t t on t.id = j.value), "
            "(select count(t.id) from json_each(?1) j left join db2.west_t t on t.id = j.value)",
            (json.dumps([i % 5 + 1 for i in range(30)]),),
        ),
        [(30, 30)],
    )

    # query cache: keyed by connection
    conn.execute("create virtual table east_cache using chaos_cache_check(connection='east')")
    conn.execute("create virtual table west_cache using chaos_cache_check(connection='west')")
    q = "select group_concat(unique_col) from (select unique_col from {} order by id)"
    east1 = rows(conn, q.format("east_cache"))
    east2 = rows(conn, q.format("east_cache"))
    west1 = rows(conn, q.format("west_cache"))
    default1 = rows(conn, q.format("main.chaos_cache_check"))
    check("cache is on: same connection, same query, same rows", east1, east2)
    check("no cache hit across connections (east vs west)", east1 != west1, True)
    check("no cache hit across connections (east vs default)", east1 != default1, True)

    # config change on one connection: its cache is cleared, the other's is not
    rows(conn, "select steampipe_configure_chaos('west', ?)", ('regions = ["us-west-2"]',))
    check("reconfigured connection returns its new config", rows(conn, "select region_name from db2.chaos_regions"), [("us-west-2",)])
    check("reconfiguring west clears west's cache", rows(conn, q.format("west_cache")) != west1, True)
    check("reconfiguring west keeps east's cache", rows(conn, q.format("east_cache")), east1)
    check(
        "reconfiguring west does not change east",
        rows(conn, "select region_name from east_regions order by 1"),
        [("us-east-1",), ("us-east-2",)],
    )

    # errors
    check_error(
        "table on an unconfigured connection",
        conn,
        "create virtual table nope using chaos_regions(connection='prod')",
        (),
        'connection "prod" is not configured',
    )
    check_error(
        "unknown module argument",
        conn,
        "create virtual table nope using chaos_regions(region='x')",
        (),
        "unknown argument",
    )
    check_error(
        "invalid connection name",
        conn,
        "select steampipe_configure_chaos('bad name', ?)",
        (WEST,),
        "invalid connection name",
    )
    check_error(
        "invalid config is reported",
        conn,
        "select steampipe_configure_chaos('east', 'no_such_arg = 1')",
        (),
        "no_such_arg",
    )
    check(
        "a rejected config leaves the connection as it was",
        rows(conn, "select region_name from east_regions order by 1"),
        [("us-east-1",), ("us-east-2",)],
    )
    check_error("wrong number of arguments", conn, "select steampipe_configure_chaos('a', 'b', 'c')", (), "expected")

    conn.execute("drop table east_regions")
    conn.execute("drop table db2.chaos_regions")
    check(
        "declared tables can be dropped",
        rows(conn, "select count(*) from sqlite_master where name = 'east_regions'"),
        [(0,)],
    )
    check("eponymous table works after the drops", rows(conn, "select region_name from chaos_regions"), [("us-central-1",)])

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
