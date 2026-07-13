# Weaver Agent Guide

## Repository Role

`weaver` owns generic operational tooling for Microsoft Fabric and related
runtime surfaces:

- Fabric authentication and API helpers.
- Fabric capacity control.
- General OneLake folder sync + platform mirror push (no Git required).
- Fabric workspace item deployment.
- Fabric notebook execution.
- Fabric Spark execution via Livy.
- SQL execution against the Fabric Warehouse / SQL endpoint.
- The `generate`/`build`/`load`/`wipe` database-representation lifecycle.
- Configuration loading and CLI routing.

The shared Fabric substrate lives under `src/weaver_runtime/fabric/`
(`auth`, `client`, `resources`, `onelake`, `ignore`, `sync`, `livy`, `sql`,
`settings`, `context`). New Fabric behaviour belongs there, not in `scripts/`.

Weaver must stay environment-neutral. Do not add defaults for product names,
workspace names, Lakehouse names, SQL endpoint names, repository names,
notebook names, production endpoints, or local platform paths.

Allowed defaults are generic technical values, such as Fabric API URLs,
authentication scopes, Livy API version, timeouts, polling intervals, and
degrees of parallelism. These live in `fabric/settings.py` and resolve
CLI override -> environment config -> technical default.

## Command Surface

The supported command is installed from the platform root:

```bash
cd /Users/matthiaswong/dev/dwg-platform
.venv/bin/pip install -e ./weaver
.venv/bin/weaver --help
```

Normal product use passes configuration from the product repo:

```bash
.venv/bin/weaver fabric capacity status --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric capacity resume --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric capacity suspend --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric onelake sync --config ilovegov-dwg/etl/weaver.yaml --source <folder> --target-folder <Files-relative folder>
.venv/bin/weaver fabric platform push --config ilovegov-dwg/etl/weaver.yaml [--name weaver] [--dry-run]
.venv/bin/weaver fabric workspace push --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric notebook run --config ilovegov-dwg/etl/weaver.yaml --name "Notebook name"
.venv/bin/weaver fabric livy submit --config ilovegov-dwg/etl/weaver.yaml --kind pyspark --file path/to/code.py
.venv/bin/weaver fabric sql execute --config ilovegov-dwg/etl/weaver.yaml --file path/to/query.sql
```

SES create/wipe now runs through the lifecycle, not a dedicated command:

```bash
.venv/bin/weaver build --config weaver.yml --from <SES> --to <SQL>
.venv/bin/weaver wipe  --config weaver.yml --target <SQL_or_Lakehouse_target>
```

Legacy scripts under `scripts/` may exist as compatibility wrappers, but new
substantive behavior should live under `src/weaver_runtime`.

## Database representation build/load

`src/weaver_runtime/dbrep` is the generic database-representation build/load
system. Weaver builds between named database representations (typed
`SES`/`Files`/`Delta`/`SQL` third-level names on hosts declared in environment
config), installs a runtime bundle + manifest + load plan under the Lakehouse
host, and loads target-only from the installed runtime.

```bash
../.venv/bin/weaver generate --config weaver.yml --from T0_SES,T1_SES --to T0_LOCAL_FILES,T1_LOCAL_DELTA [--out DIR]
../.venv/bin/weaver build    --config weaver.yml --from T0_SES,T1_SES --to T0_LOCAL_FILES,T1_LOCAL_DELTA [--prune] [--dry-run]
../.venv/bin/weaver load     --config weaver.yml --target T1_LOCAL_DELTA
../.venv/bin/weaver wipe      --config weaver.yml --target T1_LOCAL_DELTA
```

`generate` produces concrete deployment/runtime artifacts without applying them
(SQL targets emit source objects + a `plan.json`; Lakehouse/Fabric targets stage
the runtime bundle under `--out` and never upload). The former public
`plan`/`discover`/`manifest` subcommands have been removed.

Rules that must hold: config uses one `env.yml` (hosts only) referenced by
`weaver.yml` (`databases:` with per-representation `type`); dependency parsing is
static (`self.repo["..."]` / SQL `from`/`join`); two-part refs are
intra-database, three-part are managed cross-database only when supplied, else
external; discovery ignores every `_`-prefixed folder/file. Keep PySpark out of
core — it is a lazy import confined to `runtime/load.py` and `runtime/load_policy`
is a pure, PySpark-free reference for load behaviour. `sqlparse`, `pyodbc`, and
`azure-identity` are also lazy so the core (and the installed Fabric runtime)
import them only when SQL discovery / SQL connections actually run.

