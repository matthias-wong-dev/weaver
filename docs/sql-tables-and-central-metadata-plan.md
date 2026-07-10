# Weaver: SQL Table Support + Central Metadata Catalogue — Implementation Handover

> **Audience:** an engineering agent with no prior context on this repo or these
> conversations. Read this document top-to-bottom before writing code. It tells
> you what Weaver is, the small set of files you must read (so you don't read the
> whole repo), the design philosophy you must preserve, and the ordered stages to
> implement.

---

## 1. What Weaver is

`weaver` is a Python CLI + library that builds and loads **database
representations** on Microsoft Fabric. A "database representation" is a typed,
named collection of objects. Four types exist:

- **SES** — the *source* representation: a folder of authored object files
  (`.sql` or `.py`), each carrying a metadata header. SES is always the `--from`;
  the other three are always the `--to`.
- **Files** — a Lakehouse folder tree.
- **Delta** — Lakehouse Delta tables.
- **SQL** — a Fabric **Warehouse** (T-SQL).

The lifecycle commands are `generate` / `build` / `load` / `wipe`. `build` reads
SES objects, works out their dependency graph, and materialises them into a
target. Each SES object declares itself as a `Folder`, `Table`, or `View` via a
metadata header (YAML in a Python docstring or a leading SQL `/* ... */` block).

Two backends exist today:

- **Lakehouse backend** (Files/Delta) — builds by rendering **one deterministic
  Python program string** and executing it verbatim, either locally (`exec`) or
  on Fabric via Livy. This is the model we want everywhere.
- **SQL backend** (Warehouse) — currently builds **imperatively in the CLI
  process** over a live pyodbc connection (computes layers in Python, runs DDL
  per object with a thread pool, reads/writes a `_weaver.objects` table). This is
  the divergent path this project converges.

### The concrete trigger for this work

The ILG product has SES **views** stacked on views. On the nightly load, the
warehouse must fully re-expand these view-on-view chains on every read, and it's
slow. The fix is to let the non-DWG intermediate objects be declared as real
`Table` objects (materialised once per load) so the DWG views on top read from
tables instead of nested view logic. Everything in this plan exists to make that
conversion **safe and repeatable** — not just possible once. The final
acceptance is: convert those ILG SES objects from `View ID:` to `Table ID:`,
build+load, and measure the nightly load speedup.

---

## 2. Come up to speed fast — read exactly these

Do **not** read the whole repo. Read these, in this order. Line references are
current at time of writing; re-grep if they've drifted.

### 2a. Orientation (read first)

| File | Why |
| --- | --- |
| `AGENTS.md` | Repo rules: environment-neutral (no product/workspace/name defaults in `src/`), lazy imports keep PySpark/pyodbc/azure out of core, SQL style guide. Tests: `../.venv/bin/python -m pytest` from `weaver/` runs core only; `-m fabric` / `-m spark` opt in. |
| `src/weaver_runtime/dbrep/ses/metadata.py` | The SES object metadata model. `ObjectMetadata`, `parse_object_metadata`, `_parse_bool`. This is where the new `Prohibit rebuild:` header is parsed. Note `FOLDER`/`TABLE`/`VIEW` kinds, `Static`, `Load mode`, `Auto delete`, `Primary key`, `Schema`. |
| `src/weaver_runtime/dbrep/build/planner.py` | `plan_build` → `BuildPlan`. Discovers SES objects, classifies dependencies (intra/cross/external), topologically orders them, produces `PlannedObject`s bound to targets. This is the polymorphic front half of build — already target-agnostic. |
| `src/weaver_runtime/dbrep/cli/commands.py` | `run_build` / `run_generate` / `run_load` / `run_wipe`. Shows how the three backends are dispatched today and where the metadata-Lakehouse parameter must be threaded in. |

### 2b. The Lakehouse "generate a program" model (the pattern to copy)

