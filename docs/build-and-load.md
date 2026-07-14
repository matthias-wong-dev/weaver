# Build and load

Weaver's runtime has two verbs. **Build** turns an SES into an installed,
self-contained runtime. **Load** runs that installed runtime against a target.
They are deliberately separate: you build once from source, then load — possibly
many times, possibly from a Fabric notebook that never sees your repository.

```
   SES  ──build──►  installed runtime  ──load──►  Files / Delta / SQL
 (source)          (self-contained bundle)        (materialised data)
```

## The lifecycle

```
  weaver build --from <SES…> --to <target…>
  ┌──────────────────────────────────────────────────────────────┐
  │ 1. discover     read object headers statically                │
  │ 2. graph        classify dependencies, topologically order    │
  │ 3. install      write the runtime bundle under the host        │
  │ 4. materialise  create empty Delta tables / SQL DDL by layer   │
  └──────────────────────────────────────────────────────────────┘

  weaver load --target <target>
  ┌──────────────────────────────────────────────────────────────┐
  │ 5. select       pick this target's objects from the catalogue │
  │ 6. order        sort by dependencies internal to the selection│
  │ 7. run          each object: read() → validate → reconcile     │
  │ 8. account      count CRUD, write a durable step log per object│
  └──────────────────────────────────────────────────────────────┘
```

## 1. Discovery

Weaver walks the SES structurally and reads each object's header **statically** —
parsing the docstring or `/* … */` block without importing or executing the file.
Discovery is what makes an SES self-describing:

- database folders are immediate children of the root;
- object files are the immediate, non-`_` files inside them;
- the header yields identity, kind, primary key, schema, and load policy.

Because nothing is imported, a syntax error deep in an object body cannot break
planning, and the plan is computed the same way whether from your repo or from an
installed bundle.

## 2. The dependency graph

Weaver extracts each object's references — `self.repo["…"]` in Python, `from` /
`join` relations in SQL — and classifies them (see
[Concepts](concepts.md#dependency-discovery)). The managed references become edges
in a directed graph, which Weaver topologically sorts into a **stable order**
(deterministic among independent objects) and, where a host runs objects in
parallel, into **layers** (each layer's dependencies all sit in earlier layers).

A cycle is an error, named at build time. For `simple-ses`:

```
layer 0:  Raw.Sales.CustomerCsv,  Raw.Sales.OrderCsv     (independent → parallel)
layer 1:  Core.Sales.Customer,    Core.Sales.Order
layer 2:  Mart.Sales.CustomerOrderSummary
```

## 3. and 4. Build

Build installs a **runtime bundle** under the Lakehouse host at
`Files/_weaver/runtime`: the object sources, a **manifest** of materialisation
paths, a **catalogue** (with a source hash per object), and a **load plan** (the
dependency edges). This bundle is what load consumes — it is complete on its own.

What "materialise" means depends on the target type:

- **Files** — nothing to pre-create; folders are populated at load time.
- **Delta** — Weaver creates each declared table as an **empty, zero-row Delta
  table from its `Schema`**, before any data exists. A table with no declared
  schema fails the build here, with a clear message. Existing tables and their
  data are left untouched.
- **SQL** — Weaver executes DDL **layer by layer** against the Warehouse: a
  self-inferring backing table, a view, and a load procedure per object. It
  records managed objects in a `_weaver.objects` table so it knows what it owns.

Build is **idempotent** and side-effect-ordered: schemas are validated and build
programs rendered *before* anything is created, so a bad schema fails before any
table exists.

### Pruning

`--prune` compares the objects now in the SES against what the installed runtime
last recorded, and drops managed objects that have been removed — only ever the
ones Weaver created. Without `--prune`, removed objects are left in place.

### Building to Fabric

A Fabric Lakehouse target stages the bundle locally, then transfers `Files/` to
OneLake: the Weaver-owned `Files/_weaver/runtime` transfers with a signature diff
and scoped delete; object folders sync without deleting anything else. The same
generated program runs locally and on Fabric — only the transport differs.

## 5. to 8. Load

`weaver load --target <target>` runs the **installed runtime**, not the SES. It
never needs `--from` and never reads your repository.

Load first **validates** the bundle: it re-discovers the installed objects and
checks them against the catalogue, including a **source-hash check** per object.
If an installed source has been tampered with, load fails rather than running
something the build never planned.

It then **selects** the objects that belong to the requested target and orders
them by the dependencies *internal to that selection*. Dependencies outside the
selection — like a Delta table reading a Files folder in another representation —
are expected to be materialised already, which is why you load upstream targets
first.

Each selected object then runs:

- a **Folder** — `read()` stages files; Weaver reconciles them into the target;
- a **Table** — `read()` returns a DataFrame; Weaver applies the load policy.

The very same generated load program runs locally (via `exec`) and on Fabric (via
a Spark job over Livy). Logging and reconciliation are identical across both.

## Reconciliation

Reconciliation is where "declare what, own how" becomes concrete. Weaver compares
what `read()` returned with what is already in the target, and applies the
minimal change.

**Files** are reconciled by identity and content:

- a staged file that is new → **created**;
- a staged file whose size or bytes differ → **updated**;
- a staged file identical to the target → **read** (unchanged);
- under `Incremental: false`, a managed target file absent from staging →
  **deleted**; under `Incremental: true`, retained.

Only files matching a File key are ever counted, changed, or deleted — Weaver
never rescans or touches unmanaged files.

**Rows** are reconciled by primary key:

- keyed staging is first cleansed — rows with a blank key are **rejected**, and
  for a duplicated key one representative is kept and the surplus rejected;
- the accepted rows drive inserts and updates;
- under a full snapshot, keys missing from the run are **deleted**; under
  incremental, they are **retained**; explicit delete tuples are applied only in
  incremental mode.

Rejected rows never block valid rows from loading, and the step's `details`
record how many rows were accepted, rejected, and whether reconciliation ran.

## Workflow logging

Every load is one **workflow**. Weaver writes `Files/_logs/<workflow_id>/` and, as
each object finishes, a JSON **step record** — success or failure — carrying the
CRUD block and kind-specific details. A failure records the full structured
exception and is written *before* the error propagates, so the record of earlier
successful steps is never lost. This is the durable, uniform account of what a
load did, produced because Weaver — not your object — performed the change.

## generate: artifacts without applying

`weaver generate` runs discovery, the graph, and rendering, but **applies
nothing**. SQL targets emit the executable DDL scripts plus a `plan.json`;
Lakehouse targets stage the full runtime bundle under `--out`; Fabric targets are
staged locally and never uploaded. Use it to review exactly what a build would
install or execute — in a pull request, or before running against production.

## wipe: remove materialisations

`weaver wipe --target <target>` removes a representation's materialised data — a
Files database's directory, a Delta database's tables, or a SQL target's managed
objects. It is the inverse of what build and load create, and it only ever
touches what Weaver manages.

## See also

- [Command reference](command-reference.md) — every flag on these commands.
- [Authoring](authoring.md) — the `read()` contract that load executes.
- [Concepts](concepts.md) — discovery, dependencies, and workflows.
