# fastactor

> **Greenspun's Tenth Rule, actor edition**: any sufficiently complex stateful-agent system built on a task queue contains an ad-hoc, informally-specified, bug-ridden, slow implementation of half of OTP.

Reach for this when you have per-entity long-lived state (agents, sessions, game entities, devices), structured failure semantics, or mailbox-shaped back-pressure. Reach for Celery/Dramatiq for stateless tasks, Temporal/Inngest for multi-day workflows, or raw `anyio` channels for data pipelines. Long version: [`writing/why_actors.md`](./writing/why_actors.md).

## Why actors?

Every concurrency model bets on what the fundamental unit should be. Task queues bet on the stateless **task**. Go and `anyio` bet on the **channel**. Temporal/Inngest/etc bets on the durable **workflow**.

Actors bet on the **process** — an addressable, stateful agent with a mailbox, a lifecycle, and a supervisor.

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from fastactor.otp import GenServer, Runtime, Call, Cast


class Conversation(GenServer):
    """One per live conversation. Owns a Claude Agent SDK client + sandbox dir."""

    async def init(self, conversation_id: str, workspace: str):
        self.conversation_id = conversation_id
        self.client = ClaudeSDKClient(options=ClaudeAgentOptions(cwd=workspace))
        await self.client.connect()

    async def terminate(self, reason):
        # The sandbox lifecycle IS the actor lifecycle — no leaks on crash or exit.
        await self.client.disconnect()
        await super().terminate(reason)

    async def handle_call(self, call: Call):
        await self.client.query(call.message)
        return [msg async for msg in self.client.receive_response()]

    async def handle_cast(self, cast: Cast):
        if cast.message == "interrupt":
            await self.client.interrupt()


async def main():
    async with Runtime():
        convo = await Conversation.start(
            conversation_id="conv-abc123",
            workspace="/tmp/sandboxes/conv-abc123",
        )

        chunks = await convo.call("Summarize the repo in a paragraph.")
        for chunk in chunks:
            print(chunk)            # render to the user however you like

        convo.cast("interrupt")     # fire-and-forget from another request handler
        await convo.stop()          # terminate() closes the SDK + sandbox
```

The sandbox is bound to the actor's lifetime: `terminate()` runs on any exit — clean stop, crash, supervisor shutdown — so a restarted process gets a fresh sandbox with no cleanup code in your request handlers. Scale this to many live conversations with `DynamicSupervisor` (one `Conversation` per `conversation_id`) and `Registry` (route `"conv-abc123"` → the right live process). The rest of this README walks through how.

## Why fastactor?

Python's existing actor options are either cluster-oriented (Ray), lightly maintained (Thespian, Pykka), or not really actors (Dramatiq — no mailboxes, no supervision, no addressable state). `fastactor` is an in-process OTP-shaped runtime on `anyio` that you can read end-to-end: a `Process` with a mailbox and lifecycle; a `GenServer` with synchronous `call` and asynchronous `cast`; `Supervisor` with `one_for_one` / `one_for_all` / `rest_for_one` strategies; a `DynamicSupervisor` for runtime-added children; a `Registry` for `unique`/`duplicate` name → process lookup with auto-cleanup; an `Agent` for state-holding workers; `Task` for one-shot awaitable coroutines; plus linking, monitoring, `trap_exits`, `handle_continue`, and named registration at `start()`.

> **Building against fastactor with a coding agent?** The full API reference — every public class, method, message type, and deterministic-sync testing pattern — is in [`llms.txt`](./llms.txt).

## Install

Requires Python 3.13+. The repo uses [uv](https://docs.astral.sh/uv/) and [mise](https://mise.jdx.dev/) for tool management:

```bash
uv sync
uv run pytest src
```

There is no PyPI release yet — install from source:

```bash
uv add "fastactor @ git+https://github.com/CyrusNuevoDia/fastactor"
```

## Core concepts

### Runtime

A `Runtime` owns the anyio task group everything else runs under, a top-level `RuntimeSupervisor` that traps exits, and the process registries. Exactly one `Runtime` may be active at a time; inside any actor you can always reach it with `Runtime.current()`.

```python
async with Runtime() as rt:
    proc = await SomeServer.start()
    rt.register_name("some_server", proc)
    assert await rt.whereis("some_server") is proc
