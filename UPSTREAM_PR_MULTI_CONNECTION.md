# Upstream PR draft: several connections of one plugin in one session

Target: `turbot/steampipe-sqlite` `main`. Branch: `huntbase-io/steampipe-sqlite:feat/multi-connection`.
It sits on top of `fix/fresh-stream-per-filter` (see `UPSTREAM_PR.md`), so open that PR first, or open
this one as stacked on it. The SDK v5 equivalent is `feat/multi-connection-sdk-v5` (same commits, v5
imports).

Not opened yet: Tyler reviews it first.

---

**Title:** Support several connections of the same plugin in one SQLite session (#98)

## Problem
Closes #98.

An extension holds one connection per process. `steampipe_configure_<plugin>(config)` replaces it and
every table reads it. If you configure a second AWS account, the first one is gone, and there is no way
to query both in one session, let alone in one query. `attach`ing another database doesn't help: the
tables are eponymous and global, so `db2.aws_account` reads the same connection as `aws_account`.

## Design
The plugin server can already serve many connections. That is how the Postgres FDW runs one plugin
process for many connections: config, connection cache and query cache entries are all keyed by
connection name. This PR exposes that in SQLite.

1. **Named connections.** `steampipe_configure_<plugin>('<name>', '<config>')` adds a connection or
   updates its config. The one-argument form is unchanged and configures the default connection, which
   is named after the plugin (`aws`).
2. **Declared tables bound to a connection.** The modules already allow `create virtual table`
   (`xCreate == xConnect`), but `xConnect` ignored its arguments. It now reads `connection='<name>'`:
   ```sql
   create virtual table prod_account using aws_account(connection='prod');
   create virtual table db2.aws_account using aws_account(connection='dev');
   ```
   Without an argument, or for the eponymous table, the table reads the default connection, as before.
   For a named connection, the columns come from that connection's schema, so a dynamic-schema plugin
   gets the right columns, and a table the connection doesn't have is an error.
3. **The connection travels with each query.** `PluginTable` carries its connection name, and
   `cursor.Filter` sends it as `ExecuteRequest.Connection` and as the `ExecuteConnectionData` key.
   Each `Filter` still gets a fresh row stream (the #2573 fix), now per connection.
4. **Config changes stay per connection.** A new connection is added with
   `UpdateConnectionConfigs(Added)` and a changed one with `UpdateConnectionConfigs(Changed)`. The plugin's
   `ConnectionConfigChangedFunc` then runs for that connection only. By default it clears that
   connection's connection cache and query cache, and nothing else.
5. **Rejected configs are errors.** The SDK's `PluginServer.UpdateConnectionConfigs` builds the
   `FailedConnections` map and then returns an empty response, so today an invalid config passed to an
   already configured static plugin is silently ignored: the connection keeps its old config. After the
   update the extension passes the same config to `SetAllConnectionConfigs`, with `MaxCacheSizeMb: -1`
   so the query cache is left alone. That call does report failures. It upserts the same config into the
   same connection, so it has no other effect. (Fixing `UpdateConnectionConfigs` in the SDK would make
   this second call unnecessary.)
6. **Errors and helpers.**
   - A declared table on a connection that isn't configured fails when it is created:
     `aws_account: connection "prod" is not configured: call steampipe_configure_aws('prod', '<config>') first`.
   - Names are limited to letters, digits, `_`, `-` and `.` (at most 128 characters).
   - Unknown module arguments are errors.
   - `drop table` works on declared tables.
   - `steampipe_connections_<plugin>()` returns the configured connection names as a JSON array.

## SQL
```sql
select steampipe_configure_aws('prod', 'profile = "prod"');
select steampipe_configure_aws('dev', 'profile = "dev"');

create virtual table prod_account using aws_account(connection='prod');
attach database ':memory:' as dev;
create virtual table dev.aws_account using aws_account(connection='dev');

-- both accounts, one query
select 'prod' as env, account_id from prod_account
union all
select 'dev', account_id from dev.aws_account;

-- the default connection is unchanged
select account_id from main.aws_account;

select steampipe_connections_aws();   -- ["aws","dev","prod"]
drop table prod_account;
```

## Compatibility
- Existing queries, the one-argument configure function and eponymous tables behave as before.
- One behaviour change: an invalid config now fails `steampipe_configure_<plugin>` instead of being
  ignored (point 5).
- SQLite resolves an unqualified name to a declared table in any schema before the eponymous table.
  After `create virtual table dev.aws_account ...`, a bare `aws_account` means `dev.aws_account`, and
  `main.aws_account` means the default connection. This is how SQLite name resolution works; the
  README says so.
- Connections belong to the process, not to the SQLite connection. This was already true of the
  single config.
- Declared tables persisted in an on-disk database need their connection configured before they are
  used or dropped. This is the same rule as needing the extension loaded.
- Dynamic-schema plugins create their modules when the default connection is configured, as before. A
  declared table on a named connection can use those table names, with that connection's columns.
- Builds on SDK v6, and on SDK v5 with only the import path changed (tested with v5.11.6).

## Tests
`scripts/test_multi_connection.py` (added to `test.yml` after the chaos build) uses the chaos plugin.
It needs no credentials. The `regions` config decides what `chaos_regions` and `chaos_multi_region`
return, and `chaos_cache_check.unique_col` is random per fetch, so a cache hit is visible. The test
checks:
- two named connections with different configs return their own rows in one `UNION ALL` across `main`
  and an attached database, and in a cross join;
- the default (eponymous) table is unaffected, before and after configuring it;
- `IN` lists on a key column and join-driven quals return every row per connection, in one query
  (`chaos_multi_region` get calls per region, 30 filters on one cursor of each connection);
- the query cache is on, but two connections never share entries, and the default connection doesn't
  share with a named one;
- reconfiguring one connection changes its rows and clears its cache, and leaves the other connection's
  rows and cache alone;
- errors: an unconfigured connection, an unknown module argument, an invalid name, a rejected config
  (the old config stays), and a wrong argument count. Dropping declared tables works.

All 25 checks pass on SDK v6 (chaos v2.0.3) and SDK v5 (chaos v1.2.4). `scripts/test_refilter.py` still
passes.
