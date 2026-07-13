# Concepts

This page builds the Weaver mental model. It uses the
[`examples/simple-ses`](../examples/simple-ses) repository throughout, so the
ideas stay concrete.

## The one idea

An object **declares what it produces**; Weaver **owns how the change is applied,
counted, and logged.**

Everything below is machinery in service of that separation. You describe the
result you want to exist. Weaver makes it exist, works out what changed, and
records it.

## The naming stack

Every managed thing has a five-part identity:

```
server . database . type . schema . object
```

| Part | What it is | Example |
|---|---|---|
| **server** | a *host* — where things physically live | `Local_Lakehouse` |
| **database** | a named representation, a folder of objects | `Core` |
| **type** | the representation type | `Delta` |
| **schema** | a namespace within a database | `Sales` |
| **object** | a folder, table, or view | `Customer` |

You rarely write the whole stack. Inside an object you refer to a dependency by
its `Schema.Object` (same database) or `Database.Schema.Object` (another
database). The rest is resolved from configuration.

## Repository and SES

A **repository** is a folder you author and keep in source control. When that
folder holds Weaver objects, it is a **Standard Extract Script (SES)**.

An SES has a simple, discoverable shape:

```
SES/
├── Raw/                       ← a database
│   ├── Sales__CustomerCsv.py     ← an object (Folder)
│   ├── Sales__OrderCsv.py        ← an object (Folder)
│   └── _helpers/                 ← ignored by discovery, importable by objects
├── Core/                      ← a database
│   ├── Sales__Customer.py        ← an object (Table)
│   └── Sales__Order.py           ← an object (Table)
└── Mart/                      ← a database
    └── Sales.CustomerOrderSummary.sql   ← an object (SQL view)
```

The rules are structural, so there is nothing to register:

- Immediate child folders of the root are **databases**.
- Files directly inside a database folder are **objects**.
- Any name beginning with `_` — a folder or a file — is skipped by discovery.
  This is how helper modules live beside objects without being mistaken for one.

## Database

A **database** is a named group of objects — a folder in the SES. In the example,
`Raw`, `Core`, and `Mart` are databases. They are also **tiers**: `Raw` lands
source files, `Core` types them into tables, `Mart` shapes them for reporting.
Tiering is a convention, not a requirement — Weaver only sees databases and the
dependencies between their objects.