```

#### Entry points

Three equivalent ways to bring up a Runtime — pick whichever matches your shape. All three install `SIGINT`/`SIGTERM` traps by default; receiving a signal cancels the runtime's task group and triggers clean shutdown. Opt out with `trap_signals=False`.

**Context manager** — idiomatic for tests and library code:

```python
import anyio
from fastactor.otp import Runtime

async def main():
    async with Runtime():
        convo = await Conversation.start(conversation_id="conv-abc", workspace="/tmp/conv-abc")
        ...

anyio.run(main)
```

**`fastactor.run(main)`** — one-line app entry point. Equivalent to `anyio.run` + `async with Runtime()` + clean-Ctrl-C handling:

```python
import fastactor

async def main():
    convo = await Conversation.start(conversation_id="conv-abc", workspace="/tmp/conv-abc")
    ...

fastactor.run(main)   # returns main's return value; absorbs signal-triggered cancellation
```

**`Runtime.start()` / `Runtime.stop()`** — explicit pair for REPLs and Jupyter, where `async with` is awkward:

```python
rt = await Runtime.start()
convo = await Conversation.start(conversation_id="conv-abc", workspace="/tmp/conv-abc")
# ... do work ...
await rt.stop()
```

Note: there is no truly "fire-and-forget, auto-cleanup-on-process-exit" mode — Python's `atexit` runs after the event loop is closed, so async cleanup can't be guaranteed that way. The signal trap gives you the same effective guarantee inside a live loop: SIGINT/SIGTERM arrive, the task group cancels, `__aexit__` finishes, process exits.

### Process

`Process` is the base actor. It has a bounded mailbox, a `_started` / `_stopped` pair of events, and `links` / `monitors` / `monitored_by` sets. Subclass it and override `init`, `handle_info`, `handle_exit`, or `terminate` as needed.

```python
from fastactor.otp import Process

class Worker(Process):
    async def init(self, *, name: str):
        self.name = name

    async def handle_info(self, message):
        print(self.name, "got", message)

p = await Worker.start(name="alice")
await p.send("hello")
await p.stop("normal")
```

`start_link` is the same as `start` but also links the new process with its supervisor, so an abnormal exit propagates.

### GenServer

`GenServer` adds `call` (synchronous, awaits a reply) and `cast` (fire-and-forget). Override `handle_call`, `handle_cast`, and `handle_info`:

```python
class Echo(GenServer):
    async def handle_call(self, call: Call):
        return call.message           # returned value becomes the reply

    async def handle_cast(self, cast: Cast):
        print("cast:", cast.message)  # no reply

e = await Echo.start()
print(await e.call("ping"))           # -> "ping"
e.cast("ignored")
```

If `handle_call` raises, the exception is both delivered to the caller (raised from `await proc.call(...)`) and propagated inside the actor, which then crashes — mirroring Elixir's `GenServer` semantics.

### Links, monitors, and `trap_exits`

- **Link** — bidirectional. If either side crashes abnormally, the other is stopped too. If a linked process has `trap_exits=True`, it instead receives an `Exit(sender, reason)` message handled by `handle_exit`.
- **Monitor** — one-way. When the monitored process terminates, the monitor receives a `Down(sender, reason)` message (via `handle_info`) and otherwise keeps running.

```python
from fastactor.otp import Down

class Watcher(GenServer):
    async def init(self):
        self.downs: list[Down] = []

    async def handle_info(self, msg):
        if isinstance(msg, Down):
            self.downs.append(msg)

