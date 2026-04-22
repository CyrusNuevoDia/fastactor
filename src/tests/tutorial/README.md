# Tutorial — The actor model for Python programmers who build AI agents

A 12-part progressive tutorial delivered as numbered pytest files. The target
reader is a Python programmer who has "heard about AI agents" but never
encountered the actor model. Every lesson ties an actor-model primitive to a
real AI-agent problem, and most of them contrast a "normal Python" approach
with the FastActor equivalent.

## How to read it

Open the files in order. Each one is self-contained: the module docstring
frames the problem, the tests encode the teaching, and a trailing comment
points to the next lesson. Run them with `pytest -v` and treat the test name
list as a table of contents.

```bash
uv run pytest src/tests/tutorial -v
```

> The project's usual shortcut `just test <name>` is a `-k` substring filter
> on test ids — it will not match a directory path. Use the full `uv run
> pytest src/tests/tutorial` form for this subdirectory.

Pair each lesson with the primitive's source and per-primitive README for
depth:

- `src/fastactor/otp/README.md` — reference docs for each primitive
- `llms.txt` — the Elixir ↔ Python mental-model table

## Reading order

| #  | File                                         | Concept                                               |
|----|----------------------------------------------|-------------------------------------------------------|
| 00 | `test_00_why_actors.py`                      | Why actors? Await-point races, mailbox serialization  |
| 01 | `test_01_your_first_actor.py`                | `Process` — the base loop, lifecycle, mailbox FIFO    |
| 02 | `test_02_call_vs_cast.py`                    | `GenServer` — `call` (RPC) and `cast` (fire-and-forget) |
| 03 | `test_03_agent_memory_with_agent.py`         | `Agent` — state without locks                         |
| 04 | `test_04_handle_continue_two_phase.py`       | `Continue` — two-phase work before the next message   |
| 05 | `test_05_crashes_are_isolated.py`            | Crash isolation vs. `asyncio.gather` cancellation     |
| 06 | `test_06_links_and_monitors.py`              | `monitor`, `link`, `trap_exits`                       |
| 07 | `test_07_supervisors_heal.py`                | `Supervisor` — restart policies and intensity limits  |
| 08 | `test_08_dynamic_supervisor_per_user.py`     | `DynamicSupervisor` — one agent per user session      |
| 09 | `test_09_parallel_tool_calls.py`             | `Task` and `TaskSupervisor` — parallel tool calls     |
| 10 | `test_10_registry_named_agents.py`           | `Registry` — name-based lookup with auto-cleanup      |
| 11 | `test_11_mini_multi_agent_system.py`         | Putting it all together — router + pool + registry   |

## Prerequisites

- Comfortable with `async` / `await` in Python 3.13+.
- Some intuition for `asyncio.Queue`, `asyncio.gather`, `asyncio.Lock`.
- No prior Elixir / Erlang / OTP exposure required.

## Conventions used here

These match the rest of the test suite — see `src/tests/README.md` for the
full guide.

- Every file sets `pytestmark = pytest.mark.anyio` at the module top.
- The `runtime` fixture in `src/tests/conftest.py` is autouse, so every test
  runs inside an active FastActor `Runtime` without ceremony.
- Shared test doubles (`EchoServer`, `CounterServer`, `BoomServer`,
  `MonitorServer`, `LinkServer`, `ContinueServer`, `await_child_restart`)
  live in `src/tests/otp/helpers.py`. Tutorial tests import them with
  `from helpers import ...` — both `src/tests/` and `src/tests/otp/` are on
  `sys.path`.
- No `asyncio.sleep` / `anyio.sleep` is used to wait for actor state. The
  standard deterministic patterns are `await proc.stopped()`, call-after-cast,
  and `MonitorServer.await_events(n)`.

## Intentionally out of scope

FastActor ships more than the tutorial covers. The omissions are deliberate —
learn the fundamentals here, graduate to the advanced primitives in
`src/tests/otp/`:

- **`GenStage`** — backpressure-aware producer/consumer streaming.
  See `src/tests/otp/test_gen_stage.py`.
- **`GenStateMachine`** — finite state machines with timeouts, postponed
  events, and hibernation. See `src/tests/otp/test_gen_state_machine.py`.
- **`GenEvent`** — pub/sub event manager. See
  `src/tests/otp/test_gen_event.py`.
- **Runtime entry points** — `fastactor.run(main)`, SIGINT/SIGTERM traps,
  REPL/Jupyter start/stop. See `src/tests/otp/test_runtime_entrypoints.py`.

## If you finish

- Rewrite a chatbot you've built as `GenServer` + `Agent` + `DynamicSupervisor`.
  Crash a worker on purpose. Watch supervision heal it.
- Read Joe Armstrong's thesis, *Making Reliable Distributed Systems in the
  Presence of Software Errors* (2003) — the original motivation for everything
  in this tutorial.
- Skim the advanced primitives above. GenStage in particular is what you'd
  reach for if "an LLM streams tokens and downstream processors consume them
  as a pipeline" is the shape of your problem.
