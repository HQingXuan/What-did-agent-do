# Decision Log

The log lives at `docs/decisions.md` in the user's project. It is append-only, numbered, newest at the bottom.

## Entry format

```markdown
## D-007: SQLite over Postgres for local storage
- **Status:** Active | Superseded by D-<n>
- **Origin:** new | follows <path> | user
- **Where:** src/db/client.ts, src/db/schema.sql
- **Context:** Single-user desktop app. Data is under 100 MB. No server to run a database on.
- **Options:**
  - Postgres: concurrent writers, richer types. Needs a running server.
  - JSON files: no dependency. No queries, no transactions.
  - SQLite: one file, transactions, SQL. One writer at a time.
- **Choice:** SQLite.
- **Trade-off:** Gave up concurrent writes and server-side scaling to get zero ops.
- **Revisit when:** More than one process writes, or the app moves to a server.
```

Rules:
- You MUST list at least one rejected option with its downside, because the rejected options are what make the choice defensible.
- Each option's downside SHOULD include its production cost (what fails, what it takes to operate, who maintains it), not only its design properties.
- You MUST write "Revisit when" as a concrete trigger, since "if needs change" tells the reader nothing.
- You SHOULD keep each entry under 15 lines. Link to code instead of pasting it.
- If `docs/decisions.md` does not exist, create it with a `# Decisions` heading and start at D-001.

## Backfill

Use this when a project has agent-written code but no log.

Copy this checklist and track progress:
```
- [ ] 1. Map the project
- [ ] 2. List candidate decisions
- [ ] 3. Confirm the list with the user
- [ ] 4. Write entries
- [ ] 5. Verify entries against code
```

1. **Map the project.** Read the README, dependency manifest, entry points, and directory layout. Run `git log --stat` on the main branch if git history exists. Next, list candidates.
2. **List candidate decisions.** Look for: each direct dependency, each storage or external service, the module boundaries, concurrency model, auth, error handling, and any place one module does things differently from the rest. Next, confirm with the user.
3. **Confirm the list with the user.** Show the candidates as one-line notes, marked `[inferred]`. Ask which ones the user remembers a reason for. The user's answer overrides your inference. Next, write entries.
4. **Write entries.** Use the entry format. Set `Origin` from git history when possible. Where the reason is not known, write `**Context:** Reason not recorded; inferred from <evidence>.` Next, verify.
5. **Verify entries against code.** For each entry, open the `Where` files and confirm the choice is still what the code does. Fix or mark superseded any entry that does not match, then re-check it. Backfill is done when every entry matches the code.