watcher = await Watcher.start()
target  = await GenServer.start()
watcher.monitor(target)

await target.stop("normal")   # watcher.downs now contains one Down(target, "normal")
```

Normal shutdown reasons — `"normal"`, `"shutdown"`, or a `Shutdown(...)` instance — do not cascade across links. Anything else is treated as a crash and propagates.

### Supervisor

Supervisors run children according to a restart policy. Build a child spec with `Supervisor.child_spec(...)` and start it with `start_child`:

```python
from fastactor.otp import Supervisor

async with Runtime() as rt:
    sup = rt.supervisor                 # the root supervisor

    spec = sup.child_spec(
        "housekeeper",                  # child id
        Housekeeper,                    # class (must expose start_link) or coroutine
        kwargs={"interval_s": 60},
        restart="permanent",            # permanent | transient | temporary
        shutdown=5,                     # seconds, "brutal_kill", or "infinity"
    )
    housekeeper = await sup.start_child(spec)
    # If Housekeeper crashes, the supervisor terminates it cleanly (running
    # terminate()) and starts a fresh instance under the same id. Callers
    # holding a reference to the old process get a Down; a registry lookup
    # returns the new one.
```

Restart semantics:

| restart     | on normal exit | on crash       |
| ----------- | -------------- | -------------- |
| `permanent` | restart        | restart        |
| `transient` | do not restart | restart        |
| `temporary` | do not restart | do not restart |

`max_restarts` / `max_seconds` cap the restart intensity; breaching the cap fails the supervisor, which in turn propagates to its own supervisor. Management calls mirror OTP: `which_children`, `start_child`, `terminate_child`, `delete_child`, `restart_child`.

All three strategies are implemented: `one_for_one` restarts just the failing child; `one_for_all` terminates all siblings (in reverse spec order) and restarts them in forward order; `rest_for_one` restarts the failing child plus every child started after it, preserving the initialization order of earlier siblings.

### DynamicSupervisor

Children are added at runtime instead of being declared in `child_specs`. This is the right shape for "one actor per live conversation": a new chat thread arrives, a fresh `Conversation` is spawned, later the thread ends or crashes and the slot is reaped.

```python
from fastactor.otp import DynamicSupervisor

async with Runtime():
    conversations = await DynamicSupervisor.start(max_children=1000)

    # Spawn a Conversation on the first message of a new thread. The id
    # doubles as the registry key below, so routing stays stable across
    # supervisor-initiated restarts.
    convo = await conversations.start_child(conversations.child_spec(
        "conv-abc123", Conversation,
        kwargs={
            "conversation_id": "conv-abc123",
            "workspace": "/tmp/sandboxes/conv-abc123",
            "via": ("conversations", "conv-abc123"),   # re-register on restart
        },
        restart="transient",   # restart on crash, leave dead on normal stop
    ))
```

`max_children` caps the pool (back-pressure: hitting the cap raises `Failed` — the caller decides whether to queue, reject, or evict). `extra_arguments` is prepended to every child's `start` args — handy for passing a shared config or a parent handle to every conversation. Strategy is always `one_for_one`.

### Registry

A name-keyed lookup — the "phone book" that lets your request handler find `"conv-abc123"`'s live process without holding a reference. Two modes:

- `unique` — one process per key; re-registering raises `AlreadyRegistered(pid)`.
- `duplicate` — many processes per key, useful for pub/sub fan-out.

```python
from fastactor.otp import Registry, whereis

await Registry.new("conversations", "unique")

# The via= kwarg above on Conversation.start means registration happens
# *inside* init — so a supervisor-restarted replacement re-registers under
# the same key automatically, with no coordinating code.

