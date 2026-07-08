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

## Tests

Run from this repo:

```bash
cd /Users/matthiaswong/dev/dwg-platform/weaver
../.venv/bin/python -m pytest
```

Tests include a guard that `src/weaver_runtime` does not contain product or
environment defaults.

## SQL style

- Use lower-case SQL keywords.
- Put join predicates on the same line as the joined table when there is one predicate.
- Start new lines only for additional `and` / `or` predicates.
- Wrap `or` predicate groups in parentheses.
- Align table names, aliases, and column lists where it improves scanability.
- Use leading commas for column lists, not trailing commas.