| File | Why |
| --- | --- |
| `src/weaver_runtime/dbrep/lakehouse/programs.py` | `render_build_program` / `render_load_program`. **This is the template for the whole project**: build renders one deterministic Python string from the plan alone (no target queried), and the transport executes it verbatim. Currently the build program only does Delta table init — it does NOT yet do catalogue/dirty/populate. That's what we extend. |
| `src/weaver_runtime/dbrep/lakehouse/artifacts.py` | Groups plan objects by physical host, orders them, renders the program, stages `Files/`. `generate_lakehouse_artifacts` (used by `generate`) and `render_host_program` (used by `build`) render the **same** string. |
| `src/weaver_runtime/dbrep/execution.py` | `execute_program_local` — the local twin of the Fabric Livy submitter. `exec`s the program string against globals `spark`, `WEAVER_RUNTIME_ROOT`, `WEAVER_SPARK_ROOT`; requires it to set `WEAVER_RESULT`. Knows nothing about what the program does. |
| `src/weaver_runtime/dbrep/runtime/initialise.py` | `delta_specs_from_plan` / `initialise_delta_tables`. Example of "specs derived purely from SES metadata → rendered into the program → same function runs local and Fabric." |
| `src/weaver_runtime/dbrep/fabric/lakehouse.py` | The Fabric transport: stage bundle → OneLake sync → submit the program via Livy. Confirms local `exec` and Fabric Livy run the identical program string. |
| `src/weaver_runtime/dbrep/build/runtime_bundle.py` | `install_build` + `_merged_runtime_metadata`. **Already computes** `build_catalogue` / `build_load_dependency` / the four dictionaries from `PlannedObject`s and does a **two-way merge/diff** against the previously-installed catalogue — but writes them as JSON under `Files/_weaver/runtime`, and only for Lakehouse targets. Much of the metadata-population logic you need already lives here; it needs generalising and retargeting to real tables. |
| `src/weaver_runtime/dbrep/build/manifest.py` | The builders: `build_catalogue`, `build_load_dependency`, `build_table_dictionary`, `build_column_dictionary`, `build_index_dictionary`, `build_foreign_key_dictionary`, plus `source_hash`. These produce the metadata content from planned objects. Reuse these. |

### 2c. The SQL backend (the divergent path to converge)

| File | Why |
| --- | --- |
| `src/weaver_runtime/dbrep/sql/backend.py` | `build_sql_target` / `load_sql_target` / `wipe_sql_target`. The current imperative orchestration: schemas + `_weaver.objects` manifest table, layer-by-layer DDL via `ThreadPoolExecutor`, load-proc install, manifest upsert, prune. **`_weaver.objects` and its `MANAGED_SCHEMA="_weaver"` were invented ad-hoc by a prior agent — not part of the intended design; treat as scaffolding to replace.** Note the source-hash is already computed (`_source_hash`) but never read back for comparison — dirty-detection is not wired. |
| `src/weaver_runtime/dbrep/sql/ddl.py` | Generates the per-Table DDL: self-inferring backing table + history table + view. `generate_infer_create_table_sql`, `wrap_create_or_alter_view`, `_derive_table_names`, `build_create_table_sql_from_describe_rows`. **Key gap:** the create is a bare `create table` with no `if not exists`/drop guard — rebuilding an existing table errors. `_render_primary_key_sql` uses `not enforced` (see §7 — Fabric platform limit, not a bug). |
| `src/weaver_runtime/dbrep/sql/etl.py` | Generates the ETL load stored procedure (staging → check → upsert/reject → apply → history). This is correct and stays. |
| `src/weaver_runtime/dbrep/sql/wrangle.py` | T-SQL text tooling: `insert_where_one_eq_zero`, `insert_ctas`, `insert_select_into`, `render_sql_template`, `find_sql_dependencies`. Important: these operate on the **last standalone SELECT** and pass everything before it (`DECLARE`/`WHILE`/`IF`/temp-table population) through untouched — so a `WHILE`-loop SES table body works with this machinery unchanged. |
| `src/weaver_runtime/dbrep/sql/connection.py` | `connect` / `execute_script` / `query` — the pyodbc transport. The "hand a sequence of SQL to the target" executor for the SQL backend. |
| `source/sql_templates/**` | The SQL templates (`ddl/`, `etl/`) rendered by `ddl.py`/`etl.py`. `backing_table_and_view.sql`, `infer_create_table.sql`, `load_proc.sql`, `primary_key_body.sql`, `full_refresh_body.sql`. |

