# fastactor

Elixir/OTP-style actors for Python, built on [anyio](https://anyio.readthedocs.io/).

`fastactor` mirrors the primitives that make OTP pleasant: a `Process` with a mailbox and lifecycle; a `GenServer` with synchronous `call` and asynchronous `cast`; `Supervisor` with `one_for_one` / `one_for_all` / `rest_for_one` strategies; a `DynamicSupervisor` for runtime-added children; a `Registry` for `unique`/`duplicate` name → process lookup with auto-cleanup; an `Agent` for state-holding workers; `Task` for one-shot awaitable coroutines; plus linking, monitoring, `trap_exits`, `handle_continue`, and named registration at `start()`.

> **Building against fastactor with a coding agent?** The full API reference — every public class, method, message type, and deterministic-sync testing pattern — is in [`llms.txt`](./llms.txt).

```python
from fastactor.otp import GenServer, Runtime, Call, Cast


class Counter(GenServer):
    count: int

    async def init(self, start: int = 0):
        self.count = start

    async def handle_call(self, call: Call):
        return self.count

    async def handle_cast(self, cast: Cast):
        match cast.message:
            case ("add", n): self.count += n
            case ("reset",): self.count = 0


async def main():
    async with Runtime():
        counter = await Counter.start(start=10)

        counter.cast(("add", 5))
        counter.cast(("add", 2))
        assert await counter.call(None) == 17

        await counter.stop()
```

## Status

Early but green. All three supervisor strategies (`one_for_one`, `one_for_all`, `rest_for_one`) work, the Runtime survives crashes cleanly, and the test suite covers ~92 canonical OTP scenarios — parametrized across restart modes × exit kinds, with deterministic synchronization (no `asyncio.sleep` to wait for state). Treat this as a reference implementation you can fork and hack on, not a production dependency.

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
    assert rt.where_is("some_server") is proc
```

#### Entry points

Three equivalent ways to bring up a Runtime — pick whichever matches your shape. All three install `SIGINT`/`SIGTERM` traps by default; receiving a signal cancels the runtime's task group and triggers clean shutdown. Opt out with `trap_signals=False`.

**Context manager** — idiomatic for tests and library code:

```python
import anyio
from fastactor.otp import Runtime

async def main():
    async with Runtime():
        counter = await Counter.start()
        ...

anyio.run(main)
```

**`fastactor.run(main)`** — one-line app entry point. Equivalent to `anyio.run` + `async with Runtime()` + clean-Ctrl-C handling:

```python
import fastactor

async def main():
    counter = await Counter.start()
    ...

fastactor.run(main)   # returns main's return value; absorbs signal-triggered cancellation
```

**`Runtime.start()` / `Runtime.stop()`** — explicit pair for REPLs and Jupyter, where `async with` is awkward:

```python
rt = await Runtime.start()
counter = await Counter.start()
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
        "counter",                      # child id
        Counter,                        # class (must expose start_link) or coroutine
        kwargs={"start": 0},
        restart="transient",            # permanent | transient | temporary
        shutdown=5,                     # seconds, "brutal_kill", or "infinity"
    )
    counter = await sup.start_child(spec)
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

Children are added at runtime instead of being declared in `child_specs`:

```python
from fastactor.otp import DynamicSupervisor

async with Runtime():
    pool = await DynamicSupervisor.start(max_children=10, extra_arguments=(shared_config,))
    worker = await pool.start_child(pool.child_spec(None, Worker, kwargs={"name": "alice"}))
```

`max_children` caps the pool; `extra_arguments` are prepended to every child's `start` tuple; ids auto-generate (`dyn:…`) when you pass `None`. Strategy is always `one_for_one`.

### Registry

A name-keyed lookup, separate from `Runtime.register_name` (which is for a single global name per process). `Registry` supports two modes:

- `unique` — one process per key; re-registering with the same key raises `AlreadyRegistered(pid)`.
- `duplicate` — many processes per key, useful for pub/sub fan-out.

```python
from fastactor.otp import Registry

await Registry.new("workers", mode="duplicate")
await Registry.register("workers", "jobs", worker1)
await Registry.register("workers", "jobs", worker2)

procs = await Registry.lookup("workers", "jobs")   # [worker1, worker2]
await Registry.dispatch("workers", "jobs", lambda p: p.send(Notify()))
```

Entries auto-scrub when a process terminates.

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

One-shot awaitable coroutines with crash-propagation semantics:

```python
from fastactor.otp import Task, TaskSupervisor

t1 = await Task.start(compute)            # unlinked: caller survives a task crash
t2 = await Task.start_link(compute)       # linked: caller dies on task crash
result = await t1                         # __await__ returns result or raises

tsup = await TaskSupervisor.start()
t3 = await tsup.run(compute)
```

Timeouts are the caller's responsibility: `with fail_after(5): result = await t`.

### handle_continue

Schedule a follow-up callback without re-entering the mailbox loop. Useful for multi-step init or post-processing:

```python
from fastactor.otp import Continue

class Bootstrap(GenServer):
    async def init(self, db_url):
        self.db_url = db_url
        return Continue("load")

    async def handle_continue(self, term):
        if term == "load":
            self.conn = await connect(self.db_url)
```

`handle_call` can return `(reply, Continue(term))` to piggyback a continuation on a reply.

### Named registration at start()

```python
g = await GenServer.start(name="sessions")
assert runtime.where_is("sessions") is g
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
