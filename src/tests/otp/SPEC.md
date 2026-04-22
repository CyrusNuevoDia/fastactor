# OTP Conformance Spec — Python/anyio Port

> **Purpose**: This document specifies the observable behaviours a Python/anyio re-implementation of Erlang/OTP must match. It is structured so a coding agent can generate a pytest suite directly from its numbered claims.
>
> **How to use this as an agent**: For each numbered claim (§X.Y), generate exactly one `pytest` test named `test_X_Y_<slug>`. Every test must (1) cite the claim number in a docstring and (2) link the spec source it verifies. If a claim cannot be tested without additional information, leave a `# SPEC-GAP(X.Y): ...` comment rather than guessing.

---

## 0. Ground truth

When sources disagree — and they occasionally do on edge cases — use this priority order:

1. **OTP source code** (the `.erl` files in `lib/stdlib/src/`) — linked per-section below
2. **Official documentation** at <https://www.erlang.org/doc>
3. **EEPs** at <https://github.com/erlang/eep>
4. **Books**: Cesarini & Vinoski _Designing for Scalability with Erlang/OTP_; Hébert _Learn You Some Erlang_; Armstrong _Programming Erlang_

### Top-level references

| Resource                     | URL                                                          |
| ---------------------------- | ------------------------------------------------------------ |
| Docs root                    | <https://www.erlang.org/doc>                                 |
| OTP GitHub                   | <https://github.com/erlang/otp>                              |
| `stdlib` source dir          | <https://github.com/erlang/otp/tree/master/lib/stdlib/src>   |
| Design principles            | <https://www.erlang.org/doc/system/design_principles.html>   |
| Reference manual (processes) | <https://www.erlang.org/doc/system/ref_man_processes.html>   |
| `receive` expression         | <https://www.erlang.org/doc/system/expressions.html>         |
| EEP index                    | <https://github.com/erlang/eep/blob/master/eeps/eep-0000.md> |

> **Pin your links**: When citing OTP source in tests, use GitHub commit-pinned permalinks (press `y` on a file view) so a future refactor in OTP doesn't invalidate your references.

---

## 1. Testing conventions

All tests should follow these rules:

- Use `pytest.mark.anyio` for async tests; configure `anyio_backend` to run against both `asyncio` and `trio` if portability matters.
- Use `anyio.fail_after` for timeout assertions; never use `time.sleep`.
- Every test begins with a docstring of the form:

  ```python
  """SPEC §X.Y: <claim name>.

  Source: <doc or github URL>
  """
  ```

- Tests assert on **observable behaviour only** — message flow, return values, timing, exit reasons. Never assert on internal state representation.
- Use small fixture processes (`EchoServer`, `CrashServer`, `SlowServer`) rather than bespoke setups per test.
- For timing-sensitive tests, allow a 50ms tolerance unless the claim explicitly concerns timing precision.

---

## 2. Process primitives

**Docs**: <https://www.erlang.org/doc/system/ref_man_processes.html>
**Module reference** (BIFs): <https://www.erlang.org/doc/apps/erts/erlang.html>

### 2.1 Spawn returns a live process identifier

**Claim**: `spawn(fun)` returns a pid that is alive immediately after the call returns. The spawned function runs concurrently.
**Source**: <https://www.erlang.org/doc/system/ref_man_processes.html>
**Test**: Spawn a function that blocks forever; assert the spawn call returns within 10ms and the pid passes an `is_alive` check.

### 2.2 Normal exit does not propagate across links

**Claim**: When A is linked to B and B exits with reason `normal`, A continues running (A receives no exit signal unless trapping exits).
**Source**: <https://www.erlang.org/doc/system/ref_man_processes.html> §"Error Handling"
**Test**: Link A and B. B exits normally. Assert A is still alive after 100ms.

### 2.3 Abnormal exit propagates across links bidirectionally

**Claim**: If A is linked to B and either exits with a non-`normal` reason, the other also exits with the same reason (unless trapping exits).
**Source**: <https://www.erlang.org/doc/system/ref_man_processes.html> §"Links"
**Tests**:

