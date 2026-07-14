# Configuration

Weaver reads two YAML files, and keeps them deliberately separate:

| File | Answers | Declares |
|---|---|---|
| `dbrep-env.yml` | *where do things live?* | **hosts** (servers) |
| `dbrep-weaver.yml` | *what do I build?* | **database representations** |

Because *where* lives apart from *what*, moving a pipeline from your laptop to
Microsoft Fabric is a change of hosts only — the database representations and the
SES objects do not change. You pick an environment by pointing `--config` at a
different file; there is no prod/dev/test switch inside a file.

The names `dbrep-env.yml` / `dbrep-weaver.yml` are conventional. Any path works —
`--config` names the weaver file, and the weaver file names its environment.

---

## The environment file (`dbrep-env.yml`)

An environment declares **hosts**. A host is a physical place: an SES folder, a
Lakehouse, or a SQL endpoint.

```yaml
version: 1

servers:
  Repo:
    type: SES
    server: SES

  Local_Lakehouse:
    type: Local Lakehouse
    server: .lakehouse
```

- `version` must be `1`.
- `servers` is a non-empty mapping of **alias → host**. The alias (e.g. `Repo`)
  is how database representations refer to the host.

### Host types

| `type` | `server` means | Extra keys |
|---|---|---|
| `SES` | a filesystem path to the SES root | — |
| `Local Lakehouse` | a filesystem path to a Lakehouse folder | — |
| `Fabric Lakehouse` | `Workspace/Lakehouse` | `environment` |
| `SQL` | a Fabric Warehouse / SQL endpoint hostname | `degrees_of_parallelism` |

Common keys on any host:

- **`type`** *(required)* — one of the four above.
- **`server`** *(required)* — a non-empty string; its meaning depends on `type`.
- **`degrees_of_parallelism`** *(optional)* — a positive integer; how many
  objects the runtime may process in parallel within a dependency layer.
- **`environment`** *(Fabric Lakehouse only)* — the Fabric environment (with
  `weaver-runtime` installed) that runs the load.

Keys that do not belong to a host's type are rejected, so a typo fails fast
rather than being silently ignored.

### Path resolution

Filesystem `server` values (`SES`, `Local Lakehouse`) are resolved **relative to
the environment file**, unless absolute. In the example, `server: SES` beside
`dbrep-env.yml` resolves to `.../simple-ses/SES`, and `.lakehouse` to
`.../simple-ses/.lakehouse`.

A `Fabric Lakehouse` server is `Workspace/Lakehouse` (both parts non-empty). A
`SQL` server is the endpoint hostname.

---

## The weaver file (`dbrep-weaver.yml`)

The weaver file names **database representations** and binds each to a host.

```yaml
version: 1

uses:
  environment: dbrep-env.yml

databases:
  Raw_SES:
    type: SES
    server: Repo
    database: Raw

  Raw_Files:
    type: Files
    server: Local_Lakehouse
    database: Raw
```

- `version` must be `1`.
- `uses.environment` is a path to the environment file, resolved relative to this
  file. (You may instead inline the environment under an `environment:` key, but a
  referenced file is the norm.)
- `databases` is a non-empty mapping of **alias → representation**.

### Representation keys

- **`type`** *(required)* — `SES`, `Files`, `Delta`, or `SQL`. This is the type of
  the *representation*, not the host.
- **`server`** *(required)* — a host alias from the environment file.
- **`database`** *(required)* — the third-level database name. This is the folder
  name under an SES root, and the name that groups materialisations under a
  Lakehouse or Warehouse.
- **`environment`** *(optional)* — overrides the host's Fabric environment; valid
  only for `Files` or `Delta` on a `Fabric Lakehouse` host.

### Type compatibility

A representation type is only valid on matching host types:

| Representation `type` | Valid host types |
|---|---|
| `SES` | `SES` |
| `Files` | `Local Lakehouse`, `Fabric Lakehouse` |
| `Delta` | `Local Lakehouse`, `Fabric Lakehouse` |
| `SQL` | `SQL` |

A mismatch (say, a `Delta` representation on a `SQL` host) is rejected when the
config loads.

### One database, several representations

The same `database` name appears under several aliases — that is the point.
`Raw_SES` and `Raw_Files` are both the `Raw` database: the SES you author and the
Files it builds into. The alias you pass on the command line selects which
representation you mean:

```bash
weaver build --from Raw_SES --to Raw_Files   # author → files
weaver load  --target Raw_Files              # load the files
```

### Naming conventions

Aliases are yours to choose, but a consistent scheme keeps commands readable. The
examples use `<Database>_<Representation>`:

```
Raw_SES     Core_SES     Mart_SES        ← SES sources
Raw_Files   Core_Delta   Mart_SQL        ← destinations
```

---

## Complete example: local

The `simple-ses` configuration, in full. This is everything needed to build and
load on a laptop.

`dbrep-env.yml`:

```yaml
version: 1

servers:
  Repo:            { type: SES,             server: SES }
  Local_Lakehouse: { type: Local Lakehouse, server: .lakehouse }
```

`dbrep-weaver.yml`:

```yaml
version: 1

uses:
  environment: dbrep-env.yml

databases:
  Raw_SES:    { type: SES,   server: Repo,            database: Raw }
  Core_SES:   { type: SES,   server: Repo,            database: Core }
  Mart_SES:   { type: SES,   server: Repo,            database: Mart }

  Raw_Files:  { type: Files, server: Local_Lakehouse, database: Raw }
  Core_Delta: { type: Delta, server: Local_Lakehouse, database: Core }
```

---

## Complete example: Microsoft Fabric

The **same weaver file** with Fabric hosts added. Only the environment gains
hosts; the SES objects are untouched. Here `Raw` and `Core` build to a Fabric
Lakehouse and `Mart` to a Fabric Warehouse.

`dbrep-env.yml`:

```yaml
version: 1

servers:
  Repo:
    type: SES
    server: SES

  Fabric_Lakehouse:
    type: Fabric Lakehouse
    server: My Workspace/Core        # Workspace / Lakehouse
    environment: simple-ses          # Fabric environment with weaver-runtime
    degrees_of_parallelism: 32

  Warehouse:
    type: SQL
    server: <sql-endpoint>.datawarehouse.fabric.microsoft.com
    degrees_of_parallelism: 8
```

`dbrep-weaver.yml`:

```yaml
version: 1

uses:
  environment: dbrep-env.yml

databases:
  Raw_SES:  { type: SES, server: Repo, database: Raw }
  Core_SES: { type: SES, server: Repo, database: Core }
  Mart_SES: { type: SES, server: Repo, database: Mart }

  Raw_Files_Fabric:  { type: Files, server: Fabric_Lakehouse, database: Raw }
  Core_Delta_Fabric: { type: Delta, server: Fabric_Lakehouse, database: Core }
  Mart_SQL:          { type: SQL,   server: Warehouse,        database: Mart }
```

On a **Fabric Lakehouse** the Lakehouse *is* the database host, so Delta tables
materialise at `Tables/<schema>/<object>` (no database component). On a **local**
Lakehouse, one folder can host several databases, so tables materialise at
`Tables/<database>/<schema>/<object>`. Both are handled for you; the difference
matters only if you inspect the files directly.

---

## See also

- [Concepts](concepts.md) — the `server.database.type.schema.object` model.
- [Command reference](command-reference.md) — how `--from`, `--to`, and
  `--target` consume these aliases.
- [Build and load](build-and-load.md) — what building to each host type does.
