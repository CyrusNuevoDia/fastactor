# Why Actors?

## The four bets

Every concurrency model is a bet about _what the fundamental unit should be_. Different bets make different problems easy and different problems nearly impossible.

> **CSP** (Go channels, Python `trio`/`anyio`): the unit is the **channel** — a typed conduit for data flow. Processes are anonymous; the channel has the identity.
>
> **Task queues** (Celery, RQ, Dramatiq): the unit is the **task** — a stateless work item. Workers are fungible; the task is the thing.
>
> **Durable execution** (Temporal, Inngest, Restate): the unit is the **workflow** — a deterministic narrative that survives restarts via event-sourced replay.
>
> **Actors** (Erlang/OTP, Akka, Orleans, Ray): the unit is the **process** — an addressable, stateful agent with a mailbox, a lifecycle, and a supervisor.

Notice what each model treats as ontologically real versus merely instrumental. In Celery, your `Order` object is real; the worker that processes it is a disposable body the task borrows for a few milliseconds. In Erlang, the process _is_ the order — it lives for the order's duration, owns its state, and has a name you can route messages to.

This framing recurs throughout. **Hold on to it: what does each primitive reify?**

---

## Erlang/OTP's primitives, extracted

OTP is not really about "functional programming on the BEAM." It's a specific set of concurrency primitives that compose into a fault-tolerance discipline.

> **Processes** — Green-threaded, isolated, no shared heap. Cheap (tens of thousands per node is routine). Memory corruption between them isn't even expressible. Crashes are bounded to the process.

> **Mailboxes + selective receive** — Every process has a queue. You don't just pop the next message; you pattern-match and pull the _right_ message out. Subtle but enormous — it lets you write state-machine code that reads like linear code.

> **Links and monitors** — Two processes can be _linked_ so one dying kills the other (bidirectional), or _monitored_ so one watches the other and gets a notification on death (unidirectional). This is how failure propagates.

> **Supervisors** — A process whose only job is to restart its children according to a declared strategy (`one_for_one`, `one_for_all`, `rest_for_one`). You write supervision _trees_, and the tree _is_ the system's fault-tolerance policy.

> **Registry / naming** — Processes can have global or local names. You send messages to a name, not a transient handle. This enables **location transparency**: the same code works whether the target is local or across the cluster.

> **Behaviors** (`GenServer`, `GenStateM`, `Application`) — Templates for common process shapes. You fill in the callbacks; the library handles the loop, the selective receive, the state threading.

The philosophy gluing these together:

> **_"Let it crash."_** Don't defensively program around every error. When something unexpected happens, die cleanly. Let the supervisor restart you into a known-good state. This works _because_ processes are isolated — your death doesn't corrupt anyone else.

This is a _discipline_, not just a library. It only works if the primitives — isolation, cheap processes, supervisors, structured failure — are all simultaneously present.

---

## Three axes where actors differ

### 1. Where does state live?

| Model        | State location                               | Consequence                                                       |
| ------------ | -------------------------------------------- | ----------------------------------------------------------------- |
| **Celery**   | External (DB, Redis)                         | Stateless workers scale horizontally; every op is a round-trip    |
| **CSP**      | Inside goroutines, flowing through channels  | Great locality; hard to address specific state from outside       |
| **Temporal** | Event log; replayed into memory              | Survives anything; pays replay cost and determinism constraints   |
| **Actors**   | Inside the process, protected by the mailbox | Addressable _and_ fast; lost on crash unless explicitly persisted |

The actor model is the only one where **state has an address**. You can send a message to `user_session(42)` and the relevant state will process it — no DB lookup, no replay, no routing table in your application code.

### 2. What _is_ failure?

- **Celery**: a retry. Eventually, a dead-letter.
- **CSP**: an exception that propagates or closes a channel.
- **Temporal**: an activity failure that the workflow catches and compensates, with the workflow itself nearly immortal.
- **Actors**: a **supervised event**. A process dies; its supervisor decides recovery via a declared strategy. Siblings may or may not be affected based on that strategy.

Actors make failure _structural_. You don't write try/except around everything — you declare, at the supervisor level, _what recovery means for this subsystem_.

### 3. What's addressable?

