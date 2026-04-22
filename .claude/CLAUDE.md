# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

FastActor is an Elixir/OTP-inspired actor framework for Python, built on top of `anyio`. The runtime lives as a package at `src/fastactor/otp/` with one module per concept (`process.py`, `gen_server.py`, `gen_event.py`, `gen_stage.py`, `gen_state_machine.py`, `agent.py`, `supervisor.py`, `dynamic_supervisor.py`, `task.py`, `registry.py`, `runtime.py`, plus private `_messages.py` and `_exceptions.py`). Public surface is re-exported from `src/fastactor/otp/__init__.py`. OpenTelemetry tracing lives separately at `src/fastactor/telemetry.py` and is gated behind the `otel` optional extra.

## Environment

- Python is pinned via `mise.toml` (3.13.7). Dependencies are managed with `uv` (`uv.lock`, `pyproject.toml`); there is no `requirements*.lock` anymore.
- Package layout uses `src/fastactor` as the importable package; tests live under `src/tests/` and import via `fastactor.*`.
- Optional extras: `pip install fastactor[otel]` pulls in `opentelemetry-api`/`sdk` for `fastactor.telemetry`.

## Common commands

Day-to-day, prefer the `just` recipes in `justfile`; they wrap `uv run` with the right defaults:

- Install / sync deps: `uv sync`
- Run the core suite (skips `src/tests/e2e/`): `just test` — or `uv run pytest src --ignore=src/tests/e2e`
- Run tests matching a name: `just test gen_server` (joins args into a `-k` filter)
- Run only the e2e suite: `just test e2e` (or `just test e2e chaos` to filter inside it)
- Run a single test: `uv run pytest src/tests/otp/test_gen_server.py::test_call_returns_echo_payload`
- Stop on first failure and drop into `ipdb`: `just debug [name]`
- Lint + type check: `just lint` (`ruff check src` + `ty check src`)
- Format + fix: `just fmt` (`ruff format` + `ruff check --fix`)
- `fastactor.otp.*` pre-imported REPL: `just repl`

Tests are split into three trees:

- `src/tests/otp/` — unit tests for every OTP module (plus `SPEC.md`, the source-of-truth behaviour spec used to drive development).
- `src/tests/tutorial/` — the 12-part actor-model walkthrough (`test_00_why_actors.py` … `test_11_mini_multi_agent_system.py`); each file is also the lesson, with a `README.md` for the narrative order.
- `src/tests/e2e/` — multi-agent system demos (chaos monkey, adaptive pool, consensus voting, hierarchical planning, racing speculators, compute/state split). Skipped by `just test`; run explicitly via `just test e2e`.
- `src/tests/test_telemetry.py` — covers the optional OTEL integration at the top level.

Tests are async. `src/tests/conftest.py` installs an autouse `anyio_backend` fixture that forces the `asyncio` backend and an autouse `_active_runtime` fixture that wraps every test in `async with Runtime()` (exposed as the `runtime` fixture); it also offers a `supervisor` fixture (`runtime.supervisor`) and a `make_supervisor` factory for isolated test supervisors. Each test module sets `pytestmark = pytest.mark.anyio`. When adding new test files, either add the same `pytestmark` or mark individual tests with `@pytest.mark.anyio`.

Shared test doubles — `EchoServer`, `CounterServer`, `BoomServer`, `SlowStopServer`, `MonitorServer`, `LinkServer`, `ContinueServer`, `OrderLog`, and the `await_child_restart` helper — live in `src/tests/otp/helpers.py`. Reuse these rather than re-rolling minimal `GenServer` subclasses in each test file. E2E helpers live separately in `src/tests/e2e/helpers.py`.

`conftest.py` prepends both `src/tests/` and `src/tests/otp/` to `sys.path` (with `otp/` winning), so unit tests, tutorial tests, and e2e tests can all `from helpers import ...` and reach the otp doubles. `pyproject.toml` mirrors this in `[tool.ty.environment].extra-paths` so the type checker resolves the same imports.

## Architecture

The design mirrors Erlang/OTP concepts layered on anyio primitives. Reading the files in dependency order (`_exceptions.py` → `_messages.py` → `process.py` → `gen_server.py` → `gen_event.py` / `gen_stage.py` / `gen_state_machine.py` → `agent.py` → `supervisor.py` → `dynamic_supervisor.py` → `task.py` → `registry.py` → `runtime.py`) gives the full picture; the key pieces and their relationships:

