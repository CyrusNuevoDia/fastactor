# `fastactor.otp` — API reference

Per-primitive deep-dive. For the Elixir↔Python mental model, idiomatic patterns, and common gotchas, see [`llms.txt`](../../../llms.txt) at the repo root. For testing patterns, see [`src/tests/README.md`](../../tests/README.md).

Each section opens with direct links to its source file and its test file. Read the source for exact semantics — this README covers contracts, return shapes, and the moving parts that aren't obvious from the code alone.

---

## Process

**Source:** [`process.py`](./process.py) · **Tests:** [`../../tests/test_process.py`](../../tests/test_process.py)

The base actor. Bounded mailbox, `_started` / `_stopped` events, `links` / `monitors` / `monitored_by` sets, lifecycle driven by `loop()`. Subclass and override the callbacks you need.

### Callbacks

```python
async def init(self, *args, **kwargs) -> Ignore | Stop | Continue | None:
    """Runs once before the mailbox loop. Return value controls what happens next."""
    return None

async def handle_info(self, message: Any) -> Continue | None:
    """Called for every non-standard message (anything not Stop/Exit/Call/Cast)."""

async def handle_exit(self, message: Exit):
    """Only called when trap_exits=True and a linked process exits."""

async def handle_down(self, message: Down):
    """Called on monitored-process termination. Default base-class no-op — override to react.
    `Down` also lands in `handle_info` when routed through the mailbox; GenServer uses that path."""

async def handle_continue(self, term: Any):
    """Runs next after a callback returns Continue(term)."""

async def on_terminate(self, reason: Any):
    """User cleanup hook. Runs BEFORE monitors/links are notified, so observers see the
    post-cleanup state. Exceptions here are logged but do NOT block the Down/Exit fanout.
    Prefer this over overriding `terminate`."""

async def terminate(self, reason: Any):
    """Machinery teardown — calls `on_terminate`, notifies monitors/links, unregisters.
    You almost never override this; override `on_terminate` instead."""
```

### Starting, stopping, sending

```python
# Factory methods (classmethods)
await Worker.start(*args, trap_exits=False, supervisor=None, name=None, via=None, **kwargs)
await Worker.start_link(*args, trap_exits=False, supervisor=None, name=None, via=None, **kwargs)
# `name=` registers under the runtime's global name map; `via=(registry, key)` registers
# into a user-created Registry. They are mutually exclusive.

# Instance methods
await proc.send(msg)                # async send; waits if mailbox is full
proc.send_nowait(msg)               # raises if full
await proc.stop(reason="normal", timeout=60, sender=None)
await proc.kill()                   # closes the mailbox, unwinds immediately
proc.info(msg, sender=None)         # wraps in Info(sender, msg) and send_nowait

# Lifecycle observation
proc.has_started() -> bool
await proc.started() -> Self        # waits until init completes
proc.has_stopped() -> bool
await proc.stopped() -> Self        # waits until terminate completes
```

### Init return sentinels

| Return             | Effect                                                                         |
| ------------------ | ------------------------------------------------------------------------------ |
| `None`             | Normal start, enter mailbox loop.                                              |
| `Ignore()`         | Skip the loop, never run; `_started` and `_stopped` both set.                  |
| `Stop(reason=...)` | Refuse to start; emits `Shutdown(reason)`; crashes if reason is abnormal.      |
| `Continue(term)`   | Start normally, but run `handle_continue(term)` before the first mailbox read. |

### Minimal example

```python
from fastactor.otp import Process, Runtime

class Worker(Process):
    async def init(self, *, name: str):
        self.name = name

    async def handle_info(self, message):
        print(self.name, "got", message)

async def main():
    async with Runtime():
        p = await Worker.start(name="alice")
        await p.send("hello")
        await p.stop("normal")
```

### Equality and identity

`Process.__hash__` and `__eq__` are by `id` (a ksuid string from `utils.id_generator`). Two `Process` instances with the same id compare equal — this is what makes `links`/`monitors` sets work.

