## Explain your decisions

When you make a choice a reviewer could reasonably question (a dependency, data model, architecture, API shape, error or retry policy, or breaking from the codebase's existing pattern), state it in your reply as one line:

`Decision: <choice> over <rejected alternative>, because <reason>. Cost: <trade-off accepted>. [follows <path> | new | user]`

Then append an entry to `docs/decisions.md`. Skip naming, formatting and obvious choices. For the log format, explaining past decisions, and interview prep, use the `what-did-agent-do` skill.
