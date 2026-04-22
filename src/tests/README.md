# `src/tests` — Testing patterns

The canonical shapes for testing fastactor code. The non-e2e suite (~114 tests) runs in under a second; it gets there by never using `asyncio.sleep` to wait for actor state.

For the primitive API docs, see [`../fastactor/otp/README.md`](../fastactor/otp/README.md). For orientation, see [`../../llms.txt`](../../llms.txt).

---

## Running tests

```bash
just test                   # full non-e2e suite
just test gen_server        # substring filter via -k
just test e2e               # e2e suite only
just debug <name>           # stop on first failure, drop into ipdb
```

Or directly:

```bash
uv run pytest src --ignore=src/tests/e2e
uv run pytest src/tests/test_gen_server.py::test_call_returns_echo_payload
```

---

## File boilerplate

Every test module is async. At the top:

```python
import pytest
pytestmark = pytest.mark.anyio
```

Individual tests can also be marked with `@pytest.mark.anyio` — but the module-level `pytestmark` is the convention.

---

## Fixtures

Defined in [`./conftest.py`](./conftest.py):

| Fixture            | Scope    | What it gives you                                                                 |
| ------------------ | -------- | --------------------------------------------------------------------------------- |
| `anyio_backend`    | autouse  | Forces the `asyncio` backend.                                                     |
| `runtime`          | function | A fresh `Runtime` entered via `async with`. Always one active.                    |
| `_active_runtime`  | autouse  | Wraps every test in the runtime fixture — you get the Runtime for free.           |
| `supervisor`       | function | `runtime.supervisor` — the pre-installed `RuntimeSupervisor`.                     |
| `make_supervisor`  | function | Factory: `await make_supervisor(strategy=..., max_restarts=..., max_seconds=...)` |

`make_supervisor` tracks every supervisor it creates and tears them down in reverse order during fixture teardown — use it whenever a test needs its own supervisor with non-default settings.

```python
async def test_rest_for_one_restarts_downstream(make_supervisor):
    sup = await make_supervisor(strategy="rest_for_one")
    ...
```

---

## Shared doubles

Reusable `GenServer` subclasses live in [`./otp/helpers.py`](./otp/helpers.py). Import with `from helpers import ...` — `conftest.py` inserts both `src/tests/` and `src/tests/otp/` onto `sys.path`.

| Double             | Purpose                                                                          |
| ------------------ | -------------------------------------------------------------------------------- |
| `EchoServer`       | Logs casts, echoes calls. Simplest working `GenServer`.                          |
| `CounterServer`    | Adds/subtracts/multiplies/resets via casts; `get` / `sync` via calls.            |
| `BoomServer`       | Crashes on `call("boom")`; `init(on_init=True)` crashes during init.             |
| `SlowStopServer`   | Sleeps ~50ms in `terminate`. For shutdown-timeout / `brutal_kill` tests.         |
| `MonitorServer`    | Appends `Down` messages; `await_events(n)` blocks until `n` received.            |
| `LinkServer`       | Trap-exits variant of MonitorServer; collects `Exit` messages.                   |
| `ContinueServer`   | Records `init` → `handle_continue` → `handle_call` order.                        |
| `OrderLog`         | Thread-safe global event log for cross-actor ordering assertions.                |
| `await_child_restart(sup, cid, old_proc, timeout=2)` | Blocks until the supervisor swaps `cid` to a new process (identity-compared against `old_proc`). |

Reuse these rather than rolling minimal `GenServer` subclasses in every test file. E2E helpers live separately in [`./e2e/helpers.py`](./e2e/helpers.py).

---

## Deterministic synchronization

**Never** use `asyncio.sleep` to wait for actor state. Use these patterns instead:

### 1. Call round-trip after casts

A successful `call` reply proves every prior `cast` has already been handled — the mailbox is FIFO.

```python
server.cast(("set", key, value))
await server.call("sync")           # round-trip forces the prior cast to finish
```

Make sure the server returns a non-`None` reply for your sync key — the default base-class `handle_call` returns `None`, which under the GenServer contract means NoReply and will hang. `CounterServer.handle_call("sync")` exists precisely for this.

### 2. `await proc.stopped()` + inspect `_crash_exc`

```python
proc = await Worker.start()
proc.send_nowait("boom")              # hypothetical: triggers a crash in handle_info
await proc.stopped()
assert isinstance(proc._crash_exc, Crashed)
assert "boom" in str(proc._crash_exc)
```

### 3. `MonitorServer.await_events(n)` for monitor fan-out

```python
monitor = await MonitorServer.start()
monitor.monitor(target)
await target.stop("normal")
downs = await monitor.await_events(1)
assert downs[0].reason == "normal"
```

`LinkServer.await_events(n)` has the same shape for `Exit` fanout under `trap_exits=True`.

### 4. `await_child_restart` for supervisor restarts

```python
old = await sup.start_child(spec)
old.send_nowait(("crash", RuntimeError("die")))
await old.stopped()
new = await await_child_restart(sup, "worker", old_proc=old)
assert new is not old
```

---

## The one intentional `sleep`

There is exactly one `sleep(...)` in the full suite — in `test_max_restarts_window_resets_after_max_seconds`, which genuinely needs a real time gap to observe the restart window resetting. It's annotated inline. Everything else is event-driven.