The same database name appears on both sides of a build. `Raw` names both the SES
source you author *and* the `Files` representation it builds into. Which one you
mean is a matter of the alias you use in configuration (see
[Environment and target](#environment-and-target)).

## Object

An **object** is one buildable, loadable thing. There are three kinds, chosen by
the base class you subclass (Python) or the ID you declare (SQL):

| Kind | Produces | `read()` returns |
|---|---|---|
| **Folder** | a managed set of files | `(staging_folder, file_names_to_delete)` |
| **Table** | a managed table | `(staging_dataframe, primary_key_values_to_delete)` |
| **View** (SQL) | a warehouse view or table | *(a `select` body, not a `read()`)* |

Every object begins with a **header** — a small block of YAML in a Python
docstring or a SQL `/* … */` comment — that declares its identity and contract:

```python
"""
Table ID: Sales.Customer
Description: One row per customer, typed from the landed customer snapshot.
Lineage: Reads the customer CSV landed in Raw.Sales.CustomerCsv; the current file is the whole table.
Primary key: customer_id
Schema:
  customer_id: string
  customer_name: string
  segment: string
  signup_date: date
  loaded_at: timestamp
"""
```

Weaver reads the header **statically** — without importing or running the file —
to plan the build. The body runs only at load time. The header is the contract;
the body is the implementation. Full detail in [Authoring](authoring.md).

### Folder

A **Folder** produces files. Its `read()` stages the files it wants to exist and
returns them, plus any file names to delete. Weaver reconciles the staged files
against the destination and counts file-level CRUD. Object code never writes to
the destination directly — it writes to a Weaver-issued staging folder.

### Table

A **Table** produces rows. Its `read()` returns a Spark DataFrame of rows to
exist, plus any primary keys to delete. Weaver applies the table's load policy
(full replacement or incremental upsert), reconciles against the current table,
and counts row-level CRUD.

### SQL object

A **SQL object** is a `.sql` file: a header block followed by a `select`. Weaver
builds it into a Warehouse as a self-inferring backing table, a view, and a load
procedure. Its dependencies are read from the `from` / `join` relations in the
query.

## Environment and target

Weaver keeps *what you build* separate from *where it lives*, in two config files.

An **environment** (`dbrep-env.yml`) declares **hosts** — a server is a place:

```yaml
servers:
  Repo:             { type: SES,             server: SES }
  Local_Lakehouse:  { type: Local Lakehouse, server: .lakehouse }
```

The **database representations** (`dbrep-weaver.yml`) name each buildable thing
and bind it to a host:

```yaml
databases:
  Core_SES:   { type: SES,   server: Repo,            database: Core }
  Core_Delta: { type: Delta, server: Local_Lakehouse, database: Core }
```

`Core_SES` and `Core_Delta` are the same `Core` database in two representations:
the SES you author, and the Delta tables it builds into. Each named
representation is an **alias** you pass on the command line. A **target** is
simply the representation you are building *to* or loading:

```bash
weaver build --from Core_SES --to Core_Delta
weaver load  --target Core_Delta
```

There is no fixed source/target split in the config — both are just
representations, and the command decides direction. Switching from a local
Lakehouse to Microsoft Fabric is a change of hosts in `dbrep-env.yml`; the SES
and its objects do not change. Full reference: [Configuration](configuration.md).

## Dependency discovery

Weaver learns the dependency graph by **reading object source statically** — it
never imports a module to find out what it needs.

- **Python** objects declare dependencies by reading them:
  `self.repo["Raw.Sales.CustomerCsv"]`.
- **SQL** objects declare them by querying them: `from Core.Sales.Order`.

A reference is classified by how many parts it has, relative to the object's own
database and the databases supplied to the build:

| Reference | Meaning |
|---|---|
| `Schema.Object` | same database |
| `Database.Schema.Object` | another database, if that database is in the build; otherwise external |
| `Server.Database.Schema.Object` | external — recorded, never built or loaded |

**External** references are real dependencies that live outside what Weaver
manages (a source system, a hand-maintained table). Weaver records them for
lineage but never tries to build, order, or load them.

### The example's graph

From those references alone, Weaver derives the order for `simple-ses`:

```
Raw.Sales.CustomerCsv ─► Core.Sales.Customer ─┐
                                              ├─► Mart.Sales.CustomerOrderSummary
Raw.Sales.OrderCsv    ─► Core.Sales.Order    ─┘
```

`Core.Sales.Customer` reads `self.repo["Raw.Sales.CustomerCsv"]`, so the folder
must load first. `Mart.Sales.CustomerOrderSummary` selects `from
Core.Sales.Customer` and `from Core.Sales.Order`, so both tables must build
first. Independent objects (the two folders) may run in parallel; a cycle is an
error. You never write this order down — it is discovered.

## Workflow

A **workflow** is one `weaver load` invocation. Weaver mints a
`{timestamp}_{uuid}` workflow id and writes one JSON **step record** per object
under `Files/_logs/<workflow_id>/` the moment that step finishes — success or
failure.

Each step carries a standard CRUD block:

```
Folder → unit: files    read / created / updated / deleted
Table  → unit: rows     read / created / updated / deleted
```

A failed step records the full structured exception and is written before the
error propagates, so the logs of earlier successful steps are always preserved.
The workflow log is the durable, uniform record of what every load did — because
Weaver, not your object, performed and counted the change. See
[Build and load](build-and-load.md).

## Where next

- [Getting started](getting-started.md) — run the example.
- [Authoring](authoring.md) — write your own objects.
- [Configuration](configuration.md) — the two config files in full.