convo = await whereis(("conversations", "conv-abc123"))   # -> the live Process
chunks = await convo.call("Add error handling to the auth module.")
```

Entries auto-scrub when the registered process terminates. For broadcast ("notify every live conversation belonging to org X"), use `duplicate` mode + `Registry.dispatch(key, callback)` — exceptions in one callback don't affect peers.

### Agent

A tiny state-holder over `GenServer`. Callbacks run inside the agent process; the state is whatever your factory returns.

```python
from fastactor.otp import Agent

counter = await Agent.start(lambda: 0)
await counter.update(lambda n: n + 1)
await counter.update(lambda n: n + 1)
assert await counter.get(lambda n: n) == 2
assert await counter.get_and_update(lambda n: (n, n * 10)) == 2
```

Functions may be sync or async.

### Task

One-shot awaitable coroutines with crash-propagation semantics. The shape you reach for when a conversation's turn fans out into N concurrent tool calls and you want isolated failure (one flaky API doesn't cancel the others — unlike `asyncio.gather`).

```python
from fastactor.otp import Task, TaskSupervisor

tools = await TaskSupervisor.start()

search = await tools.run(web_search, "erlang otp origin")
read   = await tools.run(read_file, "/workspace/README.md")
compute = await tools.run(eval_python, "sum(range(1_000_000))")

results = [await search, await read, await compute]   # each resolves or raises

# t.poll(timeout) is the non-raising variant — returns result, exception, or None.
outcome = await compute.poll(timeout=5)
```

A crashing tool raises only when the caller awaits _it_; sibling tools keep running. `Task.start_link(fn)` opts into crash cascade to the caller; `Task.start(fn)` (unlinked) keeps the caller alive. Timeouts are the caller's responsibility: `with fail_after(5): result = await t`.

### handle_continue

Schedule a follow-up callback that runs **before the next mailbox message**. Two shapes:

1. `init` returns `Continue(term)` → `handle_continue(term)` runs before the first message (multi-phase startup).
2. `handle_call` returns `(reply, Continue(term))` → the caller gets `reply` immediately; `handle_continue` runs next, mailbox still paused.

The "plan, reply, reflect" pattern for agents falls out naturally:

```python
from fastactor.otp import Call, Continue, GenServer

class PlanAndReflect(GenServer):
    async def init(self):
        self.reflections: list[str] = []

    async def handle_call(self, call: Call):
        plan = await llm.generate_plan(call.message)
        return plan, Continue(("reflect", plan))   # caller gets `plan` now

    async def handle_continue(self, term):
        match term:
            case ("reflect", plan):
                # Runs before the next message — so reflection can't interleave
                # with the next request. The mailbox is your serialization fence.
                self.reflections.append(await llm.critique(plan))
```

Stronger than chaining awaits inside one handler: nothing in the mailbox can interleave between the reply and the continuation.

### Named registration at start()

```python
g = await GenServer.start(name="sessions")
assert await runtime.whereis("sessions") is g
# A second start with the same name raises Failed("already_started: sessions")
```

Names are freed when the process terminates.

## Settings

Runtime defaults are exposed via `pydantic-settings` and can be overridden by environment variables prefixed with `FASTACTOR_`:

| setting                   | env var                             | default |
| ------------------------- | ----------------------------------- | ------- |
| `mailbox_size`            | `FASTACTOR_MAILBOX_SIZE`            | `1024`  |
| `call_timeout`            | `FASTACTOR_CALL_TIMEOUT`            | `5`     |
| `stop_timeout`            | `FASTACTOR_STOP_TIMEOUT`            | `60`    |
| `supervisor_max_restarts` | `FASTACTOR_SUPERVISOR_MAX_RESTARTS` | `3`     |
| `supervisor_max_seconds`  | `FASTACTOR_SUPERVISOR_MAX_SECONDS`  | `5.0`   |

```bash
FASTACTOR_MAILBOX_SIZE=4096 uv run python my_app.py
```

## Prior art

Erlang/OTP and Elixir's `GenServer` / `Supervisor` modules are the direct inspiration.

## License

MIT — see `LICENSE`.
