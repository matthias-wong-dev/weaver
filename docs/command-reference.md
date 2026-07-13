# Command reference

Reference for every public `weaver` command. For a guided first run, see
[Getting started](getting-started.md); for what these commands do internally, see
[Build and load](build-and-load.md).

```
weaver {generate,build,load,wipe,fabric} …
```

The examples use the [`examples/simple-ses`](../examples/simple-ses) aliases.

---

## Lifecycle commands

Every lifecycle command takes `--config <path>`, the `dbrep-weaver.yml` that names
the database representations. `--from` / `--to` / `--target` take representation
**aliases** from that file. `--from` and `--to` accept comma-separated lists and
are matched **position by position**.

### `weaver generate`

Render deployment and runtime artifacts **without applying them**.

```
weaver generate --config <path> --from <alias,…> --to <alias,…> [--out DIR] [--prune] [--strict]
```

| Argument | Meaning |
|---|---|
| `--config` *(required)* | path to `dbrep-weaver.yml` |
| `--from` *(required)* | source representation alias(es) |
| `--to` *(required)* | target representation alias(es), matched to `--from` by position |
| `--out DIR` | output directory (defaults to `.weaver/generate` beside the config) |
| `--prune` | include prune decisions in the generated plan |
| `--strict` | fail on conditions a non-strict run would tolerate |

SQL targets emit executable DDL plus a `plan.json`; Lakehouse targets stage the
full runtime bundle; Fabric targets are staged locally and never uploaded.

```bash
weaver generate --config dbrep-weaver.yml --from Mart_SES --to Mart_SQL --out ./artifacts
```

### `weaver build`

Discover, order, and install a runtime, then materialise the targets.

```
weaver build --config <path> --from <alias,…> --to <alias,…>
             [--prune] [--dry-run] [--strict] [--assume-installed-runtime]
```

| Argument | Meaning |
|---|---|
| `--config` *(required)* | path to `dbrep-weaver.yml` |
| `--from` *(required)* | source representation alias(es) |
| `--to` *(required)* | target representation alias(es), matched to `--from` by position |
| `--prune` | drop managed objects removed from the SES since the last build |
| `--dry-run` | print the plan and load order; create nothing |
| `--strict` | fail on conditions a non-strict run would tolerate |
| `--assume-installed-runtime` | skip re-installing the runtime bundle; materialise only |

```bash
# Build the Raw (Files) and Core (Delta) representations from their SES sources.
weaver build --config dbrep-weaver.yml --from Raw_SES,Core_SES --to Raw_Files,Core_Delta

# See the plan without building.
weaver build --config dbrep-weaver.yml --from Raw_SES,Core_SES --to Raw_Files,Core_Delta --dry-run
```

### `weaver load`

Run the installed runtime against one target. Reads the installed bundle, never
the SES.

```
weaver load --config <path> --target <alias>
            [--object ID …] [--include-static] [--dry-run] [--strict | --no-strict]
```

| Argument | Meaning |
|---|---|
| `--config` *(required)* | path to `dbrep-weaver.yml` |
| `--target` *(required)* | the representation alias to load |
| `--object ID` | load only this object; repeatable |
| `--include-static` | also load objects marked `Static` in their header |
| `--dry-run` | validate the bundle and print the ordered steps; execute nothing |
| `--strict` / `--no-strict` | fail (default) or tolerate installed objects absent from the catalogue |

```bash
weaver load --config dbrep-weaver.yml --target Raw_Files
weaver load --config dbrep-weaver.yml --target Core_Delta

# Load a single object.
weaver load --config dbrep-weaver.yml --target Core_Delta --object Core.Sales.Order
```

### `weaver wipe`

Remove a target's materialised data or managed objects.

```
weaver wipe --config <path> --target <alias>
```

| Argument | Meaning |
|---|---|
| `--config` *(required)* | path to `dbrep-weaver.yml` |
| `--target` *(required)* | the representation alias to wipe |

Files → the database's `Files/<database>` directory. Delta → the database's
tables. SQL → the target's managed objects. Only Weaver-managed materialisations
are removed.

```bash
weaver wipe --config dbrep-weaver.yml --target Core_Delta
```

---

## Fabric commands

Operational commands for Microsoft Fabric. These act on a Fabric workspace and
need Azure credentials (typically `az login`). Technical defaults (API URL,
scopes, timeouts) resolve as CLI override → environment variable → built-in
default.

### `weaver fabric capacity`

Control a Fabric capacity.

```
weaver fabric capacity {status,resume,suspend}
    --resource-group <name> --capacity-name <name> [--subscription-id <id>]
```

| Argument | Meaning |
|---|---|
| `--resource-group` *(required)* | the Azure resource group |
| `--capacity-name` *(required)* | the Fabric capacity name |
| `--subscription-id` | Azure subscription (defaults to `FABRIC_SUBSCRIPTION_ID`) |

```bash
weaver fabric capacity status  --resource-group my-rg --capacity-name my-cap
weaver fabric capacity resume  --resource-group my-rg --capacity-name my-cap
weaver fabric capacity suspend --resource-group my-rg --capacity-name my-cap
```

### `weaver fabric workspace push`

Deploy workspace items (such as notebooks) from a source directory.

```
weaver fabric workspace push --source DIR
    (--workspace-name <name> | --workspace-id <id>)
    [--item <name>] [--description <text>] [--prune] [--update-metadata] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--source` *(required)* | directory of workspace item sources |
| `--workspace-name` / `--workspace-id` | the target workspace (one is required) |
| `--item` | push only this item |
| `--description` | description to set on pushed items |
| `--prune` | remove workspace items absent from the source |
| `--update-metadata` | update item metadata as well as content |
| `--dry-run` | show what would change; push nothing |

```bash
weaver fabric workspace push --source ./notebooks --workspace-name "My Workspace"
```

### `weaver fabric notebook run`

Run a notebook in a workspace and (by default) wait for it to finish.

```
weaver fabric notebook run
    (--name <name> | --notebook-id <id>)
    (--workspace-name <name> | --workspace-id <id>)
    [--parameter K=V …] [--no-wait] [--poll-interval SECONDS] [--timeout SECONDS]
```

| Argument | Meaning |
|---|---|
| `--name` / `--notebook-id` | the notebook to run (one is required) |
| `--workspace-name` / `--workspace-id` | the workspace (one is required) |
| `--parameter K=V` | a notebook parameter; repeatable |
| `--no-wait` | submit and return without waiting |
| `--poll-interval` | seconds between status polls while waiting |
| `--timeout` | seconds to wait before giving up |

```bash
weaver fabric notebook run --workspace-name "My Workspace" --name "Load Weaver" \
  --parameter LOAD_TARGETS=Raw_Files_Fabric,Core_Delta_Fabric
```
