---
name: what-did-agent-do
description: Records and explains the design decisions an agent makes while coding, so a human who did not write the code can still defend it. Use when the agent picks a library, data model, architecture, or trade-off; when the user asks "why is it built this way", "what did you decide", or "explain this project"; or when preparing for a system design interview about the project.
tags: [skill, decision-log, adr, explainability, system-design-interview]
---

# What Did Agent Do

## Overview
Makes every meaningful agent decision visible in one line at the time it is made, durable in a decision log, and explainable later, down to a system design interview.

## Usage
Use this skill when:
- You are about to make a decision a reviewer could reasonably question (see Core Concepts).
- The user asks why something was built a certain way, or what changed in a session.
- The user wants to understand an existing agent-written project that has no decision log.
- The user is preparing to present or interview on the project.

## Core Concepts

### What counts as a decision
A decision is a choice where a competent engineer could have picked differently and the difference matters later. Examples: a dependency, a storage or data model, sync vs. async, a caching or retry strategy, an API shape, error handling policy, where a boundary sits, or deviating from the codebase's existing pattern.

Not a decision: naming, formatting, or anything with one obvious answer. You MUST NOT log these, because a noisy log trains the user to stop reading it.

### Origin
Every decision has an origin, which answers "did the agent invent this?":
- `follows <path>`: copies a pattern already in the codebase.
- `new`: introduces something the codebase did not have before.
- `user`: the user asked for it.

### The one-line note
You MUST state each decision in your reply as one line, in this form:

```
Decision: <choice> over <rejected alternative>, because <reason>. Cost: <trade-off accepted>. [<origin>]
```

Examples:
```
Decision: SQLite over Postgres, because single user and zero ops. Cost: one writer at a time. [new]
Decision: cursor pagination over offset, because the feed is appended live. Cost: no jump-to-page. [follows api/orders.ts]
Decision: retry 3x with backoff on the payment call, because the provider rate-limits bursts. Cost: up to 7s extra latency on failure. [user]
```

Rules for the line:
- You MUST name the rejected alternative, since a choice with no alternative explains nothing.
- You MUST write `Cost: none` only when no cost exists. If you can't name a cost, you probably haven't thought it through.
- You SHOULD use numbers over adjectives ("up to 7s", not "some latency").
- Keep it under about 25 words so it can be read at a glance. Detail goes in the log.

## Modes

| User intent | Mode | Read |
|---|---|---|
| Normal coding work | Capture | [references/decision-log.md](references/decision-log.md) |
| "Why is X like this?", "what did you do?" | Explain | This section |
| Project has agent code but no log | Backfill | [references/decision-log.md](references/decision-log.md) |
| "Help me present / interview on this" | Interview | [references/interview-mode.md](references/interview-mode.md) |

If the skill is invoked with the argument `explain`, `backfill` or `interview`, you MUST run that mode directly.

### Capture
1. When you make a decision, write the one-line note in your reply.
2. Append an entry to `docs/decisions.md` in the project, using the format in the decision log reference.
3. If a new decision reverses an old one, mark the old entry `Superseded by D-<n>` rather than deleting it, because the history of why it changed is what interviewers probe.

### Explain
With no question, explain the decisions behind the current session's changes, or the latest diff if the session made none.

1. Read `docs/decisions.md`, then the code it points to.
2. Answer the question directly first, then give the trade-off.
3. You MUST tag each claim with its source: `[logged D-<n>]`, `[from code <path>]`, or `[inferred]`. You MUST NOT present an inferred reason as a logged one, because the user will repeat it in an interview as fact.
4. If the reason is only inferred and it matters, offer to backfill it into the log.

### Backfill
Reconstruct decisions for a project with no log. Follow the backfill steps in the decision log reference.

### Interview
Build a system design walkthrough from the log and code, then drill the user. Follow the interview mode reference.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Logging every edit | Log only choices a reviewer could question. |
| "Chose X because it's better" | Name the alternative and the cost. |
| Explaining from memory of the session | Re-read the log and code; sessions end, the log persists. |
| Writing a paragraph inline | One line inline, detail in the log. |
