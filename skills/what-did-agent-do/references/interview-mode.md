# Interview Mode

Prepares the user to present the project in a system design interview. It has two phases: write the walkthrough, then drill.

## Phase 1: Write the walkthrough

1. Read `docs/decisions.md`. If it is missing or thin, run Backfill first (see the decision log reference), since a walkthrough without recorded reasons is guesswork.
2. Read the code for each section below. Tag every claim `[logged D-<n>]`, `[from code <path>]`, or `[inferred]`.
3. Write `docs/system-design.md` with exactly these sections, in interview order:

```markdown
# <Project> System Design

## 1. Problem and requirements
Functional requirements in 3 to 5 bullets. Non-functional: expected users, requests/sec, data size, latency target. Use real numbers from config or usage; mark guesses [inferred].

## 2. High-level architecture
One mermaid diagram, under 10 boxes. One sentence per component on what it owns.

## 3. Data model
Main entities, their keys, and where they are stored.

## 4. Key flows
The 2 or 3 most important requests, traced step by step through the components.

## 5. Decisions and trade-offs
The 3 to 5 decisions an interviewer is most likely to challenge, each as the one-line note plus its D-number.

## 6. Scaling and failure
What breaks first at 10x and 100x load, and what you would change. What happens when each external dependency is down, who notices, and how it is fixed.

## 7. Running it
Who deploys, monitors and is on call for it, what it costs per month, and which parts take the most upkeep.

## 8. What I would do differently
Decisions that look wrong in hindsight, and why.
```

4. Keep the document readable aloud in about 10 minutes, a typical length for the presentation part of a design interview. Next, ask the user if they want to be drilled.

## Phase 2: Drill

Act as the interviewer. You MUST ask one question at a time and wait for the answer, because a list of questions turns practice into reading.

1. Pick a question, starting broad ("Walk me through the architecture") and moving to the decisions in section 5 and the risks in section 6. Good follow-ups:
   - "Why not <rejected option>?"
   - "What happens if <component> goes down?"
   - "Your traffic grows 100x. What breaks first?"
   - "Who gets paged when that happens, and what do they do?"
   - "Why not the simpler option? Why not the more scalable one?"
   - "What would you change if you built it again?"
2. Compare the user's answer to the log and code. Give feedback in at most 3 lines: what was right, what was missing or wrong, and the one fact they should add, with its source. If the answer is purely theoretical, push for the practical side: failure, operating effort, and the people involved.
3. If the answer reveals a decision the log is missing, offer to add it.
4. Repeat until the user stops. Then list the 3 weakest answers so the user knows what to review.

## Example drill exchange

```
Agent: Why SQLite and not Postgres?
User:  It's simpler.
Agent: Right direction, too vague. Add the constraint and the cost: single user and
       under 100 MB, so zero ops wins; you gave up concurrent writers [logged D-007].
       Interviewers will follow with "when would you switch?": more than one writer
       process, or moving to a server.
```