- Link A and B. B raises. Assert A exits with the same reason.
- Link A and B. A raises. Assert B exits with the same reason.

### 2.4 `trap_exit` converts exit signals to mailbox messages

**Claim**: With `process_flag(trap_exit, true)` set, incoming exit signals (except `kill`) become `{'EXIT', FromPid, Reason}` messages rather than terminating the process.
**Source**: <https://www.erlang.org/doc/apps/erts/erlang.html#process_flag/2>
**Tests**:

- Trap exits on A. Link A and B. B raises with reason `oops`. Assert A receives `('EXIT', B_pid, 'oops')`.
- Trap exits on A. Link A and B. B exits normally. Assert A receives `('EXIT', B_pid, 'normal')` (trapping converts _all_ exit signals, including normal, when linked).

### 2.5 `kill` is untrappable; recipient receives `killed`

**Claim**: An exit signal with reason `kill` always terminates the recipient, regardless of `trap_exit`. However, a linked third party that is _trapping_ receives the reason as `killed` (not `kill`). This asymmetry is important.
**Source**: <https://www.erlang.org/doc/apps/erts/erlang.html#exit/2>
**Tests**:

- Trap exits on A. Send `exit(A, kill)`. Assert A terminates regardless.
- A links B, A traps exits. `exit(B, kill)`. Assert A receives `('EXIT', B_pid, 'killed')` — note `killed`, not `kill`.

### 2.6 Monitor is asymmetric and one-shot

**Claim**: `monitor(process, Pid)` returns a unique ref. When Pid dies, the monitoring process receives exactly one `('DOWN', Ref, 'process', Pid, Reason)` message. Monitoring a non-existent pid yields immediate DOWN with reason `noproc`.
**Source**: <https://www.erlang.org/doc/apps/erts/erlang.html#monitor/2>
**Tests**:

- Monitor a live pid, let it exit normally, assert DOWN with reason `normal`.
- Monitor an already-dead pid, assert immediate DOWN with reason `noproc`.
- Monitor same pid twice, assert two distinct refs and two DOWN messages on exit.
- Monitor then `demonitor(ref, [flush])`, let pid exit, assert no DOWN in mailbox.

### 2.7 Unlink is symmetric

**Claim**: After `unlink(B)` from A, neither party receives exit signals from the other.
**Source**: <https://www.erlang.org/doc/apps/erts/erlang.html#unlink/1>
**Test**: Link A-B, unlink, B raises. Assert A is alive.

---

## 3. Mailbox semantics

**Docs**: <https://www.erlang.org/doc/system/ref_man_processes.html#message-sending>
**`receive` expression**: <https://www.erlang.org/doc/system/expressions.html>

### 3.1 FIFO ordering per sender-receiver pair

**Claim**: Messages sent from a single sender S to a single receiver R arrive in the order S sent them. No ordering guarantee exists across different senders.
**Source**: <https://www.erlang.org/doc/system/ref_man_processes.html>
**Tests**:

- S sends `[1, 2, 3, 4, 5]` to R. R receives all 5; assert strict order `[1,2,3,4,5]`.
- S1 sends `[a, b]` and S2 sends `[x, y]` concurrently to R. Assert `a` precedes `b` in R's mailbox and `x` precedes `y`, but make no assertion about `a` vs `x` ordering.

### 3.2 Selective receive leaves non-matching messages in place

**Claim**: `receive Pattern -> ... end` scans the mailbox from oldest to newest. Messages not matching the pattern are left at their original position. Subsequent receives see them again in original order.
**Source**: <https://www.erlang.org/doc/system/expressions.html> §"Receive"
**Test**: Send `[foo, bar, baz, bar, quux]`. Selectively receive one `bar` with a pattern match. Then drain remaining mailbox. Assert drain order is `[foo, baz, bar, quux]` (first `bar` removed, others untouched).

### 3.3 `receive` with timeout returns after timeout if no match

**Claim**: `receive Pattern -> ... after T -> Fallback end` waits up to T ms; if no matching message arrives, the `after` branch runs. An `after 0` branch runs immediately if nothing matches.
**Source**: <https://www.erlang.org/doc/system/expressions.html> §"Receive"
**Tests**:

