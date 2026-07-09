# Weaver Agent Guide

## Repository Role

`weaver` owns generic operational tooling for Microsoft Fabric and related
runtime surfaces:

- Fabric authentication and API helpers.
- Fabric capacity control.
- Lakehouse repository sync.
- Fabric workspace item deployment.
- Fabric notebook execution.
- Fabric Spark execution.
- SQL execution.
- SES package build.
- Configuration loading and CLI routing.

Weaver must stay environment-neutral. Do not add defaults for product names,
workspace names, Lakehouse names, SQL endpoint names, repository names,
notebook names, production endpoints, or local platform paths.

Allowed defaults are generic technical values, such as Fabric API URLs,
authentication scopes, timeouts, polling intervals, and worker counts.

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
.venv/bin/weaver fabric lakehouse sync --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric workspace push --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric platform push --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric build ses --config ilovegov-dwg/etl/weaver.yaml
.venv/bin/weaver fabric notebook run --config ilovegov-dwg/etl/weaver.yaml --name "Notebook name"
.venv/bin/weaver fabric spark run --config ilovegov-dwg/etl/weaver.yaml --file path/to/code.py
.venv/bin/weaver fabric sql run --config ilovegov-dwg/etl/weaver.yaml --file path/to/query.sql
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
../.venv/bin/weaver build  --config weaver.yml --from T0_SES,T1_SES --to T0_LOCAL_FILES,T1_LOCAL_DELTA [--prune] [--dry-run]
../.venv/bin/weaver load   --config weaver.yml --target T1_LOCAL_DELTA
../.venv/bin/weaver plan    --config weaver.yml --from ... --to ...
../.venv/bin/weaver discover --config weaver.yml --database T1_SES
../.venv/bin/weaver manifest --config weaver.yml --target T1_LOCAL_DELTA
```

Rules that must hold: config uses one `env.yml` (hosts only) referenced by
`weaver.yml` (`databases:` with per-representation `type`); dependency parsing is
static (`self.repo["..."]` / SQL `from`/`join`); two-part refs are
intra-database, three-part are managed cross-database only when supplied, else
external; discovery ignores every `_`-prefixed folder/file. Keep PySpark out of
core — it is a lazy import confined to `runtime/load.py` and `runtime/load_policy`
is a pure, PySpark-free reference for load behaviour. `sqlparse`, `pyodbc`, and
`azure-identity` are also lazy so the core (and the installed Fabric runtime)
import them only when SQL discovery / SQL connections actually run.

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

Set `platform: fabric` on a server whose `server` is `Workspace/Lakehouse`.
Build stages the bundle locally then uploads `Files/` to OneLake (reusing
`scripts/sync_folder`). Load submits the bundled orchestrator to Fabric Spark via
Livy (`scripts/sparksession`): it mounts the Lakehouse for orchestrator import +
Folder Python IO, and passes the `abfss://` OneLake path as `spark_root` for all
Delta reads/writes (the FUSE mount cannot host Spark Delta writes). Fabric-facing
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
