# Weaver Agent Guide

Guidance for coding agents working **on Weaver itself**. For how to *use* Weaver,
read the user documentation (below) — do not duplicate it here.

## Repository role

`weaver` is a generic data-engineering runtime and operational toolkit for
Microsoft Fabric. It owns:

- The `generate` / `build` / `load` / `wipe` database-representation lifecycle.
- SES discovery, static dependency analysis, and dependency-ordered execution.
- Reconciliation, CRUD accounting, and durable workflow logging.
- The SQL backend (self-inferring backing table + view + load procedure).
- The Fabric substrate: authentication, capacity control, OneLake transfer,
  workspace item deployment, notebook execution, Spark-over-Livy, and SQL.
- Configuration loading and CLI routing.

Weaver must stay **environment-neutral**. Never add defaults for product,
workspace, Lakehouse, endpoint, repository, or notebook names, production
endpoints, or local platform paths. Allowed defaults are generic technical values
(Fabric API URLs, auth scopes, Livy version, timeouts, polling intervals,
parallelism) and live in `fabric/settings.py`, resolving CLI override →
environment variable → technical default.

## Repository map

```
weaver/
├── README.md                    project landing page
├── AGENTS.md                    this guide
├── docs/                        user documentation (authoritative — see below)
├── examples/simple-ses/         the canonical runnable example
├── src/weaver_runtime/
│   ├── cli.py                   top-level argparse entry point (weaver …)
│   ├── fabric/                  shared Fabric substrate (auth, client, onelake, livy, sql, settings)
│   └── dbrep/                   the database-representation build/load system
│       ├── cli/                 generate/build/load/wipe subcommands
│       ├── config/              environment.py, databases.py, resolution.py
│       ├── ses/                 discovery, metadata, dependencies, graph
│       ├── build/               planner, manifest, runtime bundle, prune
│       ├── runtime/             orchestrator, load, folders, load_policy, logging, workflow_logging
│       ├── sql/                 SQL backend (DDL, ETL, templates)
│       ├── targets/             Files/Delta/SQL/Fabric target adapters
│       ├── fabric/              DBRep OneLake transfer + Fabric Lakehouse build/load
│       └── objects.py           Folder/Table/View authoring base classes
├── scripts/                     legacy operational wrappers (prefer src/weaver_runtime)
└── tests/                       core (default), spark/, fabric/
```

## Authoritative documents

The user documentation is the source of truth for behaviour. When you change
behaviour, update the matching doc in the same change.

| Document | Owns |
|---|---|
| [README.md](README.md) | what Weaver is, the philosophy, install, first run |
| [docs/concepts.md](docs/concepts.md) | the mental model and naming stack |
| [docs/getting-started.md](docs/getting-started.md) | the first-run walkthrough |
| [docs/configuration.md](docs/configuration.md) | `dbrep-env.yml` / `dbrep-weaver.yml` |
| [docs/authoring.md](docs/authoring.md) | the object header + `read()` contract |
| [docs/build-and-load.md](docs/build-and-load.md) | the runtime lifecycle |
| [docs/command-reference.md](docs/command-reference.md) | every public CLI command |
| [examples/simple-ses/](examples/simple-ses) | the example referenced throughout the docs |

`docs/fabric_capacity_control.md` and `docs/sql-tables-and-central-metadata-plan.md`
are focused design notes, not part of the user-facing set above.

## Command surface

Install editable and run the CLI:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .            # add [spark] for local Delta builds
.venv/bin/weaver --help
```

The lifecycle is one `env.yml` (hosts only) referenced by a `weaver.yml`
(`databases:` with per-representation `type`):

```bash
.venv/bin/weaver generate --config weaver.yml --from T0_SES,T1_SES --to T0_FILES,T1_DELTA [--out DIR]
.venv/bin/weaver build    --config weaver.yml --from T0_SES,T1_SES --to T0_FILES,T1_DELTA [--prune] [--dry-run]
.venv/bin/weaver load     --config weaver.yml --target T1_DELTA
.venv/bin/weaver wipe     --config weaver.yml --target T1_DELTA
```

Fabric operational commands:

```bash
.venv/bin/weaver fabric capacity status  --resource-group <rg> --capacity-name <cap>
.venv/bin/weaver fabric workspace push    --source <dir> --workspace-name <workspace>
.venv/bin/weaver fabric notebook run      --workspace-name <workspace> --name "<notebook>"
```

In the product monorepo, Weaver is installed from the platform root
(`pip install -e ./weaver`) and invoked with product config; keep paths and names
in the product repo, never in Weaver.

## Architecture invariants

These are not in the user docs and must hold:

- **Static dependency parsing.** Discovery never imports object modules.
  `self.repo["…"]` (Python) and `from` / `join` (SQL) references are read from
  source. Two-part refs are intra-database; three-part are managed cross-database
  only when the database is supplied, else external; four-part are external.
- **Discovery ignores every `_`-prefixed folder and file.**
- **PySpark stays out of the core.** It is a lazy import confined to
  `runtime/load.py`. `runtime/folders.py`, `runtime/load_policy.py`, and
  `runtime/workflow_logging.py` are pure stdlib. `sqlparse`, `pyodbc`, and
  `azure-identity` are likewise lazy so the core and the installed Fabric runtime
  import them only when actually needed.
- **One deletion authority.** A table without a primary key cannot delete rows;
  `Incremental: false` and explicit delete tuples cannot be combined. Enforced in
  `runtime/load_policy.py` before any write.
- **Objects never mutate the target.** `read()` returns `(upserts, deletes)`;
  Weaver owns mutation, CRUD counting, staging, and logging. Folders stage into a
  Weaver-issued `<Folder>_Staging` sibling, retained on failure.
- **The same generated program runs locally and on Fabric.** Logging and
  reconciliation are not transport-specific. Fabric-facing object code joins paths
  with f-strings, not `pathlib.Path` (which mangles `abfss://`).
- **SQL build executes DDL layer by layer** (no retry loops), parallel within a
  layer up to the server's `degrees_of_parallelism`, recording managed objects in
  `_weaver.objects` so `--prune` drops only removed managed objects.

## Tests

```bash
.venv/bin/python -m pytest                 # core only (default)
.venv/bin/python -m pytest -m spark        # optional local Spark/Delta (needs Java 17)
WEAVER_FABRIC_WORKSPACE=<ws> .venv/bin/python -m pytest -m fabric   # opt-in Fabric integration
```

Default `pytest` runs core only (`addopts = -m "not spark and not fabric"`). Core
tests never import PySpark (guarded by `tests/test_no_pyspark_core.py`), and a
guard asserts `src/weaver_runtime` contains no product or environment defaults.
Fabric tests create and delete a disposable Warehouse and Lakehouse in the given
workspace and skip unless it is set.

## Conventions

### Documentation

- Treat the user docs as behaviour's source of truth. A behaviour change that
  leaves a doc stale is incomplete.
- Keep [`examples/simple-ses`](examples/simple-ses) runnable and in step with the
  docs — it is the canonical reference the whole documentation set points to.
  Verify it with `weaver build`/`load` (Files needs no Spark; Delta needs Java 17).
- Prefer linking to a doc over restating it.

### SQL style

- Lower-case SQL keywords.
- Put a single join predicate on the same line as the joined table; start new
  lines only for additional `and` / `or` predicates, and wrap `or` groups in
  parentheses.
- Leading commas for column lists; align names, aliases, and lists where it aids
  scanability.
