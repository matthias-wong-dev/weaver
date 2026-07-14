# Authoring objects

A Weaver object declares **what it produces**; Weaver owns **how the change is
applied, counted, and logged.** Objects never mutate the target directly.

This page is the authoring reference. It follows the
[`examples/simple-ses`](../examples/simple-ses) objects, which are short enough to
read in full.

## The shape of an object

Every object is a file with two parts:

1. a **header** — a small YAML block (in a Python docstring or a SQL `/* … */`
   comment) that declares the object's identity and contract;
2. a **body** — ordinary Python or SQL that produces the result.

Weaver reads the header **statically** to plan the build, and runs the body only
at load time.

Folder and Table objects share one endpoint: `read()` returns a two-item tuple of
**proposed upserts and explicit deletes**.

```text
Folder.read()  ->  staging_folder,    file_names_to_delete
Table.read()   ->  staging_dataframe, primary_key_values_to_delete

Weaver then:
    validates      the pair
    reconciles     upserts and deletes against the target
    mutates        the target
    counts         standard CRUD
    writes         a durable step log
    captures       full errors
    cleans         staging
```

The normal no-delete case for either kind is `return upserts, ()`.

---

## The header contract

The header is a small YAML mapping. These keys are understood:

| Key | Applies to | Meaning |
|---|---|---|
| `Folder ID` / `Table ID` / `View ID` | one, required | the object's `Schema.Object` identity and its kind |
| `Description` | all, required | what the object is, in a sentence |
| `Lineage` | all, required | where its data comes from |
| `Primary key` | Table / SQL | key column, or comma-separated columns for a composite key |
| `Incremental` | Folder / Table | load policy (see [Load policy](#load-policy-incremental)) |
| `File key` | Folder, required | glob(s) identifying the files this folder manages |
| `Schema` | Table | ordered `column: type` mapping; required for Delta tables |

Rules worth knowing up front:

- Exactly **one** of `Folder ID` / `Table ID` / `View ID` must be present — it
  both names the object and picks its kind.
- The ID is two-part `Schema.Object`. The **database** comes from configuration,
  not the header.
- `Description` and `Lineage` must be real text — placeholders like `TBD` or
  `n/a` are rejected.
- You may add your own keys (`Notes`, `Revisions`, `Column notes`, …). Weaver
  keeps them but does not interpret them.

### Filenames follow the ID

The filename must match the declared ID, so the file you see is the object it
declares:

| Kind | ID | Filename |
|---|---|---|
| Python | `Sales.Customer` | `Sales__Customer.py` (`.` → `__`) |
| SQL | `Sales.CustomerOrderSummary` | `Sales.CustomerOrderSummary.sql` |

A Python file must define exactly one Weaver object class, named for the stem
(`class Sales__Customer(Table)`).

---

## Folder objects

A **Folder** produces a managed set of files. `read()` returns:

1. a **`StagingFolder`** whose files are created or updated in the target;
2. a sequence of **relative file names to delete**.

Here is `Sales.CustomerCsv` from the example — it lands the current customer
extract:

```python
"""
Folder ID: Sales.CustomerCsv
Description: Raw customer master snapshot as landed CSV, one file per extract.
Lineage: Writes the current customer extract into the landing folder as customers.csv.
File key: "**/*.csv"
Incremental: false
"""

from pathlib import Path

from ._helpers.sample_source import CUSTOMER_SNAPSHOT
from weaver_runtime.dbrep.objects import Folder


class Sales__CustomerCsv(Folder):
    def read(self):
        with self.staging_folder() as staging:
            for file_name, text in CUSTOMER_SNAPSHOT.items():
                (Path(staging.path) / file_name).write_text(text, encoding="utf-8")

        return staging, ()
```

Inside `staging_folder().path` you use ordinary Python — `pathlib`, `shutil`,
`requests`, `zipfile`, `pandas`, plain file writes. There are no special Weaver
file-write methods. The pair may be returned **inside or after** the `with`
block; both behave identically.

### File keys

Every Folder header declares a **`File key`** — one glob or a list of globs — that
identifies the files the folder manages:

```yaml
File key:
  - "**/*.pdf"
  - "**/*.json"
```

File keys match relative POSIX paths, and each matching path is one managed file
identity. Weaver **fails the load before touching the target** if any staged file
does not match a File key — so temporary download pages or scratch artefacts must
stay *outside* staging unless they are meant to persist.

### Load policy (folders)

Folders default to `Incremental: true`. The policy decides what happens to target
files that are **not** in this run's staging:

- **`Incremental: true`** — files already in the target are **retained**. Staging
  need only carry what is new or changed. This is the pattern for accumulating
  extracts, like `Sales.OrderCsv`:

  ```python
  class Sales__OrderCsv(Folder):
      def read(self):
          destination = Path(str(self.path))
          already_landed = {path.name for path in destination.glob("*.csv")}

          with self.staging_folder() as staging:
              for file_name, text in ORDER_SNAPSHOTS.items():
                  if file_name not in already_landed:
                      (Path(staging.path) / file_name).write_text(text, encoding="utf-8")

          return staging, ()
  ```

- **`Incremental: false`** — staging is the **complete** managed population.
  Weaver deletes managed target files that are absent from staging. This is the
  pattern for a full snapshot, like `Sales.CustomerCsv`.

Reading the destination (`self.path`) for a watermark, as above, is fine.
**Writing** to `self.path` is not — stage instead.

### Deleting files

With `Incremental: true` you may also return explicit file names to delete. They
must be exact relative names that match a File key — never absolute, never a
glob, never `..`-traversing, never a directory:

```python
return staging, ("obsolete/report-2019.pdf",)
```

With `Incremental: false`, explicit deletes must be empty (the snapshot already
implies them). Reserved Weaver files such as `_weaver.json` can never be staged,
replaced, or deleted.

---

## Table objects

A **Table** produces rows. `read()` returns:

1. a **Spark DataFrame** of rows to insert or update;
2. a sequence of **primary-key tuples** identifying rows to delete, in declared
   primary-key column order.

Here is `Sales.Customer` from the example:

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

from ._helpers.csv_frames import read_typed_csv
from weaver_runtime.dbrep.objects import Table


class Sales__Customer(Table):
    def read(self):
        from pyspark.sql import functions as F

        source = self.repo["Raw.Sales.CustomerCsv"]
        typed = read_typed_csv(self.spark, source, self.schema)
        return typed.withColumn("loaded_at", F.current_timestamp()), ()
```

A Delta table **must** declare a `Schema` — Weaver creates the empty table from it
at build time, before any data exists.

### Load policy (incremental)

The `Incremental` flag decides what happens to rows **not** returned by this run:

| `Incremental` | Primary key | Behaviour |
|---|---|---|
| `false` (default) | present | **Full snapshot** — returned rows become the whole table; missing keys are reconciled out. |
| `false` | absent | **Full replacement** — returned rows become the whole table. |
| `true` | required | **Incremental** — missing keys are retained; returned rows are upserted. |

`Sales.Customer` above is a full snapshot. `Sales.Order` is incremental — one line
in the header, and past orders survive across runs:

```python
"""
Table ID: Sales.Order
...
Primary key: order_id
Incremental: true
Schema:
  order_id: string
  ...
"""
```

`Incremental: true` **requires** a primary key — without one there is no way to
know which existing rows to keep.

### Introspecting the current table

A Table can read its own current state through `self.current_dataframe` — the
persisted table as a DataFrame, or `None` before its first load. This is how an
object does genuinely incremental work: skipping already-loaded inputs, or
carrying metadata timestamps forward for unchanged rows.

```python
class Sales__Order(Table):
    def read(self):
        incoming = read_new_orders(self.spark, self.repo["Raw.Sales.OrderCsv"])
        existing = self.current_dataframe
        if existing is None:                       # first load
            return incoming, ()
        already = {row["order_id"] for row in existing.select("order_id").collect()}
        return incoming.where(~F.col("order_id").isin(already)), ()
```

Always guard for the `None` first-load case.

### Deleting rows

There is one deletion authority, and it is strict:

> **No primary key means no row deletion.**

| Primary key | Incremental | Explicit deletes | Result |
|---|---|---|---|
| absent  | false | empty     | allowed (full replacement) |
| absent  | false | populated | **error** |
| absent  | true  | any       | **error** |
| present | false | empty     | derive missing-key deletes |
| present | false | populated | **error** |
| present | true  | empty     | retain missing keys |
| present | true  | populated | apply explicit deletes |

Explicit delete tuples are available only in incremental mode, must match the
primary-key arity, and contain no nulls:

```python
# single-column primary key
primary_key_values_to_delete = (("order-17",), ("order-29",))

# composite primary key, in declared column order
primary_key_values_to_delete = (("agency-a", "2026-07"), ("agency-b", "2026-06"))
```

An append-only audit or event table is just an incremental table whose primary
key uniquely identifies each event.

---

## SQL objects

A **SQL object** is a `.sql` file: a `/* … */` header followed by a `select`.
Weaver builds it into a Warehouse as a self-inferring backing table, a view, and
a load procedure. You write only the query.

`Sales.CustomerOrderSummary` from the example joins the two Core tables:

```sql
/*
View ID: Sales.CustomerOrderSummary

Description: |
    One row per customer with their order count and total order amount.

Lineage: Reads Core.Sales.Customer and Core.Sales.Order.
*/

with order_rollup as (
    select
            o.customer_id
        ,   count(*)        as order_count
        ,   sum(o.amount)   as total_amount
    from Core.Sales.Order as o
    group by
            o.customer_id
)
select
        c.customer_id
    ,   c.customer_name
    ,   coalesce(r.order_count, 0)      as order_count
    ,   coalesce(r.total_amount, 0.00)  as total_amount
from      Core.Sales.Customer as c
left join order_rollup as r on r.customer_id = c.customer_id
```

- **Dependencies are the query's relations.** Weaver reads `from` / `join`
  relations statically: `Core.Sales.Order` and `Core.Sales.Customer` become build
  dependencies. CTEs and aliases (`order_rollup`, `o`, `c`) are not relations and
  are ignored.
- **Two- vs three-part** relations follow the same rule as everywhere: `from
  Sales.Order` is the same database; `from Core.Sales.Order` names the `Core`
  database explicitly.
- Use `View ID` for a view, `Table ID` for a materialised table (which also
  declares a `Primary key`). `Incremental` is not valid on a `View`.

Follow the repository SQL style: lower-case keywords, leading commas, one join
predicate on the line with its table. See [AGENTS.md](../AGENTS.md).

---

## Helpers

Objects stay short by delegating the mechanical work to **helper modules**. A
helper lives in a subfolder **inside the object's own database folder** and is
imported relative to it:

```text
SES/
  Core/                       ← a database (a build source)
    Sales__Customer.py
    Sales__Order.py
    _helpers/                 ← any name works; see the note below
      __init__.py
      csv_frames.py           ← read_typed_csv lives here
```

```python
from ._helpers.csv_frames import read_typed_csv   # in the object
from .csv_frames import _ddl                       # helper → sibling helper
```

Weaver imports every object with **its own database folder as the package root**,
so `from .<subfolder>…` is ordinary Python resolved against that folder. Each
database is its own package, so two databases may ship like-named helpers with
different content and never collide. There is no shared, repo-wide helper package.

**Naming is your choice** — `_helpers`, `helpers`, `lib`, `vendor`, anything.
Discovery scans a database folder's *immediate files* for objects and never
descends into subfolders, so a helper *subfolder* is invisible to it whatever its
name. The one rule: a loose helper *file* placed **directly** beside your objects
(e.g. `Core/csv_frames.py`) would be treated as an object — so either keep helpers
in a subfolder (any name) or prefix such a file with `_` (e.g. `_csv_frames.py`).
The `_` prefix also signals "not an object" to a reader.

---

## The object surface

Inside `read()`, an object reads everything it needs through ergonomic `self.*`
accessors — it never touches `self.context`. The same code runs locally and on
Fabric `abfss://` paths.

| Accessor | Kind | Meaning |
|---|---|---|
| `self.repo["Schema.Object"]` | both | a resolved dependency: a Folder's **path**, or a dependency Table's **DataFrame** |
| `self.path` | both | the destination path, **read-only** — a Delta path for tables, a directory for folders. Do not write here. |
| `self.spark` | both | the active Spark session (`None` for folder-only loads) |
| `self.log_dir` | both | the current workflow's durable log directory |
| `self.staging_folder()` | Folder | a fresh, empty, object-local staging folder |
| `self.schema` | Table | the declared ordered `((column, type), …)` schema |
| `self.primary_key` | Table | the declared primary-key column tuple (empty when none) |
| `self.is_incremental` | Table | the declared incremental policy |
| `self.current_dataframe` | Table | the persisted table as a DataFrame, or `None` if never written |
| `self.empty_frame()` | Table | an empty DataFrame in `self.schema` |

A dependency's representation depends on its kind: `self.repo["Raw.Sales.CustomerCsv"]`
(a Folder) resolves to the folder's **path**, while `self.repo["Core.Sales.Customer"]`
(a Table) would resolve to its **DataFrame**.

---

## Staging in detail

For a Folder, the staging directory is a physical sibling of the target named
`<FolderName>_Staging`. While `Sales.OrderCsv` runs:

```text
Files/Raw/Sales/
├── OrderCsv/
└── OrderCsv_Staging/
```

A new attempt clears and recreates this directory before `read()` runs. Weaver
removes it only after validation **and** reconciliation both succeed. If
authoring, validation, reconciliation, or cleanup fails, staging is **retained
beside the target for diagnosis**; the next deliberate retry clears it before
rebuilding. Only one staging folder may be requested per object step.

This is why direct writes to `self.path` are unsupported: staging is what makes a
partial failure safe. Object code that raises mid-write leaves a retained staging
folder and an untouched target.

---

## Workflow logging

You write no logging code. Each `weaver load` is one **workflow**: Weaver mints a
`{timestamp}_{uuid}` id, creates `Files/_logs/<workflow_id>/`, and writes one
`{timestamp}_{uuid}.json` step record per object **the moment it finishes** —
success or failure. Object and module names live inside the JSON, never in the
filename.

Every step carries a standard CRUD block (`unit: files` or `unit: rows`;
`read` / `created` / `updated` / `deleted`), with kind-specific detail under
`details`. A failed step records the full structured exception — type, message,
traceback, and Spark/SQL error detail where available — and is written before the
error propagates, so earlier successful steps are always preserved.

Because Weaver applied the change, Weaver is the authority on what changed. That
is the whole point of returning a pair from `read()` instead of writing to the
target yourself.

---

## See also

- [Concepts](concepts.md) — objects, databases, and dependencies.
- [Build and load](build-and-load.md) — what happens to an object after `read()`.
- [`examples/simple-ses`](../examples/simple-ses) — every object above, runnable.