---

## GenServer

**Source:** [`gen_server.py`](./gen_server.py) · **Tests:** [`../../tests/test_gen_server.py`](../../tests/test_gen_server.py)

Inherits from `Process`. Adds `call` (synchronous, awaits a reply) and `cast` (fire-and-forget).

### Callbacks

```python
async def handle_call(
    self, call: Call,
) -> Any | None | tuple[Any, Continue] | tuple[Any, Stop]:
    # Return contract — pick one of these:
    #   None                         → NoReply; caller stays blocked. Stash `call` and later
    #                                  invoke `call.set_result(value)` to resolve the reply.
    #   value                        → Reply with `value`.
    #   (value, Continue(term))      → Reply with `value`, then run handle_continue(term).
    #   (value, Stop(reason))        → Reply with `value`, then stop with `reason`.
    #   raise                        → Exception propagates to caller AND crashes the server.
    return call.message

async def handle_cast(self, cast: Cast) -> Continue | None:
    # no reply; optionally return Continue(term) to schedule handle_continue
    ...

async def handle_info(self, message) -> Continue | None: ...
```

### Client API

```python
await server.call(request, sender: Process | None = None, timeout=5) -> Any
server.cast(request, sender: Process | None = None)           # sync call, not awaitable
```

`sender` defaults to the current process (via `_current_process` ContextVar) or the supervisor. `call` raises `TimeoutError` on timeout and re-raises any exception thrown inside `handle_call`.

### Deferred reply — `handle_call` returning `None`

Returning `None` from `handle_call` means "don't reply yet". Stash the `Call` object, finish your callback, and later — from `handle_info`, `handle_continue`, or another `handle_call` — call `saved_call.set_result(value)` to unblock the waiting caller. This is the fastactor equivalent of Elixir's `{:noreply, state}` + `GenServer.reply(from, v)`.

```python
class Deferred(GenServer):
    async def init(self):
        self.pending: Call | None = None

    async def handle_call(self, call: Call):
        if call.message == "wait":
            self.pending = call
            return None                    # NoReply — caller stays blocked
        if call.message == "release":
            if self.pending is not None:
                self.pending.set_result("done")
                self.pending = None
            return "ok"
```

Because `None` is the NoReply sentinel, to reply with the _literal_ value `None` use the tuple form `return (None, Continue("done"))` or resolve via `call.set_result(None)` explicitly.

### Call and Cast wrappers

Callbacks receive wrapped messages. Unpack with `.message`:

```python
async def handle_call(self, call: Call):
    match call.message:
        case ("get", key):      return self.store[key]
        case ("put", key, val): self.store[key] = val; return "ok"
```

Do not write `async def handle_call(self, request, from_, state):` — that's Elixir, not fastactor.

### If `handle_call` raises

The exception is both delivered to the caller (re-raised from `await server.call(...)`) AND re-raised inside the server process, which then crashes. This mirrors Elixir's `GenServer` semantics.

### Counter example

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
        assert await counter.call(None) == 17       # FIFO mailbox: casts already processed
        await counter.stop()
```

A subsequent `call` round-trip guarantees prior casts have been processed. Use this in tests instead of `asyncio.sleep`.

---

## Links, monitors, `trap_exits`

**Source:** [`process.py`](./process.py) · **Tests:** [`../../tests/test_links_and_monitors.py`](../../tests/test_links_and_monitors.py)

Two relationships, both OTP-identical:

- **Link** — bidirectional. If either side exits abnormally, the other is stopped with the same reason. If a linked process has `trap_exits=True`, it instead receives `Exit(sender, reason)` via `handle_exit`.
- **Monitor** — one-way. When the monitored process terminates (for any reason), the monitor receives `Down(sender, reason)` via `handle_info` and keeps running.

```python
from fastactor.otp import Down, GenServer, Runtime

