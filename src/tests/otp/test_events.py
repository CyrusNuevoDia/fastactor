from time import monotonic

import pytest
from anyio import Event, fail_after, sleep

from fastactor.otp import GenServer, Process, Runtime

pytestmark = pytest.mark.anyio


class PingServer(GenServer):
    async def handle_call(self, call):
        return call.message


async def test_process_emits_started_and_stopped(runtime: Runtime):
    events: list[str] = []

    class Observed(Process):
        async def init(self) -> None:
            self.emitter.on("started", lambda _self: events.append("started"))
            self.emitter.on(
                "stopped", lambda _self, reason: events.append(f"stopped:{reason}")
            )

    proc = await Observed.start()
    with fail_after(2):
        await proc.stop("normal")

    assert events == ["started", "stopped:normal"]


async def test_process_emits_crashed_on_abnormal_exit(runtime: Runtime):
    seen: list[tuple] = []

    class Boom(Process):
        async def init(self) -> None:
            self.emitter.on(
                "crashed", lambda _self, exc, reason: seen.append((exc, reason))
            )

        async def handle_info(self, message):
            raise RuntimeError("kaboom")

    proc = await Boom.start()
    proc.info("go")
    with fail_after(2):
        await proc.stopped()

    assert len(seen) == 1
    exc, reason = seen[0]
    assert isinstance(exc, RuntimeError)
    assert isinstance(reason, RuntimeError)


async def test_process_does_not_emit_crashed_on_normal_stop(runtime: Runtime):
    seen: list[object] = []

    class Quiet(Process):
        async def init(self) -> None:
            self.emitter.on("crashed", lambda *args, **kwargs: seen.append(args))

    proc = await Quiet.start()
    await proc.stop("normal")

    assert seen == []


async def test_runtime_emits_process_spawned_and_unregistered(runtime: Runtime):
    spawned: list[str] = []
    unregistered: list[str] = []

    runtime.emitter.on("process:spawned", lambda p: spawned.append(p.id))
    runtime.emitter.on("process:unregistered", lambda p: unregistered.append(p.id))

    proc = await PingServer.start()
    with fail_after(2):
        await proc.stop("normal")

    # Runtime supervisor itself fires spawned during Runtime startup (before our listener
    # is attached via the fixture), so we only see our new process here.
    assert proc.id in spawned
    assert proc.id in unregistered


async def test_runtime_firehose_sees_process_events(runtime: Runtime):
    seen: list[tuple[str, str]] = []

    def listener(event):
        return lambda proc, **_kw: seen.append((event, proc.id))

    runtime.emitter.on("started", listener("started"))
    runtime.emitter.on("stopped", listener("stopped"))

    proc = await PingServer.start()
    await proc.stop("normal")

    assert ("started", proc.id) in seen
    assert ("stopped", proc.id) in seen


async def test_runtime_emits_message_events_for_call(runtime: Runtime):
    received: list[object] = []
    handled: list[object] = []

    runtime.emitter.on(
        "message:received",
        lambda _proc, message: received.append(type(message).__name__),
    )
    runtime.emitter.on(
        "message:handled", lambda _proc, message: handled.append(type(message).__name__)
    )

    proc = await PingServer.start()
    reply = await proc.call("hi")
    assert reply == "hi"
    await proc.stop("normal")

    assert "Call" in received
    assert "Call" in handled


async def test_async_listener_is_awaited(runtime: Runtime):
    done = Event()
    observed = Event()

    async def slow_listener(_proc):
        await sleep(0.05)
        done.set()

    class Simple(Process):
        pass

    proc = Simple()
    proc.emitter.on("started", slow_listener)
    proc.emitter.on("started", lambda _self: observed.set())

    start = monotonic()
    await runtime.spawn(proc)
    elapsed = monotonic() - start

    # started emit awaits both listeners; slow_listener runs before spawn returns
    assert done.is_set()
    assert observed.is_set()
    assert elapsed >= 0.05

    await proc.stop("normal")


async def test_bad_listener_crashes_process(runtime: Runtime):
    class Stable(Process):
        async def init(self) -> None:
            self.emitter.on(
                "message:received",
                lambda _self, message: (_ for _ in ()).throw(
                    ValueError("listener bug")
                ),
            )

        async def handle_info(self, message):
            return None

    proc = await Stable.start()
    proc.info("trigger")
    with fail_after(2):
        await proc.stopped()

    assert isinstance(proc._crash_exc, ValueError)
    assert "listener bug" in str(proc._crash_exc)


async def test_supervisor_emits_child_started_and_terminated(runtime: Runtime):
    from fastactor.otp import Supervisor

    sup = await Supervisor.start()

    started: list[str] = []
    terminated: list[tuple[str, object]] = []
    sup.emitter.on(
        "child:started", lambda _sup, child, spec_id: started.append(spec_id)
    )
    sup.emitter.on(
        "child:terminated",
        lambda _sup, child, spec_id, reason: terminated.append((spec_id, reason)),
    )

    # Attach listeners first, then add/remove a child to observe both events.
    spec = Supervisor.child_spec("pinger", PingServer, restart="temporary")
    await sup.start_child(spec)
    await sup.terminate_child("pinger")
    await sup.stop("normal")

    assert "pinger" in started
    assert "pinger" in [tid for tid, _ in terminated]


async def test_raising_started_listener_does_not_hang_spawn(runtime: Runtime):
    class Target(Process):
        pass

    proc = Target()
    proc.emitter.on(
        "started",
        lambda _self: (_ for _ in ()).throw(RuntimeError("bad started listener")),
    )

    with pytest.raises(RuntimeError, match="bad started listener"):
        with fail_after(2):
            await runtime.spawn(proc)

    assert proc.has_stopped()
    assert isinstance(proc._crash_exc, RuntimeError)