- **Celery**: task IDs (but the running task isn't really a live thing you message).
- **CSP**: channels, not processes.
- **Temporal**: workflows by ID; signals let you message them.
- **Actors**: every process, by PID or name. _Everything is addressable._

This is what makes actors natural for agent-like systems. An agent _is_ an addressable stateful thing. "Tell agent 17 the user sent a message" — that's actor semantics natively. In a task queue, you'd reconstruct it by hand.

---

## The Python angle

Python has traditionally been weak at in-process actor concurrency for three reasons:

1. **The GIL** prevents real parallelism across threads in one process. Actor systems assume many processes run concurrently on different cores.
2. **No native cheap-process primitive.** You choose between `asyncio` tasks (cheap, cooperative, single-threaded), threads (OS-heavy, GIL-bound), or OS processes (heavy, IPC-costly). None is BEAM-process-like.
3. **The ecosystem made different bets.** Celery locked in the task-queue pattern circa 2010; `asyncio` made CSP-ish coordination the default newer pattern; Temporal and Inngest filled the durable-execution niche.

What you actually get when you reach for actors in Python:

> **Thespian, Pykka** — Classic actor libraries. Single-process mostly. Give you mailboxes and some supervision. Low-traffic usage today.
>
> **Ray Actors** — The closest thing to "real actors" at scale. Each actor is a separate OS process (often on a separate machine), addressable by handle, with mailboxes. Designed for ML workloads but works generally. Cost: cluster-oriented, no _spawn 100k actors in-process_ cheapness.
>
> **Dramatiq** — Task queue that calls workers "actors." Not really actors — no mailboxes, no supervision, no addressable state. A nice task queue, nonetheless.
>
> **FastStream / stream processors** — Actor-flavored if you squint. Kafka or a broker is doing the mailbox work.

The new hope: **subinterpreters** (PEP 554 / 734, usable in 3.13+) finally give Python independent interpreters with their own GILs in one process. This is the first time Python has had a _native_ substrate that could host Erlang-style in-process actor parallelism. Expect libraries to emerge here over the next couple of years.

---

## When actors beat the alternatives

Reach for actors when _at least two_ of these are true:

1. **State is per-entity and long-lived.** User sessions, game entities, IoT devices, agents, active collab documents. If you'd otherwise write `get_from_db → mutate → write_back` on every operation, you probably want an actor.
2. **Failure has structure.** Some subsystems should restart together; others should isolate. You want to _declare_ this, not thread try/except through business logic.
3. **You need back-pressure.** The mailbox _is_ the back-pressure mechanism. When a consumer is slow, the mailbox grows and you decide the policy (drop, shed load, restart).
4. **Addressable identity matters.** "Tell _that specific_ running thing to do X" is your dominant pattern.
5. **Soft real-time.** Telecoms, trading, games, live audio. Actors were _built_ for this — Ericsson's AXD301 switch is the canonical case.

### When they lose

- **Pure data pipelines** — CSP / stream processing is cleaner. Channels compose better than mailboxes when _data_ is the thing flowing, not control.
- **Stateless short tasks** — A task queue is the right answer. Don't overbuild.
- **Long business workflows with complex retry semantics across external systems** — Temporal's sweet spot. _Workflow-as-durable-narrative_ is the right primitive when your process is "charge card, wait 3 days, ship item, email receipt" and you need it to survive any crash.
- **Embarrassingly parallel compute** — Just use `multiprocessing` or Ray Tasks. Actors add overhead you don't need.

---

## The meta-pattern: primitives shape domains

Zoom out. The deepest reason to care about this distinction isn't performance or syntax — it's **isomorphism**. Each concurrency model lets you express some domains naturally and forces you to encode others.

Agent systems have a natural actor shape: agents are addressable, stateful, long-lived, and fail independently. _Forcing them into a task queue means manually reconstructing actor semantics on top of stateless workers and a database._ You'll end up building a worse actor runtime as a byproduct — complete with registries, mailboxes implemented as DB rows, and ad-hoc supervision logic scattered across retry decorators. Every mature agent framework on Celery eventually grows these limbs.

Conversely, forcing a straightforward stateless batch job into an actor system adds accidental complexity. You're reifying a process for something that didn't need identity.

> **The craft question is always**: _what does my domain treat as real?_ If the answer is "agents," "sessions," "devices," "rooms," "orders-in-progress" — addressable stateful things — actors are the primitive that matches. If the answer is "events," "tasks," "data flowing through transforms," or "business processes that must survive arbitrary downtime" — pick accordingly.

The Erlang wisdom isn't _actors are better_. It's that **the shape of your runtime should match the shape of your problem**, and for a surprising number of real systems — especially agentic and stateful ones — the actor shape fits. When it fits and you're missing it, you feel it as endless plumbing. When it doesn't fit, you feel it as overkill.

---

## A practical heuristic for Python today

| You have...                                             | Reach for                                                                             |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| One-shot async work, cron-like jobs                     | Celery / Dramatiq / RQ                                                                |
| Streaming data transforms                               | FastStream, `arq`, or raw `asyncio` with structured concurrency                       |
| Business workflows needing durability across days/weeks | Temporal (self-hosted) or Inngest (hosted)                                            |
| Stateful agents / sessions / per-entity compute         | Ray actors at scale, or roll your own on `asyncio` with a registry + supervisor layer |
| Multi-core in-process stateful concurrency              | Watch the subinterpreter space; nothing production-ready yet                          |

And if you find yourself writing _"we need to route messages to the specific running X, X must survive crashes with clean recovery, and we have hundreds of thousands of Xs"_ — you've discovered actors. The only question is whether to adopt a system that gives them to you, or build a weaker version yourself.

> **Greenspun's Tenth Rule, actor edition**: _Any sufficiently complex stateful-agent system built on a task queue contains an ad-hoc, informally-specified, bug-ridden, slow implementation of half of OTP._