- Empty mailbox, `receive foo -> ... after 100 -> timeout`. Assert returns `timeout` within 100–150ms.
- Empty mailbox, `receive foo -> ... after 0 -> timeout`. Assert returns `timeout` within 10ms.
- Mailbox contains `bar`; `receive foo -> ... after 100 -> timeout`. Assert returns `timeout`; `bar` still in mailbox.

### 3.4 Self-send

**Claim**: A process can send to itself. The message is enqueued to its own mailbox and can be received later.
**Source**: <https://www.erlang.org/doc/apps/erts/erlang.html#send/2>
**Test**: Process sends `ping` to self, then receives; assert `ping` is received.

---

## 4. `gen_server`

**Module reference**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html>
**Concepts guide**: <https://www.erlang.org/doc/system/gen_server_concepts.html>
**Source**: <https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen_server.erl>
**Shared plumbing (critical)**: <https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen.erl>

> **Read `gen.erl` before testing `gen_server:call`** — the monitor-then-send-then-selective-receive pattern that makes `call` robust against crashed callees is implemented there, not in `gen_server.erl`.

### Callback contract

Required callbacks: `init/1`, `handle_call/3`, `handle_cast/2`, `handle_info/2`, `terminate/2`, `code_change/3`.
Optional: `handle_continue/2`, `format_status/1`.

### 4.1 `init/1` return shapes

**Claim**: `init/1` may return any of:

- `{ok, State}`
- `{ok, State, Timeout}` — integer ms or `infinity`
- `{ok, State, hibernate}`
- `{ok, State, {continue, Term}}` — `handle_continue(Term, State)` called before processing mailbox
- `{stop, Reason}` — process exits; `start_link` caller receives `{error, Reason}`
- `ignore` — `start_link` caller receives `ignore`, no process remains

**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:init/1>
**Tests**: One test per return shape, asserting the observable outcome for each.

### 4.2 `handle_call/3` return shapes

**Claim**: `handle_call/3` may return:

- `{reply, Reply, NewState}`
- `{reply, Reply, NewState, Timeout | hibernate | {continue, C}}`
- `{noreply, NewState}` — caller stays blocked until explicit `gen_server:reply/2`
- `{noreply, NewState, Timeout | hibernate | {continue, C}}`
- `{stop, Reason, Reply, NewState}` — reply sent, then terminate
- `{stop, Reason, NewState}` — no reply, terminate (caller sees DOWN / timeout)

**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_call/3>
**Tests**: One per shape. For `noreply`, spawn a helper that `reply`s after a delay and assert caller unblocks correctly.

### 4.3 `call` uses monitor-based rendezvous

**Claim**: `gen_server:call(Server, Request, Timeout)` must:

1. Monitor the server pid
2. Send the request tagged with the monitor ref
3. Selectively receive _only_ the matching reply or a DOWN for the ref
4. On timeout, demonitor and raise `{timeout, {gen_server, call, [Server, Request, Timeout]}}`
5. On DOWN before reply, raise `{noproc, ...}` or the exit reason

**Source**: `do_call` in <https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen.erl>
**Tests**:

- Call a server that replies normally; assert reply received.
- Call a server whose `handle_call` raises; assert caller raises the same reason, not a timeout.
- Call a non-existent server; assert caller raises `noproc`.
- Call a slow server with timeout=50ms; assert caller raises `timeout` and server is still alive afterward (not killed by the timeout).

### 4.4 `cast` is fire-and-forget

**Claim**: `gen_server:cast(Server, Msg)` always returns `ok` immediately, even if Server doesn't exist. No confirmation of delivery.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#cast/2>
**Tests**:

- Cast to live server; assert immediate return and `handle_cast` eventually invoked.
- Cast to dead pid; assert `ok` returned, no exception.

### 4.5 `handle_info` receives non-OTP messages

**Claim**: Any message sent to a gen_server that is not a `call`, `cast`, or system message is delivered to `handle_info/2`. This includes monitor DOWN messages, trapped exit signals, and raw sends.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_info/2>
**Test**: Raw-send `{arbitrary, 42}` to a gen_server; assert `handle_info({arbitrary, 42}, State)` is invoked.