class Watcher(GenServer):
    async def init(self):
        self.downs: list[Down] = []

    async def handle_info(self, msg):
        if isinstance(msg, Down):
            self.downs.append(msg)

async def main():
    async with Runtime():
        watcher = await Watcher.start()
        target  = await GenServer.start()
        watcher.monitor(target)

        await target.stop("normal")
        await target.stopped()
        # watcher.downs has one Down(target, "normal")
```

### Normal vs abnormal reasons

Only these reasons are normal (do not cascade across links, do not trigger `transient` restarts):

- `"normal"`
- `"shutdown"`
- an instance of `Shutdown(...)`

Everything else — including `None`, `""`, `"ok"` — is a crash and propagates. `is_normal_shutdown_reason` in `_exceptions.py` is the single source of truth.

### Operations

```python
proc.link(other)
proc.unlink(other)
proc.monitor(other)
proc.demonitor(other)
```

All are synchronous; they mutate the local sets on both processes.

---

## Supervisor

**Source:** [`supervisor.py`](./supervisor.py) · **Tests:** [`../../tests/test_supervisor_strategies.py`](../../tests/test_supervisor_strategies.py), [`../../tests/test_supervisor_lifecycle.py`](../../tests/test_supervisor_lifecycle.py)

Runs children according to a restart policy. Build specs with `Supervisor.child_spec(...)`, start them with `start_child(spec)`.

### Strategies

| `strategy`     | On child exit                                                                               |
| -------------- | ------------------------------------------------------------------------------------------- |
| `one_for_one`  | Restart just the failing child.                                                             |
| `one_for_all`  | Terminate all siblings (in reverse spec order) and restart them in forward order.           |
| `rest_for_one` | Restart the failing child plus every child started after it; earlier siblings keep running. |

### Restart types (per child)

| `restart`   | Normal exit   | Crash         |
| ----------- | ------------- | ------------- |
| `permanent` | restart       | restart       |
| `transient` | don't restart | restart       |
| `temporary` | don't restart | don't restart |

### Shutdown types (per child)

| `shutdown`      | Behavior                                                                         |
| --------------- | -------------------------------------------------------------------------------- |
| `int` (seconds) | `proc.stop(reason, timeout=shutdown)`; kill on timeout.                          |
| `"infinity"`    | `proc.stop(reason, timeout=None)`. Wait forever.                                 |
| `"brutal_kill"` | `proc.kill()` immediately — closes the mailbox, no terminate callback completes. |

### Restart intensity

`max_restarts` (default 3, env `FASTACTOR_SUPERVISOR_MAX_RESTARTS`) / `max_seconds` (default 5.0, env `FASTACTOR_SUPERVISOR_MAX_SECONDS`) cap how often the supervisor will restart. Exceeding the cap crashes the supervisor itself with `Failed("Max restart intensity reached")`, which then propagates to its own supervisor.

### API

```python
spec = Supervisor.child_spec(
    child_id: str,           # unique within this supervisor; required
    func_or_class,           # class with start_link (preferred) or async callable
    args: tuple | None = None,
    kwargs: dict | None = None,
    restart: RestartType = "permanent",
    shutdown: ShutdownType = 5,
    type: Literal["worker", "supervisor"] = "worker",
    modules: list | None = None,
    significant: bool = False,
) -> ChildSpec

sup = await Supervisor.start(
    strategy="one_for_one",  # or "one_for_all" / "rest_for_one"
    max_restarts=3,
    max_seconds=5,
    children=[spec_or_tuple, ...],   # optional; seeds before the strategy loop starts
)

