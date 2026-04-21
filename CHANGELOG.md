# Changelog

All notable changes to FastActor are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] — 2026-04-21

Initial public release. FastActor is an Elixir/OTP-inspired actor framework
for Python, built on [anyio](https://anyio.readthedocs.io/). It ports the
primitives that make OTP pleasant — processes, GenServers, supervisors,
registries, agents, tasks — to idiomatic Python `async` / `await`, with the
semantics you'd expect from Elixir.

### Added — Core actor model

- `Process` — abstract base actor.
  - Bounded anyio memory-object-stream mailbox (`send`, `send_nowait`,
    `info`).
  - Explicit lifecycle events: `_started` / `_stopped`, observable via
    `has_started()`, `await started()`, `has_stopped()`, `await stopped()`.
  - Bidirectional links (`link`, `unlink`) and unidirectional monitors
    (`monitor`, `demonitor`) with `links` / `monitors` / `monitored_by`
    set membership.
  - `trap_exits=True` converts inbound link exits from crashes into
    `handle_exit(Exit)` callbacks.
  - Callbacks: `init`, `handle_info`, `handle_exit`, `handle_continue`,
    `on_terminate`, `terminate`.
  - `init` return sentinels: `None`, `Ignore()`, `Stop(reason)`,
    `Continue(term)`.
  - `start()` / `start_link()` factory classmethods with shared kwargs
    (`trap_exits`, `supervisor`, `name`, `via`).
  - `stop(reason, timeout, sender)` and `kill()` teardown helpers.
  - Identity / hashing by ksuid string id (so equal ids compare equal —
    required for `links` / `monitors` sets to round-trip across restarts).

- `GenServer(Process)` — Elixir-style synchronous `call` + fire-and-forget
  `cast`.
  - `await server.call(request, sender=None, timeout=5)` — awaits a reply,
    raises `TimeoutError` on timeout, re-raises any exception thrown in
    `handle_call`.
  - `server.cast(request, sender=None)` — synchronous fire-and-forget.
  - Wrapped message convention: `handle_call(self, call: Call)` /
    `handle_cast(self, cast: Cast)` — unpack with `.message`.
  - Return contract for `handle_call`: `value` | `None` (deferred reply) |
    `(value, Continue(term))` | `(value, Stop(reason))`; raising both
    delivers to the caller and crashes the server.
  - Deferred-reply pattern: return `None`, stash the `Call`, later invoke
    `call.set_result(value)` (equivalent to Elixir's `{:noreply, state}` +
    `GenServer.reply/2`).

### Added — Supervision

- `Supervisor(Process)` — OTP supervisor.
  - Strategies: `one_for_one`, `one_for_all`, `rest_for_one`.
  - Restart types: `permanent` (always restart), `transient` (restart on
    abnormal exit only), `temporary` (never restart).
  - Shutdown types: integer timeout in seconds, `"brutal_kill"`,
    `"infinity"`.
  - Restart intensity caps via `max_restarts` / `max_seconds`; exceeding
    the window fails the supervisor and cascades to its own supervisor.
  - `ChildSpec` construction via `Supervisor.child_spec(id, factory,
    args=..., kwargs=..., restart=..., shutdown=..., type=...)`.
  - Management API mirroring OTP: `start_child`, `terminate_child`,
    `delete_child`, `restart_child`, `which_children`, `count_children`.
  - `start_child()` installs the spec, spawns the runner into the
    supervisor's own `TaskGroup`, and waits on a ready `Event` before
    returning.

- `DynamicSupervisor(Supervisor)` — `one_for_one`-only runtime-added
  children.
  - `max_children` cap.
  - `extra_arguments` prepended to every child's init tuple.
  - Auto-generated `dyn:…` child ids when `None` is passed.

### Added — Higher-level primitives

- `Agent` — tiny state-holder over `GenServer`.
  - `Agent.start(factory)` — factory returns initial state; may be sync or
    async.
  - Public API: `get(fn)`, `update(fn)`, `get_and_update(fn)`,
    `cast_update(fn)` — callbacks run inside the agent process and may be
    sync or async.

- `Task` / `TaskSupervisor` — one-shot awaitable coroutines.
  - `Task.start(fn, *args)` — unlinked; caller survives a task crash.
  - `Task.start_link(fn, *args)` — linked; caller dies on task crash.
  - `result = await task` — resolves to return value or re-raises.
  - `TaskSupervisor.run(fn, *args)` — `DynamicSupervisor`-backed shortcut
    for spawning supervised tasks.

- `Registry` — named process bag, separate from `Runtime.register_name`.
  - Modes: `unique` (one pid per key; duplicate register raises
    `AlreadyRegistered(pid)`) and `duplicate` (many pids per key,
    pub/sub-friendly).
  - API: `Registry.new(name, mode)`, `Registry.register(name, key, proc)`,
    `Registry.lookup`, `Registry.dispatch`, `Registry.keys`.
  - Entries auto-scrub when the registered process terminates.

### Added — Runtime

- `Runtime` — async context manager that owns the top-level task group,
  the root `RuntimeSupervisor` (with `trap_exits=True`), and the process
  registries.
  - Three equivalent entry points:
    1. `async with Runtime(): ...` — primary, idiomatic.
    2. `fastactor.run(main_fn, *, trap_signals=True, backend="asyncio")` —
       one-line app entry point. Returns `main_fn`'s value; absorbs
       signal-triggered cancellation.
    3. `Runtime.start()` / `Runtime.stop()` — explicit pair for REPLs and
       Jupyter, where `async with` is awkward.
  - SIGINT / SIGTERM trap installed by default; receiving a signal cancels
    the task group and triggers clean `__aexit__` shutdown. Opt out via
    `trap_signals=False`.
  - Singleton enforcement: only one `Runtime` may be active per process;
    a second entry raises `RuntimeError("Runtime already started")`.
  - `Runtime.current()` — reach the active runtime from any actor or
    runtime-scoped coroutine.
  - Named registration: `register_name` / `unregister_name` / `where_is`
    (sync, string-only) and the async `whereis(name_or_via)` — accepts
    both global names and `(registry, key)` tuples.
  - `spawn(process, *args, name=None, via=None, **kwargs)` — used by
    `Process.start` / `Process.start_link`; `name` and `via` are mutually
    exclusive.
  - Single-exception-group unwrap in `__aexit__` so callers see the
    original error, not anyio's `BaseExceptionGroup` wrapper.

### Added — Messages and sentinels

- Mailbox message types (`from fastactor.otp import ...`):
  `Info`, `Stop`, `Exit`, `Down`, `Call`, `Cast`.
- Return sentinels / control flow: `Continue(term)`, `Ignore`, `Shutdown`.
- Exceptions: `Crashed`, `Failed`, `AlreadyRegistered`.

### Added — Configuration

- `fastactor.settings` (pydantic-settings) exposes runtime defaults.
  - `mailbox_size` — default `1024`, overridable via
    `FASTACTOR_MAILBOX_SIZE`.

### Added — Project / packaging

- `pyproject.toml` — hatchling build backend, `src/`-layout wheel, MIT
  licence, Python `>=3.13`. Dependencies: `anyio>=4.13.0`, `pyee>=13.0.1`,
  `pydantic-settings>=2.14.0`, `sorcery>=0.2.2`, `svix-ksuid>=0.7.0`,
  `beartype>=0.22.9`.
- `uv` + `mise` for toolchain management; Python pinned to `3.13.7`.
- `uv.lock` checked in; old `requirements*.lock` files removed.
- `pyrightconfig.json` for IDE type-checking against `src/` and
  `src/tests/`.
- `justfile` with `test`, `debug`, `lint`, `fmt`, `repl` recipes; by
  default `test` skips `src/tests/e2e/` and passes extra args through as a
  `-k` keyword filter.

### Added — Tests

- 114 non-e2e tests across `src/tests/` (runs in under a second):
  `test_process`, `test_gen_server`, `test_agent`, `test_task`,
  `test_registry`, `test_links_and_monitors`,
  `test_runtime_entrypoints`, `test_supervisor_lifecycle`,
  `test_supervisor_strategies`, `test_dynamic_supervisor`.
- 14 e2e tests in `src/tests/e2e/` covering adaptive pools, chaos-monkey
  churn, compute/state split patterns, and racing-speculator workloads.
- Deterministic-sync testing discipline: no `asyncio.sleep` to wait for
  actor state. Patterns documented in `llms.txt` — `await proc.stopped()`,
  `await server.call(None)` to drain casts via FIFO, `await_child_restart`
  on an `anyio.Event`.
- `src/tests/conftest.py` forces the `asyncio` anyio backend and scopes a
  `Runtime` per test.

### Added — Documentation

- `README.md` — concept tour with runnable snippets and a pointer to
  `llms.txt` for agent-first consumers.
- `llms.txt` — complete API reference for coding agents: mental model,
  Elixir↔Python mapping, exact signatures for every public class and
  method, callback contracts, deterministic-sync testing patterns, and
  source-layout map.
- `.claude/CLAUDE.md` — repository guidance for Claude Code and other
  agents.

[Unreleased]: https://github.com/CyrusNuevoDia/fastactor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CyrusNuevoDia/fastactor/releases/tag/v0.1.0
