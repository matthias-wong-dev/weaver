# Weaver

**A data engineering runtime for Microsoft Fabric.**

Build and operate data platforms from **Lakehouse to Warehouse** using ordinary
Python and SQL.

Weaver is a desktop Python CLI. You author a source-controlled repository of
small objects — each one a plain-English header and a short `read()` — and
Weaver discovers them, works out the order they depend on each other, and builds
and loads them into files, Delta tables, and SQL tables.

Supported representations today:

- **Files**
- **Delta Tables**
- **SQL Tables**

On the roadmap: Semantic Models.

> **Active development.** Weaver is pre-1.0. Public contracts, configuration, and
> examples may change before the first stable release.

---

## The idea

An object **declares what it produces**. Weaver **owns how the change is
applied, counted, and logged.**

That single separation is the whole of Weaver. You never write code that mutates
a target, diffs it, counts what changed, retries it, or logs it. You return the
rows or files you want to exist; Weaver reconciles them into the destination and
keeps the books.

```python
from ._helpers.csv_frames import read_typed_csv
from weaver_runtime.dbrep.objects import Table


class Sales__Customer(Table):
    def read(self):
        from pyspark.sql import functions as F

        source = self.repo["Raw.Sales.CustomerCsv"]
        typed = read_typed_csv(self.spark, source, self.schema)
        return typed.withColumn("loaded_at", F.current_timestamp()), ()
```

`read()` returns a pair: **the rows to exist, and the keys to delete** (here,
none). Weaver validates the pair, reconciles it against the current table,
applies the change, counts the row-level CRUD, and writes a durable log — the
same way for every object, whether it lands on your disk or in Fabric.

---

## Why Weaver

### Strong engineering foundations, by default

The hard parts of a data platform are the runtime's job, not yours:

> dependency discovery · execution ordering · schema creation · staging ·
> reconciliation · incremental loading · CRUD accounting · workflow logging ·
> validation · fault tolerance

You get them by returning a pair from `read()` — not by wiring them up.

### Declarative where it matters

Every object begins with a plain-English header describing what it produces —
its identity, primary key, schema, and load policy. Weaver reads those headers
statically to plan the build. Intent lives at the top of the file, in prose.

```
Table ID: Sales.Order
Description: One row per order, typed from the landed order extracts.
Lineage: Reads the order CSV extracts landed in Raw.Sales.OrderCsv; past orders are retained.
Primary key: order_id
Incremental: true
Schema:
  order_id: string
  customer_id: string
  order_date: date
  amount: decimal(12,2)
  loaded_at: timestamp
```

### Natural for developers

Objects are ordinary Python and SQL. You read a CSV with Spark, shape a
DataFrame, write a `select`. There are **no templates, no DSL, no code
generators** — nothing between you and the language you already know.

---

## Mental model

```
   Repository (SES)                Weaver runtime                Representations

   Folder / Table / SQL  ──►  discover ─► order ─► build ─► load  ──►  Files
   objects with headers                                               Delta Tables
                                                                      SQL Tables
```

- You author an **SES** — a Standard Extract Script repository.
- Weaver **discovers** every object, builds the **dependency graph**, and
  **executes** objects in order.
- The same objects build to a **local Lakehouse** on your disk or to **Microsoft
  Fabric** — only the configured hosts change.

See [docs/concepts.md](docs/concepts.md) for the full model.

---

## What is an SES?

A **Standard Extract Script** is a source-controlled repository of Weaver
objects: ordinary Python and SQL files, each opening with a declarative header.
Weaver discovers everything in it automatically — there is nothing to register.

```
SES/
├── Raw/                       a database (a build source)
│   ├── Sales__CustomerCsv.py    Folder → Files
│   └── Sales__OrderCsv.py
├── Core/
│   ├── Sales__Customer.py       Table  → Delta
│   └── Sales__Order.py
└── Mart/
    └── Sales.CustomerOrderSummary.sql   SQL → Warehouse
```

Folders under the root are **databases**; the files directly inside them are
**objects**. Anything named with a leading `_` (like `_helpers/`) is for you to
import and is invisible to discovery.

---

## Install

Weaver installs from source into a virtual environment.

```bash
git clone <this-repo> weaver
python3 -m venv .venv
.venv/bin/pip install -e ./weaver          # add [spark] for local Delta builds
.venv/bin/weaver --help
```

Local Delta table builds run Spark on your machine and need Java 17. Files and
SQL work need neither. Full instructions: [docs/getting-started.md](docs/getting-started.md).

---

## First run

The [`examples/simple-ses`](examples/simple-ses) repository is a complete, runnable
pipeline. From that directory:

```bash
# Build the Raw (Files) and Core (Delta) representations from their SES sources.
weaver build --config dbrep-weaver.yml --from Raw_SES,Core_SES --to Raw_Files,Core_Delta

# Load in dependency order.
weaver load --config dbrep-weaver.yml --target Raw_Files
weaver load --config dbrep-weaver.yml --target Core_Delta
```

Build output lands in a local Lakehouse under `.lakehouse/`, and every load
writes one JSON step record per object under `Files/_logs/`.

---

## The example

[`examples/simple-ses`](examples/simple-ses) is the canonical reference used
throughout the documentation — one small pipeline from raw files to a Warehouse
view:

```
Sales.CustomerCsv ─► Sales.Customer ─┐
(Folder → Files)     (Table → Delta) │
                                     ├─► Sales.CustomerOrderSummary
Sales.OrderCsv    ─► Sales.Order    ─┘   (SQL View → Warehouse)
(Folder → Files)     (Table → Delta)
```

It shows a Folder that lands source files, Tables that type them (one full
snapshot, one incremental), a helper module, a SQL view that joins them, and a
Fabric load notebook. Read it top to bottom — the objects are meant to be read.

---

## Documentation

| Guide | What it covers |
|---|---|
| [Concepts](docs/concepts.md) | The mental model: SES, databases, objects, environments, targets, dependencies |
| [Getting started](docs/getting-started.md) | Install, build, and load the example, step by step |
| [Configuration](docs/configuration.md) | `dbrep-env.yml` and `dbrep-weaver.yml` in full |
| [Authoring](docs/authoring.md) | Writing Folder, Table, and SQL objects |
| [Build and load](docs/build-and-load.md) | The runtime lifecycle end to end |
| [Command reference](docs/command-reference.md) | Every public CLI command |

Building on Weaver with an AI coding agent? See [AGENTS.md](AGENTS.md).
