# Authoring Weaver objects

A Weaver object declares what it produces; Weaver owns how the change is applied,
counted, and logged. Objects never mutate the target directly.

Both object kinds share one endpoint shape: `read()` returns a two-item tuple of
**proposed upserts and explicit deletes**.

```text
Folder.read()  -> staging_folder,    file_names_to_delete
Table.read()   -> staging_dataframe, primary_key_values_to_delete

Weaver
    validates      the pair
    reconciles     upserts and deletes against the target
    mutates        the target
    calculates     standard CRUD
    writes         durable workflow logs
    captures       full errors
    cleans         staging resources
```

## The two authoring shapes

```python
from weaver_runtime.dbrep.objects import Folder, Table


class Source__Archive(Folder):
    def read(self):
        with self.staging_folder() as staging_folder:
            download_and_prepare_files(staging_folder.path)

        file_names_to_delete = ("unwanted.json",)
        return staging_folder, file_names_to_delete


class Sales__CustomerOrder(Table):
    def read(self, spark):
        staging_dataframe = build_customer_orders(spark)

        primary_key_values_to_delete = (("order-17",), ("order-29",))
        return staging_dataframe, primary_key_values_to_delete
```

The normal no-delete case for either kind is `return upserts, ()`.

## Workflow logging

Each `weaver load` invocation is one **workflow**. Weaver mints a
`{timestamp}_{uuid}` workflow id, creates `Files/_logs/<workflow_id>/`, and
writes one `{timestamp}_{uuid}.json` step record per executed object — the moment
the step finishes, success or failure. Object and module names live inside the
JSON, never in the filename. A failed step records the full structured exception
(type, repr, message, args, traceback, cause/context, and Spark error class / SQL
state / Java exception text where available) and is written before the error
leaves the runtime, so earlier successful steps are always preserved.

Every step carries a standard CRUD block:

```text
Folder -> unit: files    read / created / updated / deleted
Table  -> unit: rows     read / created / updated / deleted
```

## The Folder pair

1. a **`StagingFolder`** whose leaf files are created or updated in the target;
2. a sequence of **relative file names to delete**;

Inside `staging_folder.path` use ordinary Python — `pathlib`, `shutil`,
`requests`, `zipfile`, `pandas`, plain file writes. There are no special Weaver
file-write methods. The pair may be returned **inside or after** the `with`
block; both behave identically:

```python
with self.staging_folder() as staging_folder:
    ...
return staging_folder, ()
```

Every Folder metadata block must explicitly declare its managed file population
and deletion mode:

```yaml
File key:
  - "**/*.pdf"
  - "**/*.json"
Auto delete: false
```

A single glob string is also valid. File keys match relative POSIX paths, and
each matching path is one managed file identity. Weaver fails the load before
target mutation if any staged leaf file does not match at least one File key.

Weaver reconciles managed staged files against the target and counts file CRUD
(`created`/`updated`/`read`-only by size then content; `deleted` for files that
existed). With `Auto delete: false`, missing managed target files are retained
and explicit deletes are allowed only when they match a File key. With
`Auto delete: true`, staging is the complete managed population: Weaver deletes
matching target files absent from staging and rejects explicit deletes.
Non-matching target files are never counted, changed, or deleted.

### Folder rules

- **Staged files must match a File key.** Temporary download pages or intermediate
  artefacts should stay **outside** staging unless meant to persist — otherwise
  validation fails. Nested matching files count individually; directories are
  never CRUD units.
- **Deletion has one mode.** With `Auto delete: false`, delete entries are exact
  relative file names — never absolute, `..`-traversing, a glob, or a directory.
  With `Auto delete: true`, explicit deletes must be empty. A path cannot be both
  staged and explicitly deleted.
- **Reserved Weaver files** such as `_weaver.json` cannot be staged, replaced, or
  deleted.
- **Direct writes to the target are unsupported.** Do not write to
  `self.context.object_path`; stage instead.

If object code raises inside the `with` block, Weaver cleans the staging folder
automatically; on a normal return Weaver consumes and then cleans it after
reconciliation.

## The Table pair

1. a **Spark DataFrame** of rows to insert or update;
2. a sequence of **primary-key tuples** identifying rows to delete, in declared
   primary-key column order;

A Delta table without a primary key is a full replacement: its accepted incoming
rows become the complete table, and an empty incoming DataFrame empties it.

```python
# single-column primary key
primary_key_values_to_delete = (("order-17",), ("order-29",))

# composite primary key (declared order)
primary_key_values_to_delete = (("agency-a", "2026-07"), ("agency-b", "2026-06"))
```

### One deletion authority

> No primary key means no row deletion.

| Primary key | Auto delete | Explicit delete tuples | Result |
|---|---|---|---|
| absent  | false | empty     | allowed (no deletes) |
| absent  | false | populated | **error** |
| absent  | true  | any       | **error** |
| present | false | empty     | allowed (no deletes) |
| present | false | populated | apply explicit deletes |
| present | true  | empty     | derive automatic deletes |
| present | true  | populated | **error** |

`Auto delete: true` lets Weaver derive deletions by comparing returned keys with
existing keys; `Auto delete: false` deletes only the explicitly returned tuples.
A table cannot use both. Explicit delete tuples must match the primary-key arity,
contain no null values, and are deduplicated; a key that is both staged and
explicitly deleted is upserted (the delete is counted unmatched). Row CRUD counts
`deleted` for rows actually removed; details add `accepted`, `rejected`,
`auto_delete_ran`, and `explicit_delete_keys_read`/`matched`/`unmatched`.
