# What did agent do?

**Your agent writes the code. You still own the decisions.**

What-did-agent-do is a plugin for Claude Code, Codex and other coding agents. It makes the agent explain every design choice in one line as it works, keeps those choices in a log in your repo, and turns that log into a system design walkthrough you can present in an interview.

```
Decision: SQLite over Postgres, because single user and zero ops. Cost: one writer at a time. [new]
```

## Why I built this

Now 100% of my code is written by agents. But what did they do? Why did they make those decisions? Did they follow the patterns already in my codebase, or invent something new? I could ship features, but I couldn't always explain them. This plugin keeps me on top of my own projects.

## Who it's for

### You let agents write most of your code, and it's starting to feel like someone else's project

An agent makes many choices in a single task: which library, which data model, where to retry, where to cache. Most of them disappear into a long chat. A week later the code works and nobody knows why it looks the way it does, including you.

With this plugin:
- Every choice that matters shows up in the reply as one line, with the option the agent rejected and the cost it accepted.
- Each line says whether the agent **followed** a pattern in your codebase or made something **new**, so you know what to review closely.
- The choices are saved to `docs/decisions.md`, so they outlive the chat session and show up in code review.
- When you ask "why is this built this way?", the agent answers from the log and the code, and labels anything it's guessing.

You stay the person who can explain the system.

### You're preparing for a system design interview

The best project to talk about is one you built. But "I built it with an agent" is a weak answer to "why did you choose X over Y?"

Interview mode:
- Writes `docs/system-design.md` from your real code and decision log: requirements, architecture diagram, data model, key flows, trade-offs, what breaks at 10x and 100x, and what you'd change.
- Then acts as the interviewer. It asks one question at a time, checks your answer against the code, and tells you what you missed.

```
Agent: Why SQLite and not Postgres?
You:   It's simpler.
Agent: Right direction, too vague. Add the constraint and the cost: single user and
       under 100 MB, so zero ops wins; you gave up concurrent writers [logged D-007].
       Expect the follow-up "when would you switch?": more than one writer process,
       or moving to a server.
```

When you stop, it lists your three weakest answers, so you know what to study.

## What you get

| Mode | When | What happens |
|---|---|---|
| **Capture** | Always on, while you code | One-line decision notes in replies, full entries in `docs/decisions.md` |
| **Explain** | "Why did we use X?" or "What did you just do?" | Answers from the log and code, each claim tagged `logged`, `from code` or `inferred` |
| **Backfill** | You have agent-written code but no log | Reconstructs past decisions from code and git history, confirms them with you, then writes the log |
| **Interview** | "Prep me for a system design interview on this project" | Writes the walkthrough, then drills you |

A logged decision looks like this:

```markdown
## D-007: SQLite over Postgres for local storage
- **Status:** Active
- **Origin:** new
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

### The band (Claude Code)

In Claude Code, the plugin adds a row above the prompt:

```
what-did-agent-do  7 decisions · 2 new  Explain  Backfill  Interview
```

You can see at a glance how many decisions the agent has made and how many it invented. Click a button, or type `1`, `2` or `3` in an empty prompt, to start a mode.

## Install

### Claude Code

```
/plugin marketplace add HQingXuan/What-did-agent-do
/plugin install what-did-agent-do@what-did-agent-do
```

This installs everything: the skill, the always-on capture rule, and the band.

### Codex

```bash
codex plugin marketplace add HQingXuan/What-did-agent-do
codex plugin add what-did-agent-do@what-did-agent-do
```

Then paste [`AGENTS-snippet.md`](AGENTS-snippet.md) into `~/.codex/AGENTS.md` to turn on capture. The Codex plugin ships the skill only, since plugins with lifecycle hooks can't be listed in OpenAI's public plugin directory.

### Any other agent

Copy `skills/what-did-agent-do/` into your agent's skills directory, and paste [`AGENTS-snippet.md`](AGENTS-snippet.md) into its always-on instructions.

## Use

- **Code as usual.** Decisions appear in replies and in `docs/decisions.md`.
- **Ask why.** "Why did we use X?" or "Explain what you just did."
- **Catch up on an old project.** "Backfill the decision log for this project."
- **Prepare to present.** "Prep me for a system design interview on this project."

In Claude Code you can also run `/what-did-agent-do:what-did-agent-do interview` (or `explain`, `backfill`), or use the band.

## FAQ

**Won't this flood my chat with notes?**
No. The agent logs only choices a reviewer could reasonably question, such as a dependency, a data model or a retry policy. Naming, formatting and obvious choices are skipped.

**Can it make up reasons?**
The agent can guess, but it has to say so. Every claim in explain and interview mode is tagged `logged`, `from code` or `inferred`. That matters most in an interview, where a guessed reason repeated as fact is worse than "I don't know".

**Will it give me textbook answers?**
No. The agent decides and explains like the engineer who gets paged for the result. It weighs what fails when a dependency is down, who has to run and fix it, and whether the gain is worth the effort. A theoretically better design loses to a simpler one that keeps working, and the explanation says what was given up.

**Does my code leave my machine?**
The plugin doesn't send anything anywhere. The log and the walkthrough are plain markdown files in your repo. The band only reads `docs/decisions.md`.

**What do I need for the band?**
Claude Code 2.1.287 or later in the terminal, 2.1.286 or later in the Desktop app's Code tab, or 2.1.299 or later in the VS Code extension. The band doesn't show in `claude -p` or cloud sessions, and Codex has no equivalent.

## Develop

```bash
claude plugin validate .
claude plugin test .
```

## License

MIT
