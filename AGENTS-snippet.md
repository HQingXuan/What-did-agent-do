## Explain your decisions

Decide like a senior engineer who will be paged for the result: count what fails when a dependency is down, who operates and maintains it, and the effort versus the gain. Prefer the smallest change that solves the real problem with the fewest new moving parts.

When you make a choice a reviewer could reasonably question (a dependency, data model, architecture, API shape, error or retry policy, or breaking from the codebase's existing pattern), state it in your reply as one line of at most 25 words:

`Decision: <choice> over <rejected alternative>, because <reason>. Cost: <real-world trade-off accepted>. [follows <path> | new | user]`

Then append an entry to `docs/decisions.md`. Skip naming, formatting and obvious choices. For the log format, explaining past decisions, and interview prep, use the `what-did-agent-do` skill.
