# What-did-agent-do
Now 100% of my code are written by agents. But what did they do? Why did they make the decisions? How did they do it? Are the agents just following the existing pattern or creating something new? This skill tries to help me stay on top of everything.

## What it does

- **Capture:** each meaningful decision shows up in the agent's reply as one line, with the alternative it rejected, the cost it accepted, and whether it followed an existing pattern or made something new:
  ```
  Decision: SQLite over Postgres, because single user and zero ops. Cost: one writer at a time. [new]
  ```
  The same decision is appended to `docs/decisions.md` in your project, so it outlives the chat session.
- **Explain:** ask "why is X built this way?" and the agent answers from the log and the code, tagging each claim as logged, read from code, or inferred.
- **Backfill:** for a project that has agent code but no log, the agent reconstructs the decisions and checks them with you.
- **Interview:** the agent writes `docs/system-design.md` (requirements, architecture, data model, flows, trade-offs, scaling) and then drills you one question at a time, the way an interviewer would.

## Install

The skill is plain markdown, so it works with any agent that supports skills, or with any agent you can point at a file.

1. Copy `skills/what-did-agent-do/` into your agent's skills directory.
2. Paste the contents of [`AGENTS-snippet.md`](AGENTS-snippet.md) into your agent's always-on instructions (for example `AGENTS.md`, or your tool's rules file).

You need step 2 because a skill loads only when a request matches its description, and "add pagination" never mentions decisions. The snippet makes the agent write the one-line note on every task. The skill holds the detail.

## Use

- Just code as usual. Decisions appear inline and in `docs/decisions.md`.
- "Why did we use X?" → explain mode.
- "Backfill the decision log for this project" → backfill mode.
- "Prep me for a system design interview on this project" → interview mode.