### 2d. Config / resolution (where the metadata-Lakehouse parameter goes)

| File | Why |
| --- | --- |
| `src/weaver_runtime/dbrep/config/databases.py` | `databases:` block parsing → `DatabaseConfig`. Named representations with `type`/`server`/`database`. |
| `src/weaver_runtime/dbrep/config/environment.py` | `servers:` block → `ServerConfig` (`server`, `degrees_of_parallelism`, `platform: local|fabric`). |
| `src/weaver_runtime/dbrep/config/resolution.py` | `ResolvedDatabase` (+ `is_sql`/`is_delta`/`is_files`/`is_lakehouse`/`is_fabric`), `RUNTIME_RELATIVE_ROOT`, path helpers. The metadata-Lakehouse reference resolves through here. |
| `tests/dbrep_helpers.py` (`write_config_files`), `tests/fixtures/generic_ses/{env.yml,weaver.yml}` | The config shape used by tests. |

### 2e. Load orchestration reference (external — read during the load stages)

The load-orchestration and load-mechanics design comes from the author's own
prior work. The implementing agent will have **no idea** what `LoadStack` /
`LoadCandidate` / `RefreshBookmark` mean without reading these. Read them when you
reach the load-orchestration stages (§6, Stages 6–7):

- Load orchestration (LoadDependency / LoadStack / LoadCandidate, worker claim loop):
  <https://principlesofdataengineering.org/docs/efficient-stable-pipeline/load-orchestration/>
- Load mechanics (staging → check → upsert/reject → apply → history; the artifact tables):
  <https://principlesofdataengineering.org/docs/efficient-stable-pipeline/load-mechanics/>
- Tracking changes (refresh bookmarks; source-timestamp incremental extraction — **distinct** from build signatures):
  <https://principlesofdataengineering.org/docs/efficient-stable-pipeline/tracking-changes/>
- Responding to change (driver sets; how source changes map to target actions):
  <https://principlesofdataengineering.org/docs/efficient-stable-pipeline/responding-to-change/>

Note: the load **mechanics** (staging/upsert/reject/history) are already
implemented in `sql/etl.py` and `source/sql_templates/etl/` and are correct —
those stages are about **orchestration** (which objects to load, in what order,
by which worker), not the per-table mechanics.

---

## 3. Guiding philosophy (do not violate)

### 3a. The build phase is completely polymorphic

One conceptual build applies identically across:

- **Fabric Lakehouse Folders** (+ its local filesystem test proxy)
- **Fabric Lakehouse Delta** (+ its local Spark/Delta test proxy)
- **Fabric Warehouse** (SQL)

The **front half** (discover SES → classify deps → topological order →
`PlannedObject`s) is already shared and target-agnostic (`build/planner.py`). The
**back half** — deciding *what to do* per object and *emitting the program* — must
become equally shared in concept, differing only in the emitted language.

### 3b. Generate from the repo, hand to the target to execute

- **Warehouse:** generate a sequence of **SQL** from the repo — **and nothing but
  the repo** — and hand it to the target to execute over **odbc**.
- **Lakehouse:** after uploading the runtimes, generate a sequence of **Python**
  to execute — over **Livy** (Fabric) or `exec` (local proxy).

Both are **one deterministic program, rendered from SES metadata alone**, needing
**zero information queried from the warehouse/lakehouse**. The code is
**self-discovering**: at execution time it discovers what already exists in the
target and reconciles, but the *program that is generated* depends only on the
repo. This is why the same program string can be produced by `generate` (dry) and
`build` (applied), and why a build is reproducible and inspectable.

### 3c. One unified generate algorithm (both engines)

The generated program — SQL sequence or Python sequence — is **one set of
statements in topological layers** performing, in order:

1. **Prune unknown objects** — drop anything in the target that isn't declared in
   the repo (reconciled against the target's actual object inventory at execution
   time).
