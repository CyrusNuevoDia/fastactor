# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

FastActor is an Elixir/OTP-inspired actor framework for Python, built on top of `anyio`. The runtime lives as a package at `src/fastactor/otp/` with one module per concept (`process.py`, `gen_server.py`, `agent.py`, `supervisor.py`, `dynamic_supervisor.py`, `task.py`, `registry.py`, `runtime.py`, plus private `_messages.py` and `_exceptions.py`). Public surface is re-exported from `src/fastactor/otp/__init__.py`.

## Environment

- Python is pinned via `mise.toml` (3.13.7). Dependencies are managed with `uv` (`uv.lock`, `pyproject.toml`); there is no `requirements*.lock` anymore.
- Package layout uses `src/fastactor` as the importable package; tests live at `src/tests` and import via `fastactor.*`.

## Common commands

- Install / sync deps: `uv sync`
- Run the full test suite: `uv run pytest src`
- Run a single test: `uv run pytest src/tests/test_gen_server.py::test_call_returns_echo_payload`
- Run tests matching a name: `uv run pytest src -k genserver`
- Tests are async; `src/tests/conftest.py` sets an autouse `anyio_backend` fixture that forces the `asyncio` backend, and each test module sets `pytestmark = pytest.mark.anyio`. When adding new test files, either add the same `pytestmark` or mark individual tests with `@pytest.mark.anyio`.

## Architecture

The design mirrors Erlang/OTP concepts layered on anyio primitives. Reading the files in dependency order (`_exceptions.py` → `_messages.py` → `process.py` → `gen_server.py` → `agent.py` → `supervisor.py` → `dynamic_supervisor.py` → `task.py` → `registry.py` → `runtime.py`) gives the full picture; the key pieces and their relationships:

- **`Process`** (`process.py`) — the base actor. Owns an anyio memory object stream mailbox (`_inbox` / `_mailbox`), `_started`/`_stopped` events for lifecycle, and sets for `links` / `monitors` / `monitored_by`. `loop()` calls `_init()` then drives `_loop()`, which reads messages and dispatches to `_handle_message()`. On exit, `terminate()` notifies monitors (`Down`) and links (`Exit` if `trap_exits`, otherwise cascades a crash via `stop`), then unregisters from the Runtime.
- **`GenServer(Process)`** (`gen_server.py`) — adds Elixir-style `call`/`cast`. `call()` sends a `Call` message whose `_ready` event is set by `_handle_message` once `handle_call` returns (or with the exception as the result). `cast()` is fire-and-forget via `Cast`.
- **Message types** (`_messages.py`) — `Message` is the abstract base; concrete variants (`Info`, `Stop`, `Exit`, `Down`, `Call`, `Cast`) all carry a `sender: Process`. `Ignore` and `Shutdown` are sentinels used by `init`/`_loop` control flow, not mailbox messages. `Continue` is a return value from `init`/`handle_call`/`handle_cast`/`handle_info` that triggers `handle_continue` before the next message.
- **`Supervisor(Process)`** (`supervisor.py`) — OTP supervisor with `one_for_one`, `one_for_all`, and `rest_for_one` strategies, `permanent` / `transient` / `temporary` restart semantics, and `max_restarts` / `max_seconds` restart intensity. Children are described by `ChildSpec` (constructed via `Supervisor.child_spec(...)`). Each child runs under `_run_child` (one_for_one) or `_supervise_once` (one_for_all/rest_for_one), spawned into the supervisor's own `TaskGroup`. `start_child()` installs the spec, spawns the runner, and waits on a ready `Event` before returning.
- **`DynamicSupervisor(Supervisor)`** (`dynamic_supervisor.py`) — `one_for_one`-only supervisor with `max_children` cap and `extra_arguments` prepended to each child's init args. Useful for pools of per-client workers.
- **`Agent`, `Task`, `TaskSupervisor`** (`agent.py`, `task.py`) — higher-level shorthands. `Agent` wraps state behind `get`/`update`/`get_and_update`/`cast_update`. `Task` runs a one-shot async function with an awaitable result. `TaskSupervisor` extends `DynamicSupervisor` with a `run(fn, *args)` shortcut.
- **`Registry`** (`registry.py`) — named process bag with `unique` or `duplicate` key modes. `Registry.register(name, key, proc)`, `Registry.lookup`, `Registry.dispatch`, `Registry.keys`. Auto-unregisters on process termination.
- **`Runtime`** (`runtime.py`) — async context manager that owns the top-level `TaskGroup`, a single `RuntimeSupervisor` (with `trap_exits=True`), and the process registries (`processes` by id, plus `registry` / `_reverse_registry` for named lookup via `where_is`, and `registries` for `Registry`). `Runtime.current()` returns the active runtime via a class-level singleton guarded by `Runtime._lock`; only one Runtime may be active at a time. `Process.start` / `Process.start_link` go through `Runtime.current().spawn(...)`, which calls `process.loop` inside the runtime's task group and waits for `started()` before returning.

### Important invariants to preserve when editing

- `Runtime._current` is a process-global singleton. Entering a second `Runtime()` while one is active raises. Tests rely on the `runtime` fixture in `src/tests/conftest.py` to scope this.
- A normal stop reason is one of `"normal"`, `"shutdown"`, or `Shutdown(...)`; `_is_normal_shutdown_reason` is the single source of truth — changing what counts as normal ripples through link-cascade behaviour, supervisor restart decisions, and crash-exception propagation.
- Mailbox capacity comes from `settings.mailbox_size` (overridable via the `FASTACTOR_MAILBOX_SIZE` env var through `pydantic-settings`). `send_nowait` will raise if the mailbox is full — callers that use it (e.g. `Process.stop`, `GenServer.call`/`cast`) are assuming enough headroom.
- `Process.__hash__` / `__eq__` are by `id` (a ksuid string from `utils.id_generator`). Two `Process` instances with the same id compare equal; link/monitor sets rely on this.
