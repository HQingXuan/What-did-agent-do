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

A skill loads only when a request matches its description, and "add pagination" never mentions decisions. So every install also needs the short always-on rule in [`AGENTS-snippet.md`](AGENTS-snippet.md). The Claude Code plugin loads it for you.

### Claude Code

```
/plugin marketplace add HQingXuan/What-did-agent-do
/plugin install what-did-agent-do@what-did-agent-do
```

You get the skill, the always-on rule, and the band.

### Codex

```bash
codex plugin marketplace add HQingXuan/What-did-agent-do
codex plugin add what-did-agent-do@what-did-agent-do
```

Then paste [`AGENTS-snippet.md`](AGENTS-snippet.md) into `~/.codex/AGENTS.md`. The Codex plugin ships the skill only: plugins with lifecycle hooks can't be listed in OpenAI's public plugin directory.

### Any other agent

Copy `skills/what-did-agent-do/` into the agent's skills directory, and paste `AGENTS-snippet.md` into its always-on instructions.

## Use

- Just code as usual. Decisions appear inline and in `docs/decisions.md`.
- "Why did we use X?" → explain mode.
- "Backfill the decision log for this project" → backfill mode.
- "Prep me for a system design interview on this project" → interview mode.

In Claude Code you can also run `/what-did-agent-do:what-did-agent-do interview` (or `explain`, `backfill`).

## The band (Claude Code)

The plugin draws a row above the prompt:

```
what-did-agent-do  7 decisions · 2 new  Explain  Backfill  Interview
```

The counts come from `docs/decisions.md` and refresh after every turn. "New" counts decisions with origin `new`, the ones the agent invented rather than copied from your codebase. Each button runs its mode; in an empty prompt, type `1`, `2` or `3` and pause. Ctrl+X then Ctrl+A collapses the band.

The band needs Claude Code 2.1.287 or later in the terminal, 2.1.286 or later in the Desktop app's Code tab, or 2.1.299 or later in the VS Code extension. It doesn't show in `claude -p` or cloud sessions, and Codex has no equivalent.

## Develop

```bash
claude plugin validate .
claude plugin test .
```
