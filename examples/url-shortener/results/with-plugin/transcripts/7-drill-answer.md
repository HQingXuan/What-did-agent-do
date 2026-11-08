# drill-answer

**Prompt:** It's simpler.

**Reply:**

**Feedback:**
- **Right:** keeping things simple is a theme you can defend here. The whole design avoids a second service (D-001, D-003, D-005).
- **Wrong:** "simpler" doesn't answer this question. I asked you to trace one request, and an interviewer would ask "simpler than what?" and mark you down.
- **Add:** the steps in order: rate limiter, then URL check, then title fetch with the public-IP check, then code generation, then INSERT, then 201. Also say why the fetch runs before the INSERT: no database lock is held during network calls [logged D-005].

**Question 1, second try:** Trace `POST /links` for me in 4 or 5 sentences, from the request arriving to the 201.


**Diff:**

```diff

```