### 4.6 `handle_continue` runs before next mailbox message

**Claim**: Any callback returning `{..., {continue, Term}}` causes `handle_continue(Term, State)` to run _before_ the server processes any further mailbox messages. Continues may chain (a continue returning another continue).
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:handle_continue/2>; see also `loop/4` in `gen_server.erl`
**Test**: `init` returns `{ok, s0, {continue, step1}}`; `handle_continue(step1, _)` returns `{noreply, s1, {continue, step2}}`. Send a cast _before_ init returns (race-free via start_link). Assert `handle_continue` for `step1` runs, then `step2`, then the cast is processed.

### 4.7 Timeout message after idle period

**Claim**: When a callback returns `{..., Timeout}` with an integer, if no message arrives in the mailbox within `Timeout` ms, `handle_info(timeout, State)` is invoked.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html> §"Module:handle*info/2"
**Test**: Server returns `{noreply, State, 100}`. No messages sent. Assert `handle_info(timeout, *)` is called ~100ms later.

### 4.8 `terminate/2` called on shutdown from supervisor

**Claim**: If the gen_server is trapping exits AND the supervisor's shutdown strategy is a timeout (not `brutal_kill`), `terminate(shutdown, State)` is called on stop. If not trapping exits, the process dies without `terminate` being called.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#Module:terminate/2>
**Tests**:

- Trap exits, supervisor stops with shutdown=5000. Assert `terminate(shutdown, _)` observed.
- Don't trap exits, supervisor stops. Assert `terminate` not called.

### 4.9 `reply/2` for decoupled replies

**Claim**: `gen_server:reply(From, Reply)` sends a reply to a caller that was left hanging by a `noreply` return. `From` is the 2nd argument of `handle_call`.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#reply/2>
**Test**: Server's `handle_call` stashes `From` and returns `{noreply, State}`. Caller blocks. A later event triggers `reply(From, 42)`. Assert caller unblocks with `42`.

---

## 5. `gen_statem`

**Module reference**: <https://www.erlang.org/doc/apps/stdlib/gen_statem.html>
**Concepts guide**: <https://www.erlang.org/doc/system/statem.html>
**Source**: <https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen_statem.erl>

### 5.1 Two callback modes

**Claim**: `callback_mode/0` returns either `state_functions` (one callback per state, state must be an atom) or `handle_event_function` (single `handle_event/4` callback). The mode may also be a list with `state_enter`, enabling state-entry callbacks.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_statem.html#Module:callback_mode/0>
**Tests**:

- `state_functions` mode: events in state `idle` invoke `idle(EventType, Event, Data)`.
- `handle_event_function` mode: all events invoke `handle_event(EventType, Event, State, Data)`.
- With `state_enter`: on transition to new state, callback is invoked with `(enter, OldState, Data)` _before_ any event in the new state.

### 5.2 Event types

**Claim**: Events carry a type: `{call, From}`, `cast`, `info`, `state_timeout`, `{timeout, Name}`, `timeout`, `internal`.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_statem.html#type-event_type>
**Test**: One test per event type, asserting the correct type is passed to the callback.

### 5.3 Actions

**Claim**: Callback return may include a list of actions. Key actions:

- `{reply, From, Reply}` — reply to a `call`
- `{next_event, EventType, Event}` — inject an event processed before mailbox messages
- `postpone` — re-enqueue the current event for re-processing after next state change
- `{state_timeout, Time, EventContent}` — cancelled on any state change
- `{{timeout, Name}, Time, EventContent}` — named generic timeout
- `{timeout, Time, EventContent}` — anonymous generic timeout
- `hibernate`

**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_statem.html#type-action>
**Tests**: One test per action type.

### 5.4 Postponed events are replayed on state change

**Claim**: An event handled with `postpone` is re-inserted at the front of the event queue and re-processed _after_ the next state transition. It is re-processed in original order relative to other postponed events.
**Source**: <https://www.erlang.org/doc/system/statem.html> §"Postponing Events"; source: `loop_state_callback` in `gen_statem.erl`
**Test**: In state `locked`, postpone an `unlock` event. Transition to state `unlockable`. Assert `unlock` is re-delivered and now matches.