proc   = await sup.start_child(spec_or_tuple)     # ChildSpec OR (cls, kwargs_dict) tuple
await sup.terminate_child(child_id)
sup.delete_child(child_id)                # spec must have been terminated first
proc   = await sup.restart_child(child_id) # for stopped children
infos  = sup.which_children()              # list of dicts with id/process/restart/shutdown/type/significant
counts = sup.count_children()              # {"specs": n, "active": n, "workers": n, "supervisors": n}
```

### Tuple-form child specs

Both `Supervisor` and `DynamicSupervisor` accept a 2-tuple `(cls, kwargs_dict)` anywhere a `ChildSpec` is expected — `start_child`, `children=` seeding, and `_normalize_child_spec`. Child id defaults to `cls.__name__`; collisions are still rejected.

```python
await sup.start_child(Supervisor.child_spec("counter", Counter, kwargs={"start": 0}))
await sup.start_child((Counter, {"start": 0}))                              # id = "Counter"
sup = await Supervisor.start(children=[(Counter, {"start": 0}), DbConnSpec])
```

### Root supervisor

`Runtime.supervisor` is a pre-installed `RuntimeSupervisor` with `trap_exits=True`. When you call `Worker.start_link()` without an explicit `supervisor=`, the link is made to the root. For real applications, start your own `Supervisor` as a child of the root and hang your worker tree off that.

The root supervisor also maintains a bounded crash log — see `CrashRecord`, `recent_crashes(n)`, `crash_counts()`, `total_crashes()` in [`runtime.py`](./runtime.py).

---

## DynamicSupervisor

**Source:** [`dynamic_supervisor.py`](./dynamic_supervisor.py) · **Tests:** [`../../tests/test_dynamic_supervisor.py`](../../tests/test_dynamic_supervisor.py)

Children aren't declared up front; they're added one at a time. Strategy is always `one_for_one`.

```python
pool = await DynamicSupervisor.start(
    max_children: int | float = float("inf"),
    extra_arguments: tuple = (),     # prepended to every child's start tuple
)

# Dynamic children default to restart="temporary"
spec = DynamicSupervisor.child_spec(
    None,                    # pass None to auto-generate a "dyn:..." id
    Worker,
    kwargs={"name": "alice"},
    restart="temporary",
)
worker = await pool.start_child(spec)
```

Differences from `Supervisor`:

- `child_id` can be `None` → auto-generated `"dyn:<ksuid>"`.
- Default `restart` is `"temporary"` instead of `"permanent"`.
- Passing `strategy != "one_for_one"` in `init` raises `ValueError`.

---

## Registry

**Source:** [`registry.py`](./registry.py) · **Tests:** [`../../tests/test_registry.py`](../../tests/test_registry.py)

A keyed map from arbitrary keys to processes. Distinct from `Runtime.register_name`, which is for a single global name per process.

Two modes:

- `unique` — one process per key. Re-registering raises `AlreadyRegistered(pid)`.
- `duplicate` — many processes per key. Useful for pub/sub fan-out.

Entries auto-scrub when a process terminates (hooked into `Runtime.unregister(proc)` via `_proc_keys`).

```python
await Registry.new(name: str, mode: Literal["unique", "duplicate"]) -> None
await Registry.register(name, key, proc) -> None        # raises AlreadyRegistered in unique mode
await Registry.unregister(name, key, proc) -> None
await Registry.lookup(name, key) -> list[Process]        # always a list; len ≤ 1 for unique
await Registry.dispatch(name, key, fn) -> None
await Registry.keys(name, proc) -> list
```

`dispatch` snapshots the pid set under the registry lock, releases the lock, and then awaits `fn(proc)` for each. Exceptions in `fn` are logged and swallowed so one slow/broken subscriber can't break the fan-out.

### Pub/sub fan-out

```python
from fastactor.otp import Registry, Runtime

async with Runtime():
    await Registry.new("events", mode="duplicate")
    sub1 = await Subscriber.start(); await Registry.register("events", "news", sub1)
    sub2 = await Subscriber.start(); await Registry.register("events", "news", sub2)

    async def notify(proc):
        await proc.send(Info(None, "breaking news"))
    await Registry.dispatch("events", "news", notify)

    procs = await Registry.lookup("events", "news")  # [sub1, sub2] (order not stable)
