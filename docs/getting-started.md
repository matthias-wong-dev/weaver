# Getting started

Install Weaver and run the [`examples/simple-ses`](../examples/simple-ses)
pipeline end to end — from raw CSVs to typed Delta tables — on your own machine.
No Microsoft Fabric account is needed.

By the end you will have built and loaded four objects and can read the durable
log of exactly what changed.

## Prerequisites

- **Python 3.11 or newer.**
- **Java 17** — only for building local **Delta** tables (Spark runs on your
  machine). The Files half of the walkthrough needs no Java.

## 1. Install Weaver

Clone the repository and install it into a virtual environment. The `[spark]`
extra pulls in PySpark and Delta for local table builds.

```bash
git clone <this-repo> weaver
python3 -m venv .venv
.venv/bin/pip install -e ./weaver[spark]
```

Check the CLI is available:

```bash
.venv/bin/weaver --help
```

```
usage: weaver [-h] {generate,build,load,wipe,fabric} ...
```

> The rest of this guide writes `weaver` for brevity. Use `.venv/bin/weaver`, or
> activate the environment with `source .venv/bin/activate` first.

## 2. Open the example

```bash
cd weaver/examples/simple-ses
```

You are looking at a complete SES and its configuration:

```
simple-ses/
├── dbrep-env.yml        # hosts: the SES repo and a local Lakehouse
├── dbrep-weaver.yml     # the database representations to build
└── SES/
    ├── Raw/     Sales__CustomerCsv.py, Sales__OrderCsv.py   (Folders → Files)
    ├── Core/    Sales__Customer.py, Sales__Order.py         (Tables  → Delta)
    └── Mart/    Sales.CustomerOrderSummary.sql              (SQL → Warehouse)
```

Open `SES/Raw/Sales__CustomerCsv.py` and `SES/Core/Sales__Customer.py`. Each is a
header describing what it produces and a short `read()`. That is all an object is.

## 3. Understand the configuration

Two files, already written for you. `dbrep-env.yml` declares **where things
live**:

```yaml
servers:
  Repo:             { type: SES,             server: SES }        # the SES/ folder
  Local_Lakehouse:  { type: Local Lakehouse, server: .lakehouse } # output on disk
```

`dbrep-weaver.yml` names the **representations** to build. Each is an alias you
pass on the command line:

```yaml
databases:
  Raw_SES:    { type: SES,   server: Repo,            database: Raw }
  Core_SES:   { type: SES,   server: Repo,            database: Core }
  Raw_Files:  { type: Files, server: Local_Lakehouse, database: Raw }
  Core_Delta: { type: Delta, server: Local_Lakehouse, database: Core }
```

`Raw_SES` is the folder you author; `Raw_Files` is what it builds into. Same
database, two representations. Full detail: [Configuration](configuration.md).

## 4. Build

Build the Raw (Files) and Core (Delta) representations from their SES sources.
Weaver discovers the objects, works out their order, installs a runtime bundle,
and creates the empty Delta tables from the declared schemas.

```bash
weaver build --config dbrep-weaver.yml \
  --from Raw_SES,Core_SES --to Raw_Files,Core_Delta
```

To see the plan without building anything, add `--dry-run`:

```bash
weaver build --config dbrep-weaver.yml \
  --from Raw_SES,Core_SES --to Raw_Files,Core_Delta --dry-run
```

```
objects (load order):
  Raw.Sales.CustomerCsv :: Folder -> Files/Raw/Sales/CustomerCsv (deps: none)
  Core.Sales.Customer   :: Table  -> Tables/Core/Sales/Customer (deps: Raw.Sales.CustomerCsv)
  Raw.Sales.OrderCsv    :: Folder -> Files/Raw/Sales/OrderCsv (deps: none)
  Core.Sales.Order      :: Table  -> Tables/Core/Sales/Order (deps: Raw.Sales.OrderCsv)
```

The order was **derived**, not declared: `Core.Sales.Customer` reads
`Raw.Sales.CustomerCsv`, so the folder comes first.

## 5. Load

Loading runs each object's `read()` and lets Weaver reconcile the result into the
target. Load in dependency order — the files first, then the tables that read
them:

```bash
weaver load --config dbrep-weaver.yml --target Raw_Files
weaver load --config dbrep-weaver.yml --target Core_Delta
```

Each load prints a JSON report with one step per object. The Core load types the
CSVs into the Delta tables — here are the two steps, trimmed:

```json
{ "object_id": "Core.Sales.Customer", "kind": "Table", "status": "success",
  "crud": { "unit": "rows", "read": 4, "created": 4, "updated": 0, "deleted": 0 },
  "details": { "reconciliation_ran": true } }
{ "object_id": "Core.Sales.Order", "kind": "Table", "status": "success",
  "crud": { "unit": "rows", "read": 6, "created": 6, "updated": 0, "deleted": 0 },
  "details": { "reconciliation_ran": false } }
```

The difference is the load policy. `Sales.Customer` is a **full snapshot**
(reconciliation compares the incoming rows against the whole table, so
`reconciliation_ran` is true); `Sales.Order` is **incremental** (new orders are
upserted, existing ones retained). You declared that difference in one header
line — `Incremental: true` — and Weaver did the rest.

> **No Java?** Load only `Raw_Files` (Folders need no Spark), or add `--dry-run`
> to any load to validate and order the steps without executing them.

## 6. Inspect the results

The build output is a local Lakehouse under `.lakehouse/`:

```
.lakehouse/
├── Files/Raw/Sales/CustomerCsv/customers.csv
├── Files/Raw/Sales/OrderCsv/orders-2026-01.csv
├── Files/Raw/Sales/OrderCsv/orders-2026-02.csv
├── Tables/Core/Sales/Customer/     ← Delta table (4 rows)
├── Tables/Core/Sales/Order/        ← Delta table (6 rows)
└── Files/_logs/<workflow_id>/      ← one JSON step record per object
```

Open a log file under `Files/_logs/`. Every load is a **workflow**, and every
object gets a step record the moment it finishes:

```json
{
  "object_id": "Core.Sales.Customer",
  "kind": "Table",
  "status": "success",
  "crud": { "unit": "rows", "read": 4, "created": 4, "updated": 0, "deleted": 0 },
  "details": { "accepted": 4, "rejected": 0, "reconciliation_ran": true }
}
```

You wrote no logging code and no counting code. Because Weaver applied the
change, it is the one that knows — and records — exactly what changed.

## 7. Run it again

Re-run the loads. This time Weaver reconciles against what is already there:

```bash
weaver load --config dbrep-weaver.yml --target Raw_Files
```

`Sales.OrderCsv` is incremental and every extract is already landed, so it stages
nothing and reports `created=0`. Loads are safe to repeat.

## What just happened

You authored nothing but four short objects and two config files. Weaver:

- **discovered** the objects and their dependencies from source,
- **ordered** them into a build plan,
- **created** the Delta tables from the declared schemas,
- **staged, reconciled, and applied** each object's result,
- **counted** the file- and row-level CRUD, and
- **logged** every step durably.

That is the payoff of *declare what, own how*: the platform mechanics were the
runtime's job.

## Next steps

- [Concepts](concepts.md) — the model behind what you just ran.
- [Authoring](authoring.md) — write your own Folder, Table, and SQL objects.
- [Configuration](configuration.md) — and how to point the same SES at Microsoft
  Fabric.
- [Command reference](command-reference.md) — every command and option.
