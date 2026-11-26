# SQL access controls

Use SQL-managed roles to control which tables, columns and rows an authenticated DuckFlight
client can read or change. The policies apply to remote listeners. The local DuckDB host
owner retains direct access to the database.

## Give a reader access to one column

This example uses DuckFlight 0.1.21 or later on stock DuckDB **2.0.1**, two
password-authenticated users, and an in-memory table. Access controls are not available on
DuckDB 2.0.0. See the [compatibility table](INSTALL.md#which-build-you-get) for where to
install from.

**1. Configure the two logins and the role store.** From the extension checkout, create
the credentials with the authentication helper. It prompts for each password.

```sh
mkdir -m 700 acl-demo
uv run scripts/duckflight_auth.py user add admin --file acl-demo/duckflight.toml
uv run scripts/duckflight_auth.py user add reader --file acl-demo/duckflight.toml
```

Append this section to `acl-demo/duckflight.toml` after generating the credentials.
The helper keeps it when you change credentials later.

```toml
[acl]
store = "roles.duckdb"
admins = ["local~admin"]
```

DuckFlight creates the role store relative to the config file. Keep its directory private
and writable by the host account. `local~admin` names the authenticated local user who
can issue grants. All listeners on the same host runtime must use the same ACL settings.

**2. Start the host.** In DuckDB 2.0.1, install DuckFlight from the
[Sidequery repository](INSTALL.md#sidequery-repository) and load it. Run from the directory
containing `acl-demo` and leave this DuckDB session open.

```sql
load duckflight from sidequery;
create table orders as select 42 as id, 'account-1234' as account_number;
select * from duckflight_pg_serve('127.0.0.1:5433', 'acl-demo/duckflight.toml');
```

**3. Grant access through the listener.** Open a second terminal and connect with
`psql -h 127.0.0.1 -p 5433 -U admin -d memory -W`. Run these statements as `admin`.

```sql
create role analyst;
grant usage on catalog memory to role analyst;
grant usage on schema memory.main to role analyst;
grant select (id) on table memory.main.orders to role analyst;
grant role analyst to principal 'local~reader';
show grants for role analyst details;
```

**4. Verify and revoke.** In a third terminal, connect with
`psql -h 127.0.0.1 -p 5433 -U reader -d memory -W`. The reader's single assigned
role activates automatically.

```sql
select id from memory.main.orders;             -- returns 42
select account_number from memory.main.orders; -- permission denied
```

Back in the `admin` session, run the following command. Repeat the first query in
the open `reader` session. It now returns permission denied.

```sql
revoke select (id) on table memory.main.orders from role analyst;
```

For a **file-backed native DuckDB table**, run `register table catalog.schema.table`
as the ACL administrator before granting access. This records durable table and column
identities in their comments so the grants survive reopening the same table. An in-memory
table's grants apply to its current instance.

## Mask a column or filter by tenant

Continuing the example, the administrator can expose just the last four characters of
the account number. After this grant, the reader's second query returns `****1234`.

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
verified OIDC claim. The reader still needs the relevant grants. A credential without
a tenant ID cannot read that table. Tenant restrictions also apply to permitted writes.

## Roles, credentials and protocols

- **Roles.** Assign roles to principals, verified OIDC groups or other roles.
  For example, `grant role analyst to group 'oidc:company', 'analytics'` uses the
  configured provider named `company`. Session clients can choose an assigned role
  with `set role analyst`.
- **Credential scopes.** A table grant and the credential's execution scopes both
  apply. A read-only credential cannot use an UPDATE grant. A bearer credential used
  for role administration also needs `query:admin`.
- **Writes.** Grant INSERT, UPDATE and DELETE independently. INSERT and UPDATE can
  be limited to specific columns. Columns read by a write's predicates or expressions
  also need SELECT. Writes run in autocommit on PgWire and Flight SQL.
- **Protocols.** Administer grants over PgWire or Flight SQL. Managed HTTP supports
  reads through `/streaming-query`. Managed DuckFlight Quack supports read-only
  statements and requires reconnecting after a policy revision.
- **Static policies.** For an existing file-based allowlist, set `DUCKFLIGHT_ACL_POLICY`
  before loading the unified extension and use PgWire or Flight SQL. SQL-managed roles,
  tenant row policies and named masks use the role store.