- **`Process`** (`process.py`) — the base actor. Owns an anyio memory object stream mailbox (`_inbox` / `_mailbox`), `_started`/`_stopped` events for lifecycle, and sets for `links` / `monitors` / `monitored_by`. `loop()` calls `_init()` then drives `_loop()`, which reads messages and dispatches to `_handle_message()`. On exit, `terminate()` notifies monitors (`Down`) and links (`Exit` if `trap_exits`, otherwise cascades a crash via `stop`), then unregisters from the Runtime.
- **`GenServer(Process)`** (`gen_server.py`) — adds Elixir-style `call`/`cast`. `call()` sends a `Call` message whose `_ready` event is set by `_handle_message` once `handle_call` returns (or with the exception as the result). `cast()` is fire-and-forget via `Cast`.
- **`GenEvent(Process)`** (`gen_event.py`) — event-manager process that multiplexes notifications to a dynamic set of `EventHandler`s (`add_handler` / `remove_handler` / `swap_handler` / `notify` / `sync_notify`). Handlers run in-process; exceptions surface as `HandlerNotInstalled` / `RemoveHandler` / `SwapHandler` control returns.
- **`GenStage(Process)`** (`gen_stage.py`) — back-pressured pipeline stage. `Producer` / `Consumer` / `ProducerConsumer` subclass it and pair with a dispatcher (`DemandDispatcher`, `BroadcastDispatcher`, `PartitionDispatcher`). Wire via `Subscribe` / `SubscribeAck` / `Demand` / `Events` / `Cancel` in `_messages.py`.
- **`GenStateMachine(Process)`** (`gen_state_machine.py`) — Erlang `gen_statem`-style FSM. Callbacks return control structs from the module (`Reply`, `NextEvent`, `Postpone`, `StateTimeout`, `Timeout`, `Hibernate`) to drive transitions, timers, and reply routing.
- **Message types** (`_messages.py`) — `Message` is the abstract base; concrete variants (`Info`, `Stop`, `Exit`, `Down`, `Call`, `Cast`, `Events`, `Demand`, `Subscribe`, `SubscribeAck`, `Cancel`) all carry a `sender: Process`. `Ignore` and `Shutdown` are sentinels used by `init`/`_loop` control flow, not mailbox messages. `Continue` is a return value from `init`/`handle_call`/`handle_cast`/`handle_info` that triggers `handle_continue` before the next message.
- **`Supervisor(Process)`** (`supervisor.py`) — OTP supervisor with `one_for_one`, `one_for_all`, and `rest_for_one` strategies, `permanent` / `transient` / `temporary` restart semantics, and `max_restarts` / `max_seconds` restart intensity. Children are described by `ChildSpec` (constructed via `Supervisor.child_spec(...)`); `RunningChild` / `ShutdownType` / `RestartType` / `RestartStrategy` are also exported. Each child runs under `_run_child` (one_for_one) or `_supervise_once` (one_for_all/rest_for_one), spawned into the supervisor's own `TaskGroup`. `start_child()` installs the spec, spawns the runner, and waits on a ready `Event` before returning.
- **`DynamicSupervisor(Supervisor)`** (`dynamic_supervisor.py`) — `one_for_one`-only supervisor with `max_children` cap and `extra_arguments` prepended to each child's init args. Useful for pools of per-client workers.
- **`Agent`, `Task`, `TaskSupervisor`** (`agent.py`, `task.py`) — higher-level shorthands. `Agent` wraps state behind `get`/`update`/`get_and_update`/`cast_update`. `Task` runs a one-shot async function with an awaitable result. `TaskSupervisor` extends `DynamicSupervisor` with a `run(fn, *args)` shortcut.
- **`Registry`** (`registry.py`) — named process bag with `unique` or `duplicate` key modes. `Registry.register(name, key, proc)`, `Registry.lookup`, `Registry.dispatch`, `Registry.keys`. Auto-unregisters on process termination.
- **`Runtime`** (`runtime.py`) — async context manager that owns the top-level `TaskGroup`, a single `RuntimeSupervisor` (with `trap_exits=True`), and the process registries (`processes` by id, plus `registry` / `_reverse_registry` for named lookup via the `whereis` top-level helper, and `registries` for `Registry`). `Runtime.current()` returns the active runtime via a class-level singleton guarded by `Runtime._lock`; only one Runtime may be active at a time. Crash history is exposed as `CrashRecord`s. `Process.start` / `Process.start_link` go through `Runtime.current().spawn(...)`, which calls `process.loop` inside the runtime's task group and waits for `started()` before returning. There are three equivalent entry points: `async with Runtime(): ...` (tests, library code), `fastactor.run(main)` in `src/fastactor/__init__.py` (one-shot app entry — wraps `anyio.run` + context manager + clean-Ctrl-C), and `await Runtime.start()` / `await rt.stop()` (REPL/Jupyter). All three install `SIGINT`/`SIGTERM` traps by default (`trap_signals=False` to opt out).
- **`telemetry`** (`src/fastactor/telemetry.py`) — optional OpenTelemetry integration, enabled via `FASTACTOR_TELEMETRY_ENABLED=1` (see `settings.py`). Import is lazy: no-op unless the `[otel]` extra is installed.

### Important invariants to preserve when editing

- `Runtime._current` is a process-global singleton. Entering a second `Runtime()` while one is active raises. Tests rely on the `runtime` fixture in `src/tests/conftest.py` to scope this.
- A normal stop reason is one of `"normal"`, `"shutdown"`, or `Shutdown(...)`; `is_normal_shutdown_reason` is the single source of truth — changing what counts as normal ripples through link-cascade behaviour, supervisor restart decisions, and crash-exception propagation.
- Mailbox capacity comes from `settings.mailbox_size` (overridable via the `FASTACTOR_MAILBOX_SIZE` env var through `pydantic-settings`). `send_nowait` will raise if the mailbox is full — callers that use it (e.g. `Process.stop`, `GenServer.call`/`cast`) are assuming enough headroom.
- `Process.__hash__` / `__eq__` are by `id` (a ksuid string from `utils.id_generator`). Two `Process` instances with the same id compare equal; link/monitor sets rely on this.
