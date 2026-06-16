# Repository Notes

## SQL style

- Use lower-case SQL keywords. The project convention is not to shout SQL.
- Put join predicates on the same line as the joined table when there is one predicate. Start new lines only for additional `and` / `or` predicates.
- Wrap `or` predicate groups in parentheses.
- Align table names and aliases where it improves scanability.
- Align column names in select and insert lists where it improves scanability.
- Use leading commas for column lists, not trailing commas.

