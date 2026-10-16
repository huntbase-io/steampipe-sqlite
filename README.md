# Steampipe SQLite

A family of SQLite extensions, each derived from a [Steampipe plugin](https://hub.steampipe.io/plugins), that fetch data from cloud services and APIs.

## Getting Started

You can use an installer that enables you to choose a plugin and download the SQLite extension for that plugin.

**[Installation guide →](https://steampipe.io/docs/steampipe_sqlite/install)**

## Examples

### Select EC2 instances

```sql
select * from aws_ec2_instance;
```

### Filter to running instances

```sql
select * from aws_ec2_instance
where instance_state='running';
```

### Select a subset of columns

```sql
select arn, instance_state from aws_ec2_instance
where instance_state='running';
```

### Limit results

```sql
select arn, instance_state from aws_ec2_instance
where instance_state='running'
limit 10;
```

### Query several connections of one plugin

`steampipe_configure_<plugin>(config)` configures the default connection,
which the tables you query directly (`aws_account`) use. Pass a name first to
add or update a named connection, then declare tables that read it. A declared
table can live in any schema, including an attached database:

```sql
select steampipe_configure_aws('prod', 'profile = "prod"');
select steampipe_configure_aws('dev', 'profile = "dev"');

create virtual table prod_account using aws_account(connection='prod');
attach database ':memory:' as dev;
create virtual table dev.aws_account using aws_account(connection='dev');

select 'prod' as env, account_id from prod_account
union all
select 'dev', account_id from dev.aws_account;

-- configured connections, as a JSON array
select steampipe_connections_aws();
```

Each connection has its own config, connection cache and query cache entries.
Changing one connection's config clears only that connection's caches. A
declared table that names a connection that isn't configured fails with an
error, and `drop table` removes a declared table. Connections are shared by
every database connection in the process.

SQLite resolves an unqualified table name to a declared table before the
eponymous one, so after `create virtual table dev.aws_account ...`, plain
`aws_account` means `dev.aws_account`. Write `main.aws_account` for the
default connection.
## Developing

To build an extension, use the provided `Makefile`. For example, to build the AWS extension, run the following command. The built extension lands in your current directory. 

```bash
make build plugin=aws
```

## Open Source & Contributing

This repository is published under the [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0) license. Please see our [code of conduct](https://github.com/turbot/.github/blob/main/CODE_OF_CONDUCT.md). We look forward to collaborating with you!

[Steampipe](https://steampipe.io) is a product produced exclusively by [Turbot HQ, Inc](https://turbot.com). It is distributed under our commercial terms. Others are allowed to make their own distribution of the software, but cannot use any of the Turbot trademarks, cloud services, etc. You can learn more in our [Open Source FAQ](https://turbot.com/open-source).

