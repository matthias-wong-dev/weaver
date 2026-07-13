# simple-ses — the canonical Weaver example

One small, complete pipeline that carries data from raw files to a Warehouse
view. Every Weaver concept in the documentation points back here.

The whole example runs on your machine — the Raw and Core tiers need no Fabric
account. The Mart tier is SQL and targets a Fabric Warehouse; it is included so
the example spans the full **Lakehouse → Warehouse** path, and is clearly marked
where Fabric is required.

## The pipeline

```
   SES/Raw  (→ Files)        SES/Core  (→ Delta)        SES/Mart  (→ SQL)

   Sales.CustomerCsv  ─────►  Sales.Customer  ──────┐
   (Folder)                   (Table, snapshot)     │
                                                    ├──►  Sales.CustomerOrderSummary
   Sales.OrderCsv     ─────►  Sales.Order     ──────┘     (View)
   (Folder)                   (Table, incremental)
```

- **Raw** lands source CSVs as managed files. `Sales.CustomerCsv` replaces the
  customer file each run; `Sales.OrderCsv` accumulates monthly extracts.
- **Core** types each file into a Delta table. `Sales.Customer` is a full
  snapshot; `Sales.Order` is **incremental** — past orders are retained as new
  extracts arrive.
- **Mart** joins the two tables into a per-customer summary, built in a Fabric
  Warehouse on top of the Core Delta tables.

Each object is a few lines: a plain-English header saying *what it produces*, and
a `read()` that returns the proposed rows or files. Weaver owns everything
else — ordering, staging, typing the Delta tables, reconciliation, CRUD
accounting, and durable logs. Read the objects under `SES/` top to bottom; they
are meant to be read.

## Layout

```
simple-ses/
├── dbrep-env.yml        # hosts: the SES repo, a local Lakehouse (+ commented Fabric)
├── dbrep-weaver.yml     # database representations and their types
├── SES/
│   ├── Raw/             # Folder objects → Files
│   │   ├── Sales__CustomerCsv.py
│   │   ├── Sales__OrderCsv.py
│   │   └── _helpers/    # ignored by discovery; imported by the objects
│   ├── Core/            # Table objects → Delta
│   │   ├── Sales__Customer.py
│   │   ├── Sales__Order.py
│   │   └── _helpers/
│   └── Mart/            # SQL objects → Warehouse
│       └── Sales.CustomerOrderSummary.sql
└── notebooks/
    └── Load Weaver.ipynb  # minimal Fabric load notebook
```

## Run it locally

From this directory, with Weaver installed (see the repository
[getting-started guide](../../docs/getting-started.md)):

```bash
# Build Raw (Files) and Core (Delta) from their SES sources.
weaver build --config dbrep-weaver.yml \
  --from Raw_SES,Core_SES --to Raw_Files,Core_Delta

# Load in dependency order: the files first, then the tables that read them.
weaver load --config dbrep-weaver.yml --target Raw_Files
weaver load --config dbrep-weaver.yml --target Core_Delta
```

Build output lands in a local Lakehouse under `.lakehouse/` (git-ignored):

```
.lakehouse/
├── Files/Raw/Sales/CustomerCsv/customers.csv
├── Files/Raw/Sales/OrderCsv/orders-2026-01.csv
├── Tables/Core/Sales/Customer/      # Delta table
├── Tables/Core/Sales/Order/         # Delta table
└── Files/_logs/<workflow>/          # one JSON step record per object
```

Loading tables runs Spark/Delta locally, which needs Java 17. To validate the
pipeline without Spark, load only `Raw_Files`, or add `--dry-run` to any build or
load to plan without executing.

## Take it to Fabric

The SES objects and their headers do not change — only the hosts do. Uncomment
the `Fabric_Lakehouse` and `Warehouse` hosts in `dbrep-env.yml`, add the matching
Fabric targets in `dbrep-weaver.yml`, then build to those targets and load with
`notebooks/Load Weaver.ipynb`. The Mart view builds against the Warehouse and
reads the Core Delta tables through the SQL endpoint.

## Where this is used

| To understand… | Read alongside |
|---|---|
| the ideas (SES, objects, dependencies) | [docs/concepts.md](../../docs/concepts.md) |
| a first run, step by step | [docs/getting-started.md](../../docs/getting-started.md) |
| the config files here | [docs/configuration.md](../../docs/configuration.md) |
| how the objects here are written | [docs/authoring.md](../../docs/authoring.md) |
| what build and load do | [docs/build-and-load.md](../../docs/build-and-load.md) |
