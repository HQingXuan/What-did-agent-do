# interview

**Prompt:** /what-did-agent-do:what-did-agent-do interview

**Reply:**

I wrote the walkthrough to `docs/system-design.md`, with the 8 sections the interview format calls for. It reads aloud in about 10 minutes. Every claim is tagged `[logged D-n]`, `[from code path]` or `[inferred]`. I didn't change any code or `docs/decisions.md`.

**Four things to know before you present:**

1. **The 3 s title-fetch limit is not a total cap.** Only the page-body read checks the deadline. DNS lookup, connecting, TLS and reading the response headers each get their own timeout, and a fetch can follow up to 4 hops. In a local test, a server that sent its headers one byte at a time held a 0.5 s limit for 9.2 s. A slower server could hold it for hours, and each such link ties up one server thread. If an interviewer asks "what bounds your create latency?", the honest answer is "less than I thought" (section 8, item 2).
2. **`python app.py` only listens on localhost (`127.0.0.1`).** To be reachable from outside, something has to sit in front of it, like nginx. That is exactly the case where D-002's rate limit puts every client in one shared bucket of 10 creates per minute.
3. **The foundations aren't in the log.** SQLite, random 40-bit codes with no retry on collision, 302 redirects and no auth all came from the `start` commit, so their reasons are marked `[inferred]`. These are the most common URL-shortener interview questions, so be ready to give the reasons yourself.
4. **There's no traffic data.** The local database has 0 links, so every load number in section 1 is a guess and is tagged that way.

I tagged the foundations as inferred instead of stopping to reconstruct their log entries first. Your actual reasons for them would replace my guesses. Tell me what they were and I'll add them as D-008 onward. I can also fix items 1 and 2 separately; those are code changes.

**Want me to drill you?** I'll ask one question at a time and give short feedback against the log and code. If yes, here's the first question:

> **Walk me through the architecture: what happens, component by component, when someone creates a short link?**


**Diff:**

```diff

```