```

---

## Agent

**Source:** [`agent.py`](./agent.py) · **Tests:** [`../../tests/test_agent.py`](../../tests/test_agent.py)

Thin wrapper over `GenServer` for "I just need to hold some state". Callbacks run inside the agent process; state is whatever the factory returns.

```python
counter = await Agent.start(factory: Callable[[], S | Awaitable[S]])

await agent.get(fn, timeout=5) -> R
await agent.update(fn, timeout=5) -> None
await agent.get_and_update(fn, timeout=5) -> R
agent.cast_update(fn)                      # fire-and-forget state update
```

Functions may be sync or async — `_maybe_await` handles both.

```python
from fastactor.otp import Agent, Runtime

async with Runtime():
    counter = await Agent.start(lambda: 0)
    await counter.update(lambda n: n + 1)
    await counter.update(lambda n: n + 1)
    assert await counter.get(lambda n: n) == 2
    assert await counter.get_and_update(lambda n: (n, n * 10)) == 2
    assert await counter.get(lambda n: n) == 20
```

---

## Task & TaskSupervisor

**Source:** [`task.py`](./task.py) · **Tests:** [`../../tests/test_task.py`](../../tests/test_task.py)

A `Task` runs a single coroutine once, captures the result (or exception), and exposes it through `__await__`. Two flavors — exactly the equivalent of Elixir's `Task.async_nolink` vs `Task.async`:

| Factory                    | Link? | Elixir analogue     | Failure behavior                                     |
| -------------------------- | ----- | ------------------- | ---------------------------------------------------- |
| `Task.start(fn, ...)`      | No    | `Task.async_nolink` | Caller survives a task crash; inspect via `await t`. |
| `Task.start_link(fn, ...)` | Yes   | `Task.async`        | A task crash cascades to the caller via link.        |

### Consumption

```python
task = await Task.start(compute)    # returns immediately — task runs in background
result = await task                 # blocks until done, raises if task raised
```

Timeouts are the caller's responsibility — wrap the await:

```python
from anyio import fail_after
with fail_after(5):
    result = await task
```

### Task post-start operations

```python
# Tri-state poll — does NOT raise on crash:
#   returns the result on success,
#   returns the Exception object on crash,
#   returns None on timeout.
result = await task.yield_(timeout=1)

# Graceful stop; falls back to kill() if the task doesn't stop within `timeout`.
await task.shutdown(timeout=5)
```

### TaskSupervisor

```python
tsup = await TaskSupervisor.start()
task = await tsup.async_(compute, arg1, arg2, kwarg=value)   # primary name
task = await tsup.run(compute, arg1, arg2, kwarg=value)      # backward-compat alias
result = await task
```

`TaskSupervisor` is a `DynamicSupervisor` under the hood. `async_` is the idiomatic name (Elixir calls it `Task.Supervisor.async/2`); the trailing underscore is only because `async` is a Python keyword. `run` is preserved as an alias.

### Fan-out parallelism

```python
from fastactor.otp import Task, Runtime
from anyio import fail_after

async with Runtime():
    tasks = [await Task.start(fetch, url) for url in urls]
    with fail_after(10):
        results = [await t for t in tasks]
```

---

## `handle_continue`

**Source:** [`process.py`](./process.py), [`gen_server.py`](./gen_server.py) · **Tests:** `ContinueServer` in [`../../tests/support.py`](../../tests/support.py)

Elixir-style `{:continue, term}`. Schedules a follow-up callback without re-entering the mailbox loop. Useful for deferred init, cleanup after a call, or chaining long work.

- `init` may return `Continue(term)` → `handle_continue(term)` runs before the first mailbox read.
- `handle_call` may return `(reply, Continue(term))` → caller gets `reply`, then `handle_continue(term)` runs before the next message.
- `handle_cast` / `handle_info` may return `Continue(term)` → `handle_continue(term)` runs next.

### Deferred init

```python
from fastactor.otp import GenServer, Continue