2. **Compute the topological dependency** order (from the repo).
3. **Dirty the catalogue** (if it exists) — delete the catalogue rows for objects
   whose source signature changed, plus everything downstream (transitively), and
   drop those objects up front. See §4 for why "up front."
4. **Execute DDL in topological layers** — create/replace the (dirty + missing)
   objects. Views are always re-created (cheap); Tables are dropped+recreated when
   dirty (see §4).
5. **Build the metadata dictionary tables if they don't exist** — `_.Catalogue`
   and the dictionaries are themselves declared SES objects (see §5); creating
   their shape is part of the same layered program.
6. **Populate the metadata tables with CRUD** — a diff (upsert/delete) computed
   from the repo, applied to `_.Catalogue`, `_.TableDictionary`,
   `_.ColumnDictionary`, `_.IndexDictionary`, `_.ForeignKeyDictionary`,
   `_.LoadDependency`.
7. **Execute post-DDL code** — e.g. run the ETL that loads `_.ExpandedDependency`
   (the transitive closure of `_.LoadDependency`). This is an *ordinary generated
   load*, not a special step: `_.ExpandedDependency` is a SES `Table` with a SQL
   body and a generated load stored procedure, and it depends on
   `_.LoadDependency`, so normal dependency ordering runs it after its input is
   fresh. **There is no bespoke "materialise" mechanism.**
8. **Populate the catalogue as a confirmation** — re-insert each object's
   catalogue row after that object's own rebuild succeeds. Row presence is the
   commit marker: a crash between drop (step 3) and this re-insert leaves the row
   missing, which the next run reads as "still dirty" and repairs — the normal
   dirty-detection *is* the crash-recovery path.

Everything above is generated from the repo. The transport (odbc / Livy / `exec`)
stays generic and knows nothing about what the program does — exactly as
`execution.py` already does for Lakehouse.

---

## 4. Rebuild mechanics (settled decisions)

- **Dirty signal = source hash** of the object's SES source text (already computed
  by `manifest.source_hash` / `sql/backend._source_hash`). Any change to the
  authored source marks the object dirty. Deliberately coarse: any ETL-logic
  change forces a table rebuild. A narrower "update just the load proc" escape
  hatch is **explicitly deferred** — do not build it now.
- **Recreate, not alter.** A dirty Table is **dropped and recreated**, not
  schema-altered in place. Acceptable because analytics tables are backed by an
  upstream source of truth; schema evolution is a later project. (The
  Current/History pair is dropped and rebuilt together.)
- **Downstream propagation.** Anything depending — directly or transitively — on a
  dirty object is forced dirty too, regardless of its own hash. (Forward walk of
  the same dependency graph the planner already builds.)
- **Views are free** — always `create or alter`, never dirty-gated.
- **Drop up front, in one pass.** Compute the full drop set (dirty + downstream +
  unexpected/unmanaged objects found in the target) **before dropping anything**,
  then drop it all in a single unordered pass. Safe to be unordered because Fabric
  Warehouse does not enforce FK/PK constraints (see §7). Delete the matching
  catalogue rows at drop time. Rebuild in topological order; re-insert each
  catalogue row only after its object's rebuild succeeds (step 8 above). Dropping
  up front prevents a half-built run leaving downstream tables holding stale data
  with no upstream.