### Object authoring and runtime logging (`dbrep/runtime`)

Both kinds implement `read()` returning one `(upserts, deletes)` pair (never
`load()`): `Folder.read()` → `(staging_folder, file_names_to_delete)` staged in a Weaver-issued object-local `<Folder>_Staging` sibling (retained on failure and cleared on retry; object code
never writes the destination directly); `Table.read(spark)` →
`(staging_dataframe, primary_key_values_to_delete)`. Weaver owns all
mutation, CRUD counting, and logging. Deletion has one authority: a table without
a primary key cannot delete rows, and `Incremental: false` and explicit delete
tuples cannot be combined (enforced in `runtime/load_policy.py` before any
write). `runtime/folders.py` is the pure staging contract + reconciliation (file
CRUD by size/content diff over only the staged files and explicit deletes, never
a full destination rescan); `runtime/logging.py` holds the shared pair validator;
`runtime/workflow_logging.py` mints one `{timestamp}_{uuid}` workflow
per `load`, writes one `{timestamp}_{uuid}.json` step record per object under
`Files/_logs/<workflow_id>` the moment it finishes (success or failure, with the
full structured exception), and keeps object/module names inside the JSON. Every
step carries a common `CrudCounts` (`unit: files`/`rows`); kind specifics live in
`details`. `folders.py`,
`load_policy.py`, and `workflow_logging.py` are pure stdlib — no PySpark. See
`docs/authoring.md` for the authoring reference.

### SQL backend (`dbrep/sql`)

Real SQL builds run against a Fabric Warehouse. DDL/load are ported from the
legacy `source` machinery (self-inferring backing table + view, ETL load
procedure). Build executes DDL **layer by layer** using the dependency graph
(no retry loops), parallel within a layer up to the server's
`degrees_of_parallelism`; it records managed objects in a `_weaver.objects` table
inside the warehouse so `--prune` drops only removed managed objects. `weaver
load --target <SQL>` executes installed load procedures in dependency order.
Source objects for a SQL target must be `.sql`.

### Fabric Lakehouse backend (`dbrep/fabric`)

Declare a `Fabric Lakehouse` server with `server: Workspace/Lakehouse` and an
optional Fabric `environment` name.
Build stages the bundle locally then syncs `Files/` to OneLake through the shared
movement layer (`weaver_runtime.fabric.sync`): `Files/_weaver/runtime` syncs with
signature diff + scoped delete (Weaver-owned), object folders sync without
delete. Load submits the bundled orchestrator to Fabric Spark via Livy
(`weaver_runtime.fabric.livy`): it mounts the Lakehouse for orchestrator import +
Folder staging/reconciliation IO and durable `Files/_logs` step logging, and
passes the `abfss://` OneLake path as `spark_root` for all Delta reads/writes (the
FUSE mount cannot host Spark Delta writes). The same generated runtime program
runs locally and on Fabric — logging is not transport-specific. Fabric-facing
object files must join paths with f-strings, not `pathlib.Path` (which mangles
`abfss://`).

## Tests

Run from this repo:

```bash
cd /Users/matthiaswong/dev/dwg-platform/weaver
../.venv/bin/python -m pytest
```

Core tests never import PySpark (guarded by `tests/test_no_pyspark_core.py`).
Optional local Spark/Delta tests live under `tests/spark/` and are deselected by
default; run them with Homebrew Java 17 (see `dwg-site-kit/AGENTS.md` for the
`JAVA_HOME` incantation):

```bash
../.venv/bin/python -m pytest -m spark
```

Opt-in Fabric integration tests live under `tests/fabric/` (deselected by
default; they need `az login`). They create a disposable Warehouse and Lakehouse
in the given workspace via the Fabric REST API and delete them afterwards, so
they only need the workspace and skip unless it is set:

```bash
WEAVER_FABRIC_WORKSPACE=<workspace name or id> \
../.venv/bin/python -m pytest -m fabric
```

Default `pytest` runs core only (`addopts = -m "not spark and not fabric"`).
Tests include a guard that `src/weaver_runtime` does not contain product or
environment defaults.

## SQL style

- Use lower-case SQL keywords.
- Put join predicates on the same line as the joined table when there is one predicate.
- Start new lines only for additional `and` / `or` predicates.
- Wrap `or` predicate groups in parentheses.
- Align table names, aliases, and column lists where it improves scanability.
- Use leading commas for column lists, not trailing commas.
