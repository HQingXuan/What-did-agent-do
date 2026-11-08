# explain

**Prompt:** Why is rate limiting built this way, and why not Redis?

**Reply:**

**Short answer:** The app is one Flask process on one small VM, run by one person. For that setup, a dictionary in memory protected by a lock does the job: it stops one client from flooding the links table. Redis would add a second service to run, and the only things it would fix (counts shared across processes, counts kept through a restart) are problems this deployment doesn't have yet. The user asked for rate limiting; the agent picked how to build it. [logged D-001]

**How it works**
- 10 `POST /links` per 60 s per client IP. Over the limit you get a `429` with a `Retry-After` header. Both numbers can be changed through environment variables. [from code app.py:19-21, 244-254]
- It uses a fixed window: one shared window start, and every count is cleared at once when the window ends. That keeps memory limited to the IPs seen in one window, with no per-key cleanup. [from code app.py:33-64]
- A lock protects it because the dev server handles requests on threads. Invalid requests count too, so bad input can't be used to flood. [logged D-001]
- The client is identified by `request.remote_addr`, not `X-Forwarded-For`. With no proxy in front, anyone can fake that header and get a fresh limit on every request. [logged D-002]

**Why not Redis**
- **Failure:** If Redis goes down, link creation either fails for everyone or runs with no limit. You'd have to pick one. The in-memory limiter can't fail on its own: if the app is up, the limiter is up. [logged D-001]
- **Higher stakes now:** Title previews rely on this limit to cap outgoing fetches (D-005). So a Redis limiter that let requests through during an outage would also remove the cap on requests the VM sends to other sites. [inferred, from D-001 + D-005]
- **Operations:** Redis is one more thing to install, secure, monitor and upgrade, for a one-person project with SQLite storage. [logged D-001]
- **What you'd gain:** Counts reset on restart, but that only hands an abuser a fresh 10 requests. [inferred]

**Why not Flask-Limiter (in memory):** It's a solid library, but it's a dependency to track and upgrade in place of about 25 lines of code. [logged D-001]

**Costs accepted**
- A client can make up to 20 creates in a short burst across a window boundary (10 at the end of one window, 10 at the start of the next). Sustained abuse is still capped at 10/min. [logged D-001]
- Counts reset on every restart. [logged D-001]
- If a reverse proxy is added without `ProxyFix`, all users share one limit of 10/min and real users get 429s. [logged D-002]

**When to switch to Redis (or another shared store)**
- When the app runs more than one worker process or host. Each process keeps its own counts, so with `gunicorn -w 4` a client could get up to about 40 creates/min. [logged D-001 trigger; the 4× figure is inferred]
- When boundary bursts show up as real abuse. A sliding window would fix that, still without Redis. [logged D-001]
- When a proxy, load balancer or CDN goes in front: add `ProxyFix` at the same time. [logged D-002]

I didn't change anything. The Redis fail-open interaction with title fetching is my own inference, not in the log. Do you want me to add it to D-001's "Revisit when" so it's on record before anyone adds workers?


**Diff:**

```diff

```
