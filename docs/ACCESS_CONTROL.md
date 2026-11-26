# SQL access controls

Use SQL-managed roles to control which tables, columns and rows an authenticated DuckFlight
client can read or change. The policies apply to remote listeners. The local DuckDB host
owner retains direct access to the database.

## Give a reader access to one column

SQL access controls need DuckFlight 0.1.21 or later on stock DuckDB **2.0.1**; they are not
available on DuckDB 2.0.0. This example also creates its logins in the
[account store](AUTHENTICATION.md#the-account-store), which needs DuckFlight 0.2.0 or later. See
the [compatibility table](INSTALL.md#which-build-you-get) for where to install from. It uses two
users and an in-memory table.

**1. Configure the role store.** Create a private directory with a config file in it:

```sh
mkdir -m 700 acl-demo
```

Save this as `acl-demo/duckflight.toml`:

```toml
[acl]
store = "roles.duckdb"
admins = ["local~ops"]
```

DuckFlight creates the role store relative to the config file. Keep its directory private and
writable by the host account. Roles and accounts share one store, so `roles.duckdb` also holds the
two logins below. `local~ops` names the store user `ops`, who can issue grants. All listeners on
the same host runtime must use the same ACL settings.

**2. Start the host.** In DuckDB 2.0.1, install DuckFlight from the
[Sidequery repository](INSTALL.md#sidequery-repository) and load it. Run from the directory
containing `acl-demo` and leave this DuckDB session open. Pass the config file to the setup
functions so the accounts land in the role store:

```sql
load duckflight from sidequery;
create table orders as select 42 as id, 'account-1234' as account_number;

select * from duckflight_init('ops', config_file := 'acl-demo/duckflight.toml');
select * from duckflight_user_add('bob', config_file := 'acl-demo/duckflight.toml');

select * from duckflight_pg_serve('127.0.0.1:5433', 'acl-demo/duckflight.toml');
```

`ops` is an administrator and `bob` a `reader`. Each function returns the user's generated password
once.

**3. Grant access through the listener.** Open a second terminal and connect with
`psql -h 127.0.0.1 -p 5433 -U ops -d memory -W`. Run these statements as `ops`.

```sql
create role analyst;
grant usage on catalog memory to role analyst;
grant usage on schema memory.main to role analyst;
grant select (id) on table memory.main.orders to role analyst;
grant role analyst to principal 'local~bob';
show grants for role analyst details;
```

**4. Verify and revoke.** In a third terminal, connect with
`psql -h 127.0.0.1 -p 5433 -U bob -d memory -W`. Bob's single assigned role activates
automatically.

```sql
select id from memory.main.orders;             -- returns 42
select account_number from memory.main.orders; -- permission denied
```

Back in the `ops` session, run the following command. Repeat the first query in the open `bob`
session. It now returns permission denied.

```sql
revoke select (id) on table memory.main.orders from role analyst;
```

For a **file-backed native DuckDB table**, run `register table catalog.schema.table`
as the ACL administrator before granting access. This records durable table and column
identities in their comments so the grants survive reopening the same table. An in-memory
table's grants apply to its current instance.

## Mask a column or filter by tenant

Continuing the example, the administrator can expose just the last four characters of
the account number. After this grant, Bob's second query returns `****1234`.

```sql
grant select masked last four (account_number) on table memory.main.orders to role analyst;
```

Other built-in masks include `email_domain`, `year_only` and `redact`. For a table
with an `email` column, this grant exposes only the domain:

```sql
grant select masked with email_domain (email) on table warehouse.sales.orders to role support;
```

Administrators can define reusable masks with `create masking policy`.
Filters, joins and aggregates use the value visible to the reader. A raw SELECT
grant on the same column through an active or inherited role exposes the raw value.

For a table with a physical `VARCHAR` tenant column, an administrator can run
`alter table warehouse.sales.orders set row policy using tenant (tenant_id)`.
The policy compares each row to the authenticated tenant ID, for example from a
verified OIDC claim, or the tenant of a store user, set with
`alter user bob set tenant 'acme'`. The client still needs the relevant grants. A
credential without a tenant ID cannot read that table. Tenant restrictions also apply
to permitted writes.

## Roles, credentials and protocols

- **Roles.** Assign roles to principals, verified OIDC groups or other roles.
  For example, `grant role analyst to group 'oidc:company', 'analytics'` uses the
  configured provider named `company`. Session clients can choose an assigned role
  with `set role analyst`.
- **Built-in roles.** Every store user also has a built-in role, `reader`, `writer`
  or `admin`, managed with [account SQL](AUTHENTICATION.md#manage-accounts-with-sql).
  A table grant and the built-in role both apply: a `reader` cannot use an UPDATE
  grant. The names `reader`, `writer` and `admin` are reserved, so `create role`
  cannot use them.
- **Administrators.** The principals in `acl.admins` and every store user with the
  `admin` role can issue grants. A principal in `acl.admins` also needs the
  `query:admin` scope, which for a store user means the `admin` role, and for an OIDC
  identity a rule or built-in role that grants it.
- **Writes.** Grant INSERT, UPDATE and DELETE independently. INSERT and UPDATE can
  be limited to specific columns. Columns read by a write's predicates or expressions
  also need SELECT. Writes run in autocommit on PgWire and Flight SQL.
- **Protocols.** Administer grants over PgWire or Flight SQL. Managed HTTP supports
  reads through `/streaming-query`. Managed DuckFlight Quack supports read-only
  statements and requires reconnecting after a policy revision.
- **Static policies.** For an existing file-based allowlist, set `DUCKFLIGHT_ACL_POLICY`
  before loading the unified extension and use PgWire or Flight SQL. SQL-managed roles,
  tenant row policies and named masks use the role store.