### 5.5 State timeout cancelled on state change

**Claim**: A `state_timeout` is automatically cancelled if the state changes before it fires. A `{timeout, Name}` is _not_ cancelled on state change (only on explicit cancellation or another timeout with the same name).
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_statem.html> §"State timeout" vs "Generic timeouts"
**Tests**:

- Set state_timeout 100ms in state A. Transition to state B at 50ms. Assert no timeout event.
- Set `{timeout, heartbeat}` 100ms in state A. Transition to state B at 50ms. Assert timeout event fires at ~100ms.

### 5.6 `{next_event, ...}` processed before mailbox

**Claim**: Events scheduled via `next_event` action are processed before any pending messages in the mailbox, in the order of the action list.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_statem.html#type-action>
**Test**: Send external event X. In handler, return actions `[{next_event, internal, a}, {next_event, internal, b}]`. Also send external event Y immediately after. Assert processing order: a, b, Y.

---

## 6. `supervisor`

**Module reference**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html>
**Concepts guide**: <https://www.erlang.org/doc/system/sup_princ.html>
**Source**: <https://github.com/erlang/otp/blob/master/lib/stdlib/src/supervisor.erl>

### 6.1 Supervisor init returns `{ok, {SupFlags, ChildSpecs}}`

**Claim**: `init/1` must return `{ok, {SupFlags, [ChildSpec]}}` or `ignore`. `SupFlags` is a map `#{strategy => atom, intensity => int, period => int, auto_shutdown => atom}` with defaults `one_for_one`, 1, 5, never.
**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html#Module:init/1>
**Tests**: Verify default values, verify all four strategies, verify malformed specs raise on startup.

### 6.2 Child spec shape

**Claim**: Modern child spec is a map:

```
#{id => term,              %% mandatory
  start => {M, F, A},      %% mandatory
  restart => permanent | transient | temporary,   %% default: permanent
  shutdown => brutal_kill | integer | infinity,   %% default: 5000 (worker), infinity (supervisor)
  type => worker | supervisor,                    %% default: worker
  modules => [Module] | dynamic                   %% default: [M]
}
```

**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-child_spec>
**Tests**: Verify defaults applied, verify duplicate ids rejected.

### 6.3 Restart types

**Claim**:

- `permanent`: always restarted.
- `transient`: restarted only if exit reason is _not_ `normal`, `shutdown`, or `{shutdown, _}`.
- `temporary`: never restarted.

**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-restart>
**Tests**: 3 × 3 matrix (restart_type × exit_reason_category). Assert restart occurs iff spec demands it.

### 6.4 Restart strategies

**Claim**:

- `one_for_one`: only the failed child is restarted.
- `one_for_all`: all children are terminated (in reverse start order) then restarted (in start order).
- `rest_for_one`: children started _after_ the failed child are terminated (reverse order) and restarted (forward order); earlier children untouched.
- `simple_one_for_one` (deprecated in favour of `DynamicSupervisor` in Elixir, but still in OTP): single template, dynamic children.

**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-strategy>
**Tests**: 4-child supervisor, kill child #2, verify restart set and order per strategy.

### 6.5 Restart intensity window

**Claim**: If more than `intensity` restarts occur within `period` seconds, the supervisor itself terminates with reason `shutdown`. The window is a sliding count of restart _timestamps_, not a rolling rate.
**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html> §"Tuning"; `add_restart/1` in `supervisor.erl`
**Tests**:

- Intensity 3, period 5. Crash child 3 times in 1s. Assert supervisor alive.
- Intensity 3, period 5. Crash child 4 times in 1s. Assert supervisor exits with reason `shutdown`.
- Intensity 3, period 5. Crash child 3 times, wait 6s, crash 3 more times. Assert supervisor alive (window slid).

### 6.6 Shutdown semantics

**Claim**: When a supervisor terminates a child:

- `brutal_kill`: send `exit(Pid, kill)` immediately.
- Integer ms: send `exit(Pid, shutdown)`; if alive after T ms, send `exit(Pid, kill)`.
- `infinity`: send `exit(Pid, shutdown)` and wait forever (only valid for supervisor-type children normally).

**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html#type-shutdown>
**Tests**:

- Child traps exits but exits on `shutdown`. Timeout=1000. Assert terminate called, child gone in <1s.
- Child traps exits and ignores `shutdown`. Timeout=500. Assert child killed after 500ms.
- `brutal_kill`: child traps exits. Assert no `terminate` observed, child gone immediately.

### 6.7 Shutdown order is reverse of start order

**Claim**: Children are terminated in the reverse order they were started (for strategies where multiple children are shut down).
**Source**: <https://www.erlang.org/doc/apps/stdlib/supervisor.html> §"Shutdown"; `terminate_children/2` in `supervisor.erl`
**Test**: 3 children A, B, C. Trigger supervisor shutdown; each child logs termination. Assert log order: C, B, A.

---

## 7. `gen_event`

**Module reference**: <https://www.erlang.org/doc/apps/stdlib/gen_event.html>
**Source**: <https://github.com/erlang/otp/blob/master/lib/stdlib/src/gen_event.erl>

### 7.1 Event manager hosts multiple handlers

**Claim**: A gen_event process hosts zero or more handler modules. Each handler has its own state. Events are dispatched to every handler.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_event.html>
**Test**: Add 3 handlers, notify an event, assert all 3 invoked with the event.

### 7.2 Handler add/delete/swap

**Claim**:

- `add_handler(Mgr, Handler, Args)` → calls `Handler:init(Args)`.
- `delete_handler(Mgr, Handler, Args)` → calls `Handler:terminate(Args, State)`.
- `swap_handler(Mgr, {Old, OldArgs}, {New, NewArgs})` → calls `Old:terminate`, then `New:init({NewArgs, TerminateResult})`.

**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_event.html#add_handler/3>
**Tests**: One test per lifecycle transition, asserting correct callback sequence.

### 7.3 Crashed handler is removed, not the manager

**Claim**: If a handler's callback raises, that handler is removed (the `gen_event` manager calls a registered error handler, if any, then drops it). The manager process itself stays alive and continues serving other handlers.
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_event.html> §"Error handling"
**Test**: Add 2 handlers. Handler 1 raises on next event. Send event. Assert handler 1 is removed, handler 2 still installed and received the event.

---

## 8. Registered names

**Docs**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#start_link/3> (see `ServerName` arg)

### 8.1 Local registration

**Claim**: `{local, Name}` registers the process under an atom in the local node's registry. A second registration with the same name fails with `{already_started, Pid}`.
**Source**: <https://www.erlang.org/doc/apps/erts/erlang.html#register/2>
**Tests**:

- Register A as `myserver`; send by name; A receives.
- Attempt to register B as `myserver`; assert `{already_started, A_pid}`.
- A exits; registry entry removed; fresh `myserver` registration succeeds.

### 8.2 `via` registration

**Claim**: `{via, Module, Term}` delegates registration to `Module:register_name/2`, `Module:unregister_name/1`, `Module:whereis_name/1`, `Module:send/2`. Conformant ports should ship at least one `via`-compatible registry (e.g. an in-memory global registry).
**Source**: <https://www.erlang.org/doc/apps/stdlib/gen_server.html#start_link/4>
**Test**: Implement a stub via-module with a dict-backed registry; verify register/send/whereis/unregister all routed correctly.

---

## 9. `sys` protocol

**Module reference**: <https://www.erlang.org/doc/apps/stdlib/sys.html>
**Source**: <https://github.com/erlang/otp/blob/master/lib/stdlib/src/sys.erl>

> **Why this matters**: Every OTP-compliant process must respond to system messages _even while stuck in user callbacks_. If you don't implement this, your servers won't be inspectable under production debugging tools, which is a common tell of a half-baked OTP port.

### 9.1 `get_state`