class Bootstrap(GenServer):
    async def init(self, db_url):
        self.db_url = db_url
        return Continue("load")             # init returns immediately; load happens next

    async def handle_continue(self, term):
        if term == "load":
            self.conn = await connect(self.db_url)
```

### Call with follow-up

```python
class Server(GenServer):
    async def handle_call(self, call: Call):
        reply = self._compute(call.message)
        return (reply, Continue(("log", reply)))   # reply delivered, then handle_continue

    async def handle_continue(self, term):
        match term:
            case ("log", reply):
                await self._log(reply)
```

---

## Named registration at `start()`

**Source:** [`runtime.py`](./runtime.py) (for `name=`), [`registry.py`](./registry.py) (for `via=`)

Two mutually-exclusive ways to make a process discoverable at construction time.

### `name=` — the global name map

```python
from fastactor.otp import whereis

g = await GenServer.start(name="sessions")
assert await whereis("sessions") is g

# A second start with the same live name raises:
#   Failed("already_started: sessions")
```

### `via=(registry_name, key)` — into a Registry

```python
from fastactor.otp import Registry, whereis

await Registry.new("sessions", mode="unique")
g = await GenServer.start(via=("sessions", user_id))
assert await whereis(("sessions", user_id)) is g

# Collisions in a unique registry raise AlreadyRegistered(pid).
```

Names and via-registrations both auto-free when the process terminates. Use `name=` for one-off singletons (one database client, one session manager). Use `via=` when you have many keyed instances (one session per user, one worker per tenant) — and `Registry` also supports `duplicate` mode for pub/sub fan-out.

Specifying both `name=` and `via=` in the same call raises `ValueError`.

---

## Messages reference

**Source:** [`_messages.py`](./_messages.py) · **Tests:** [`../../tests/test_message_metadata.py`](../../tests/test_message_metadata.py)

All messages carry `sender: Process | None` and an optional `metadata: dict` (kw-only). Pattern-match on the concrete type in callbacks.

```python
@dataclass
class Message:                          # abstract base
    sender: Process | None
    metadata: dict[str, Any] | None = None

@dataclass
class Info(Message):                    # generic async message
    message: Any

@dataclass
class Stop(Message):                    # graceful shutdown request; raises Shutdown internally
    reason: Any

@dataclass
class Exit(Message):                    # delivered when a linked process exits (trap_exits=True)
    reason: Any

@dataclass
class Down(Message):                    # delivered to monitors when the target terminates
    reason: Any

@dataclass
class Call(Message):                    # synchronous request, awaits a reply
    message: Any
    # set_result(value) / await result(timeout) — you don't usually touch these

@dataclass
class Cast(Message):                    # fire-and-forget request
    message: Any

class Ignore:                           # sentinel returned from init to skip the loop
    ...

@dataclass
class Continue:                         # sentinel for handle_continue scheduling
    term: Any
```

`handle_info` receives whatever you sent via `proc.send(...)` or `proc.info(...)` — usually an `Info(sender, payload)` if you used `info()`, or the raw object if you used `send()`. `Call` / `Cast` / `Stop` / `Exit` are intercepted before `handle_info` in `GenServer`.

---

## Exceptions

**Source:** [`_exceptions.py`](./_exceptions.py)

```python
class Crashed(Exception):
    reason: Any
    def __str__(self) -> str: ...       # str(self.reason)

class Failed(Exception):
    reason: Any                         # supervisor failures: already_started, max restart intensity, etc.
    def __str__(self) -> str: ...

class Shutdown(Exception):
    reason: Any                         # normal shutdown sentinel; wraps around reason
    def __str__(self) -> str: ...

class AlreadyRegistered(Exception):     # defined in registry.py, re-exported from otp
    pid: Process                        # raised by Registry.register in unique mode
```

`Crashed` wraps exceptions raised in `init` (preserving the original via `__cause__`); `Runtime.spawn` unwraps `__cause__` before re-raising so callers see the real error type. `is_normal_shutdown_reason(reason)` is the single source of truth for what counts as a normal exit.