- **`Prohibit rebuild: true`** — a new **optional** SES metadata header, valid on
  any object of any language. It exempts an object from the drop-on-signature-change
  rule (dirty-detection skips it; downstream propagation still can't force it).
  Its main purpose: the metadata tables themselves (`_.Catalogue` etc.) must not be
  wiped when their own SES source is edited, or they'd lose accumulated state.
  **Deleting the SES file still removes the object** regardless of the flag (file
  deletion is higher-order than the clause). It is **mandatory to state** on
  `Folder` objects (Folders are always `true`; make the choice explicit rather than
  silently defaulting — enforce like other required metadata in `metadata.py`).

---

## 5. Metadata table taxonomy (settled)

Two groups, split by **churn**, not by engine.

### 5a. Central metadata dictionary (low-churn, rebuilt wholesale per build)

Declared as **SES objects shipped with weaver** (they are weaver-owned), all with
`Static: true` + `Prohibit rebuild: true`, all built by the normal build:

| Object | Authored as | Populated by |
| --- | --- | --- |
| `_.Catalogue` | **empty Python** SES object (shape only: database, schema, name, object type, object role, signature) | generated **CRUD diff** (step 6) |
| `_.TableDictionary` | empty Python SES object | generated CRUD diff |
| `_.ColumnDictionary` | empty Python SES object | generated CRUD diff |
| `_.IndexDictionary` | empty Python SES object | generated CRUD diff |
| `_.ForeignKeyDictionary` | empty Python SES object | generated CRUD diff |
| `_.LoadDependency` | empty Python SES object (direct deps; 3-part name; internal-vs-external flag) | generated CRUD diff |
| `_.ExpandedDependency` | **SQL** SES object with a `WHILE`-loop body computing the transitive closure over `_.LoadDependency` | its own **generated load stored procedure** (post-DDL ETL, step 7) |

Why two authoring styles: the parse-derived tables have **no SQL query that
produces their content** — it comes from parsing the SES repo, which only Python
can do — so they are declared empty and filled by CRUD. `_.ExpandedDependency`'s
content **is** derivable by a query over another table, so it is an ordinary
SQL-bodied table with a normal load proc, sitting downstream of `_.LoadDependency`.

These live in the **central metadata Lakehouse (the "Weaver store")** as Delta
tables so the whole workspace can read them via the Lakehouse SQL endpoint. The
Weaver store also holds the **runtime bundles** the Lakehouse loads execute.

### 5b. Per-Warehouse operational tables (high-churn, physical, local)

Live **inside each Warehouse** as real T-SQL tables, never replicated. Read
cross-database live (the same way any business table in that Warehouse is
readable) — no sync, no staleness window:

- `_.LoadStack` — queue of objects needing load, with `Is started` / `Is ended`.
- `_.LoadCandidate` — a **view** over `_.LoadStack` ⋈ `_.ExpandedDependency`
  returning one execution-ready object (all upstream ended, not yet started).
- `_.RefreshBookmark` — per-table extraction bookmark (must be `SELECT`-able
  inline from a load proc's `WHERE`).
- `_.LoadStatistic` — per-load change counts (inserted/updated/deleted/rejected).
- `_.Log` — load success/failure + timestamps.

Because `_.LoadCandidate` needs `_.ExpandedDependency` locally, the Warehouse's
own build materialises `_.ExpandedDependency` **into the Warehouse** as a normal
table with a normal load proc (§3c step 7). This is what lets a Warehouse be
built and loaded **standalone** (dev-debug, no notebook).

---

## 6. Implementation stages (ordered)

Each stage is independently testable. Land them in order; earlier stages are
load-bearing for later ones. **Before Stage 3, confirm the one open decision in
§8.**

### Stage 0 — Baseline

- `cd weaver && ../.venv/bin/python -m pytest` (core green).
- Capture `weaver --help`, `weaver build --help`.
- **Run the existing Fabric integration test** `tests/fabric/test_sql_target.py`
  (needs `WEAVER_FABRIC_WORKSPACE` + `az login`). Its final assertion (line ~125)
  claims data "survives an idempotent rebuild + reload." Given the bare
  `create table` in `ddl.py` has no drop/exists guard, confirm whether this
  currently passes or is silently not exercising a true unchanged-rebuild. This
  tells you the real starting point for Stage 5.

### Stage 1 — `Prohibit rebuild` metadata header + mandatory-on-Folder

- Add optional `prohibit_rebuild: bool` to `ObjectMetadata`, parsed via the
  existing `_parse_bool` pattern in `ses/metadata.py`. Language-agnostic (Python
  docstring + SQL `/* */` both go through `parse_object_metadata`).
- Make it **required** when `kind == FOLDER` (raise `MetadataError` if absent),
  mirroring how `Description`/`Lineage` are required.
- **Acceptance:** unit tests in `tests/` — parses on both Python and SQL objects;
  Folder without the header errors; Table with/without it round-trips.

### Stage 2 — Central metadata Lakehouse as a required build parameter

- Add a way to name the **Weaver store** (metadata Lakehouse) in config and thread
  a resolved reference through `plan_build` / `run_build` / `run_generate`. Decide
  representation: most likely a reserved database alias in `databases:` (e.g. a
  `type: Delta` representation flagged as the metadata store) resolved in
  `config/resolution.py`, and/or a top-level `weaver.yml` key. Keep it
  **environment-neutral** (no product/workspace/name defaults land in `src/` — see
  `AGENTS.md`).
- The Weaver store is where §5a tables + runtime bundles live and what the
  workspace-authoritative catalogue reads from.
- **Acceptance:** config parsing tests; a build that references a missing store
  fails clearly; `generate` reflects the store in its output.

### Stage 3 — The metadata dictionary SES objects (weaver-owned source)

- Author the §5a objects as SES source shipped with weaver (empty Python for the
  parse-derived ones, SQL `WHILE`-loop body for `_.ExpandedDependency`), each with
  `Static: true` + `Prohibit rebuild: true`. Decide their home folder and how they
  are injected into a build's object set (they must be built into the Weaver store
  and, for `_.ExpandedDependency`, into each Warehouse).
- Ensure the SQL backend honours `Static` (skip **load-proc generation** for
  static tables — the parse-derived ones get shape DDL only, no ETL proc; today
  `build_sql_target` installs a load proc for every Table unconditionally).
- **Acceptance:** building a bare target creates the `_.` dictionary tables with
  the right shapes and does not generate load procs for the parse-derived ones;
  `_.ExpandedDependency` gets a load proc.

### Stage 4 — Shared "unified generate" algorithm (the core convergence)

This is the biggest structural change and the heart of the philosophy.

- Extract the §3c algorithm (steps 1–8) into a **shared, target-agnostic planner**
  that consumes the `BuildPlan` and produces an ordered, layered **operation list**
  — pure data, no SQL/Python text, no target queried. Reuse `manifest.py`'s
  builders and `runtime_bundle.py`'s two-way merge/diff for step 6.
- Add **dirty-detection** to this shared layer: compare each object's current
  source hash against the catalogue signature; mark dirty; propagate downstream via
  the dependency graph. (`runtime_bundle.py` already does a merge/diff but no
  signature-based dirtying — extend it.)
- Add **unexpected-object pruning** driven by the target's actual inventory
  (reconciled at execution time), replacing `sql/backend._prune`'s reliance on the
  ad-hoc `_weaver.objects` snapshot.
- Two **renderers** consume the operation list:
  - **SQL renderer** → one ordered sequence of T-SQL batches (extends `sql/ddl.py`
    + `sql/etl.py`; adds drop/exists guards, catalogue dirty/CRUD/confirm SQL,
    metadata-table DDL). Executed via `sql/connection.py` (odbc). This **replaces**
    `build_sql_target`'s imperative orchestration with "render one program → hand
    to executor," matching the Lakehouse model.
  - **Python renderer** → extends the existing `lakehouse/programs.py`
    `render_build_program` to include steps 1, 3, 5–8 (currently it only does Delta
    init, step 4-ish). Executed via `execution.execute_program_local` / Livy.
- **Keep `generate` and `build` rendering the identical string** for each engine
  (the Lakehouse invariant today — preserve it for SQL too).
- **Acceptance:** `generate` for a SQL target emits a full ordered SQL program
  (prune → dirty → DDL → metadata DDL → CRUD → post-DDL ETL → confirm) as inspectable
  text with no connection required; `generate` for a Lakehouse target emits the
  extended Python program. Golden-file tests on the rendered strings.

### Stage 5 — SQL Table rebuild correctness (the ILG unblock)

Rides on Stage 4. This is what makes the ILG view→table conversion safe.

- Fix `ddl.py`: Table create must be **drop-then-recreate** (guarded), not bare
  `create table`, so rebuilding an existing/unchanged table doesn't error.
- Wire dirty-detection end-to-end for the SQL backend: unchanged Table → skipped;
  changed Table + all downstream → dropped and rebuilt; unmanaged object in a
  managed schema → dropped.
- Catalogue lives where the SQL program can write it (see §8 decision).
- **Acceptance:** extend `tests/fabric/test_sql_target.py` — (a) unchanged rebuild
  leaves an unrelated table's data intact; (b) changing one table's body
  drops+rebuilds it and its downstream, not unrelated tables; (c) a manually
  created rogue table in a managed schema is dropped next build. **Real
  acceptance:** point weaver at the actual ILG SES repo, convert the target
  non-DWG `View ID:` objects to `Table ID:`, build+load, and measure the nightly
  load.

### Stage 6 — Per-Warehouse operational tables + `_.ExpandedDependency` local

- Build `_.ExpandedDependency` into each Warehouse (normal table + load proc,
  §5b) so `_.LoadCandidate` resolves locally.
- Create `_.LoadStack`, `_.LoadCandidate` (view), `_.RefreshBookmark`,
  `_.LoadStatistic`, `_.Log` as real Warehouse tables.
- **Read the load-orchestration reference (§2e) before this stage.**
- **Acceptance:** the tables/view exist after build; `_.LoadCandidate` returns
  execution-ready objects given a seeded `_.LoadStack`.

### Stage 7 — Load orchestration (Warehouse-local claim loop)

- Implement the LoadStack/LoadCandidate claim loop as a **Warehouse-local load
  stored procedure** (per the load-orchestration reference), seeding the stack
  from the dirty set (a dirty rebuild also needs a reload) and executing installed
  load procs in dependency order.
- Standalone dev-debug path: a developer connects to their Warehouse and calls the
  load SP, no Python, correct dependency order.
- **Concurrency note:** Fabric Warehouse uses **table-level locking + snapshot
  isolation** — two workers updating *different rows* of the same `_.LoadStack`
  still conflict; the loser gets an update-conflict error and rolls back (there is
  no blocking wait). If/when multi-worker claiming is pursued, the claim step must
  **catch the update-conflict and retry the whole claim** (extend the transient
  retry in `sql/backend._with_retry`). Multi-worker is **deprioritised** —
  standalone Warehouse invocation is a dev-debug convenience; production runs
  through the Lakehouse notebook. Do not build multi-worker unless asked.
- **Acceptance:** a Warehouse builds+loads standalone via odbc with correct
  ordering, matching what the notebook path would produce.

### Stage 8 — Notebook workspace orchestration (later)

- The Lakehouse notebook sequences build+load across the Lakehouse and all
  Warehouse targets via the central dependency graph, invoking each Warehouse's
  local load SP as a black box. Bookmarks/logs stay Warehouse-local; the notebook
  reads them live when it needs to.
- This is the production entry point; Warehouse-standalone is the dev-debug entry
  point. Both go through the same generated programs / local SPs.

---

## 7. Platform facts you must not "fix" (verified against Fabric docs)

- **Fabric Warehouse primary keys are `NONCLUSTERED NOT ENFORCED` — mandatory
  syntax, not a weaver choice.** `PRIMARY KEY`/`UNIQUE` are accepted *only* with
  both `NONCLUSTERED` and `NOT ENFORCED`; the engine never enforces them. There is
  **no `CREATE INDEX`** — storage is columnar Parquet/Delta + statistics. The
  not-enforced PK/UK/FK declarations are optimizer hints, and they are the *only*
  indexing mechanism available. So `ddl.py`'s `_render_primary_key_sql` is already
  doing the only thing the platform allows — do not "add enforcement." Uniqueness
  is enforced only by the reject-table check in `etl.py`/`primary_key_body.sql`.
- **Table-level locking + snapshot isolation** (see Stage 7 concurrency note).
- **`MERGE` is GA** on Fabric Warehouse, but the existing ETL deliberately uses
  separate `INSERT`/`UPDATE...FROM`/`DELETE...WHERE NOT EXISTS` (predates MERGE
  GA). For the metadata-table CRUD diff, prefer full delete+reinsert per build
  (metadata-scale row counts; matches the drop-then-rebuild philosophy) unless the
  user asks for true row-level diffing. Confirm before depending on `MERGE`.
- **Lakehouse SQL analytics endpoint lags Delta commits.** Cross-reads of the
  central Weaver store from a Warehouse can be briefly stale after a Lakehouse
  write. This only matters at build time (not per live query) and the user has
  accepted the analogous "bookmark occasionally late" tolerance — but keep it in
  mind wherever a Warehouse build reads the central store.

---

## 8. Open decision — confirm before Stage 3/5

**Where does the catalogue a Warehouse build reads/writes physically live?**

The conversation settled that (a) the central Weaver store Lakehouse holds the
authoritative dictionary as Delta, and (b) each Warehouse must be buildable and
loadable **standalone** with a self-contained generated SQL program that
"dirties the catalogue and repopulates it" (§3c). A Warehouse's odbc SQL **cannot
write** the Lakehouse's Delta tables (the SQL endpoint is read-only). The two
statements reconcile only if the catalogue the **Warehouse build reads/writes for
its own dirty-detection is local to the Warehouse** (a `_.` table inside the
Warehouse), with the central Weaver store holding the workspace-authoritative /
cross-engine-readable copy.

The most likely intended design (confirm with the user):

- **Per-target self-hosted catalogue + dictionary.** Every target (Warehouse or
  Lakehouse) builds and populates its own `_.Catalogue`/dictionaries as part of its
  own generated program — SQL tables in a Warehouse, Delta tables in a Lakehouse.
  Dirty-detection reads the catalogue in the target being built. This is what makes
  the generated program genuinely self-contained and repo-only.
- The **central Weaver store** is then: the runtime-bundle home + the
  workspace-authoritative copy the notebook reads. Confirm the sync direction (does
  a per-Warehouse build also write central copies, is central populated only by the
  notebook/Lakehouse side, or are they independent?).

Do **not** guess this in code. Confirm it, because it decides whether the SQL
program writes catalogue rows to a Warehouse-local `_.Catalogue`, to the central
store (and how), or both.

---

## 9. Guardrails (things not to break)

- **Environment neutrality:** no product/workspace/Lakehouse/notebook/name
  defaults in `src/weaver_runtime`. There is a test that guards this. New
  generic technical defaults go in `fabric/settings.py`.
- **Lazy imports:** keep PySpark out of core (guarded by
  `tests/test_no_pyspark_core.py`); `pyodbc`, `sqlparse`, `azure-identity` stay
  lazy. The rendered SQL/Python programs must not force these into core import.
- **`generate` == `build` program string** per engine — preserve this invariant
  (a test asserts the staged `_orchestrator` bundle is a byte-for-byte copy of the
  live runtime).
- **Load mechanics unchanged:** `sql/etl.py` + `source/sql_templates/etl/` (staging
  → check → upsert/reject → apply → history) are correct — this project is about
  *build* and load *orchestration*, not per-table load mechanics.
- **Views stay free** — never dirty-gate a `View`.
- Run `pytest` (core) after every stage; `-m fabric` for the Warehouse stages
  (needs `WEAVER_FABRIC_WORKSPACE` + `az login`; creates/deletes a disposable
  Warehouse+Lakehouse).

---

## 10. One-paragraph summary for the impatient

Weaver builds SES objects into Fabric Lakehouse/Warehouse. The Lakehouse backend
already **renders one deterministic program from the repo and hands it to the
target to execute**; the SQL backend does not (it orchestrates imperatively and
its Table rebuild is broken — bare `create table` with no guard). Converge the SQL
backend onto the Lakehouse model: one generated, topologically-layered program
(SQL for Warehouse over odbc, Python for Lakehouse over Livy/`exec`) that prunes
unknown objects, dirties the catalogue by source-hash (dropping changed objects +
downstream up front), rebuilds in order, ensures + CRUD-populates the weaver-owned
`_.` metadata dictionary tables, runs post-DDL ETL (e.g. `_.ExpandedDependency`),
and finally re-inserts catalogue rows as the commit marker. Add `Prohibit rebuild`
so the metadata tables survive their own edits, add the central metadata Lakehouse
as a required parameter, then per-Warehouse operational tables + a local
load-orchestration SP. The point of it all: let ILG's stacked SES views become
materialised tables so the nightly load stops re-expanding view-on-view.
```