**Claim**: `sys:get_state(Pid)` returns the process's current state without invoking user callbacks (for `gen_server`) or returns `{State, Data}` for `gen_statem`.
**Source**: <https://www.erlang.org/doc/apps/stdlib/sys.html#get_state/1>
**Test**: Start gen_server with state `{counter, 0}`. `sys:get_state` returns `{counter, 0}` without any `handle_call`/`cast` invocation.

### 9.2 `suspend` / `resume`

**Claim**: `sys:suspend(Pid)` pauses the process — subsequent messages queue but no callbacks run. `sys:resume(Pid)` drains the queue.
**Source**: <https://www.erlang.org/doc/apps/stdlib/sys.html#suspend/1>
**Test**: Suspend server, cast 3 messages, assert none processed after 200ms. Resume, assert all 3 processed.

### 9.3 `replace_state`

**Claim**: `sys:replace_state(Pid, Fun)` applies `Fun` to current state and swaps the result in.
**Source**: <https://www.erlang.org/doc/apps/stdlib/sys.html#replace_state/2>
**Test**: Server state `{counter, 0}`. Replace with `fun ({counter, N}) -> {counter, N+100} end`. Assert new state visible via subsequent call.

---

## 10. Python/anyio mapping notes (non-testable guidance)

These are design notes for the implementer, not test claims. They call out places where faithful porting requires more than a mechanical translation.

### 10.1 Mailbox implementation

`anyio.create_memory_object_stream` is FIFO-only and does not support selective receive (§3.2). A conformant mailbox should be a list with a cursor + a "wake" event, not a plain queue. See test §3.2 — if it passes, you've implemented selective receive correctly.

### 10.2 Links vs structured concurrency

Erlang links bypass call-stack structure; anyio task groups enforce it. Options:

- **Exception-based links**: linked-partner failure cancels via raising in the host task. Clean but loses the "linked to many" semantics.
- **Signal-based links**: exit signals delivered as mailbox messages, even when not trapping. Closer to BEAM semantics.

Recommend the signal-based model and implementing links on top of a dedicated "signal stream" distinct from the message mailbox.

### 10.3 `call` timeout does not kill callee

§4.3 is subtle: the Erlang `call` timeout only affects the caller's wait. The callee keeps running; it may eventually produce a reply that is silently dropped. Python implementations that use `asyncio.wait_for` tend to _cancel_ the callee, which is the wrong semantics. Use a monitor + select pattern instead.

### 10.4 Hot code reload

`code_change/3` and release upgrades depend on BEAM's module versioning. Declare this out-of-scope and have `code_change/3` be a no-op callback for API compatibility only.

### 10.5 Reduction-based fairness

BEAM preempts processes on a reduction count. anyio is cooperative. Document this as a known divergence; tests should not depend on fair scheduling under CPU-bound callbacks.

---

## 11. Coverage checklist

A conformant port should have at least one passing test for every numbered claim in §2 through §9. Use this table as a ticketing checklist:

| Section               | Claims  | Priority |
| --------------------- | ------- | -------- |
| §2 Process primitives | 2.1–2.7 | P0       |
| §3 Mailbox            | 3.1–3.4 | P0       |
| §4 gen_server         | 4.1–4.9 | P0       |
| §5 gen_statem         | 5.1–5.6 | P1       |
| §6 supervisor         | 6.1–6.7 | P0       |
| §7 gen_event          | 7.1–7.3 | P2       |
| §8 Registered names   | 8.1–8.2 | P1       |
| §9 sys protocol       | 9.1–9.3 | P1       |

P0 = blocks any real usage; P1 = needed for production credibility; P2 = nice-to-have.

---

## 12. Further reading for implementers

- Cesarini & Vinoski, _Designing for Scalability with Erlang/OTP_ — the mental model behind every claim above.
- Hébert, _Learn You Some Erlang_ — <https://learnyousomeerlang.com/> (free, excellent supervision-tree chapters).
- Armstrong, _Programming Erlang_ (2nd ed.) — the "why".
- Elixir source as a second reference impl: <https://github.com/elixir-lang/elixir/tree/main/lib/elixir/lib> (see `GenServer`, `Supervisor`, `DynamicSupervisor`).
