import pytest
from .helpers import CounterServer, EchoServer, await_child_restart

from fastactor.otp import AlreadyRegistered, Failed, Registry, Runtime
from fastactor.otp.registry import RegistryServer

pytestmark = pytest.mark.anyio


async def test_registry_unique_register_and_lookup():
    """G: a unique registry. W: a process registers a key. T: lookup returns that process."""
    await Registry.new("unique", "unique")
    proc = await CounterServer.start()

    await Registry.register("unique", "alpha", proc)

    assert await Registry.lookup("unique", "alpha") == [proc]

    await proc.stop("normal")


async def test_registry_unique_collision_raises():
    """G: a unique registry key already claimed. W: another process registers it. T: AlreadyRegistered is raised."""
    await Registry.new("unique", "unique")
    first = await CounterServer.start()
    second = await CounterServer.start()
    await Registry.register("unique", "alpha", first)

    with pytest.raises(AlreadyRegistered):
        await Registry.register("unique", "alpha", second)

    await first.stop("normal")
    await second.stop("normal")


async def test_registry_duplicate_lookup_returns_all_processes():
    """G: a duplicate registry. W: two processes register the same key. T: lookup returns both."""
    await Registry.new("dupe", "duplicate")
    first = await CounterServer.start()
    second = await CounterServer.start()
    await Registry.register("dupe", "alpha", first)
    await Registry.register("dupe", "alpha", second)

    assert set(await Registry.lookup("dupe", "alpha")) == {first, second}

    await first.stop("normal")
    await second.stop("normal")


async def test_registry_dispatch_invokes_callback_for_each_process():
    """G: duplicate registry members. W: dispatch runs a callback. T: every process observes the callback."""
    await Registry.new("dupe", "duplicate")
    first = await CounterServer.start()
    second = await CounterServer.start()
    await Registry.register("dupe", "alpha", first)
    await Registry.register("dupe", "alpha", second)

    async def add_one(proc):
        proc.cast(("add", 1))

    await Registry.dispatch("dupe", "alpha", add_one)
    await first.call("sync")
    await second.call("sync")

    assert await first.call("get") == 1
    assert await second.call("get") == 1

    await first.stop("normal")
    await second.stop("normal")


async def test_registry_keys_reflect_registration_and_auto_cleanup():
    """G: a registered process. W: it stops. T: keys disappear with the process cleanup."""
    await Registry.new("unique", "unique")
    proc = await CounterServer.start()
    await Registry.register("unique", "alpha", proc)

    assert await Registry.keys("unique", proc) == ["alpha"]

    await proc.stop("normal")
    await proc.stopped()

    assert await Registry.lookup("unique", "alpha") == []


async def test_registry_via_start_spawns_into_registry():
    """G: a unique registry. W: a GenServer starts with via=(registry, key). T: lookup and whereis both find it."""
    from fastactor.otp import whereis

    await Registry.new("via-reg", "unique")
    proc = await CounterServer.start(via=("via-reg", "alpha"))

    assert await Registry.lookup("via-reg", "alpha") == [proc]
    assert await whereis(("via-reg", "alpha")) is proc

    await proc.stop("normal")


async def test_registry_concurrent_unique_register_raises_for_losers():
    """G: a unique registry and several contenders. W: they race to register one key. T: exactly one wins and the rest raise AlreadyRegistered."""
    import anyio

    await Registry.new("race-reg", "unique")
    procs = [await CounterServer.start() for _ in range(5)]
    results: list[bool | Exception] = []

    async def contender(proc):
        try:
            await Registry.register("race-reg", "hotkey", proc)
            results.append(True)
        except AlreadyRegistered as error:
            results.append(error)

    async with anyio.create_task_group() as tg:
        for proc in procs:
            tg.start_soon(contender, proc)

    wins = [result for result in results if result is True]
    losses = [result for result in results if isinstance(result, AlreadyRegistered)]

    assert len(wins) == 1
    assert len(losses) == len(procs) - 1

    for proc in procs:
        await proc.stop("normal")


async def test_registry_start_link_returns_supervised_process(make_supervisor):
    """G: a supervisor-owned registry. W: we use Registry.start_link and stop the supervisor. T: the registry behaves normally and is cleaned up with the supervisor."""
    sup = await make_supervisor()
    registry_proc = await Registry.start_link(
        name="supervised-reg",
        keys="unique",
        supervisor=sup,
    )
    worker = await CounterServer.start()

    assert isinstance(registry_proc, RegistryServer)
    assert any(running.process is registry_proc for running in sup.children.values())

    await Registry.register("supervised-reg", "alpha", worker)

    assert await Registry.lookup("supervised-reg", "alpha") == [worker]

    await sup.stop("normal")

    assert registry_proc.has_stopped()
    assert "supervised-reg" not in Runtime.current().registries

    await worker.stop("normal")


async def test_registry_under_supervision_tree_restartable(make_supervisor):
    """G: a registry child with restart=permanent. W: it crashes. T: the supervisor restarts it with a fresh empty registry entry."""
    sup = await make_supervisor()
    registry_proc = await sup.start_child(
        sup.child_spec(
            "registry",
            RegistryServer,
            kwargs={"name": "restart-reg", "keys": "unique"},
            restart="permanent",
        )
    )
    worker = await CounterServer.start()

    await Registry.register("restart-reg", "alpha", worker)
    original_entry = Runtime.current().registries["restart-reg"]

    await registry_proc.stop(RuntimeError("registry crash"))
    restarted = await await_child_restart(sup, "registry", registry_proc)

    assert restarted is not registry_proc
    assert Runtime.current().registries["restart-reg"] is not original_entry
    assert await Registry.lookup("restart-reg", "alpha") == []

    await Registry.register("restart-reg", "alpha", worker)

    assert await Registry.lookup("restart-reg", "alpha") == [worker]

    await worker.stop("normal")
    await sup.stop("normal")


async def test_registry_new_still_works_after_refactor():
    """G: the legacy Registry.new API. W: we create and use a registry. T: existing register/lookup behavior still works."""
    await Registry.new("legacy-reg", "unique")
    worker = await CounterServer.start()

    await Registry.register("legacy-reg", "alpha", worker)

    assert await Registry.lookup("legacy-reg", "alpha") == [worker]

    await worker.stop("normal")


# ---------------------------------------------------------------------------
# §8 Registered names conformance
# ---------------------------------------------------------------------------


async def test_8_1_local_registration_and_lookup(runtime: Runtime) -> None:
    """SPEC §8.1: Local registration — whereis returns the registered pid.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#register/2
    """
    proc = await EchoServer.start(name="alpha")

    assert await runtime.whereis("alpha") is proc

    await proc.stop("normal")


async def test_8_1_duplicate_local_registration_returns_already_started_tuple() -> None:
    """SPEC §8.1: Attempting to register a name twice returns `{already_started, Pid}`.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#register/2
    """
    first = await EchoServer.start(name="alpha")

    try:
        await EchoServer.start(name="alpha")
    except Failed as error:
        # Erlang exposes the existing pid in the error; port just wraps a string.
        reason = error.reason
        assert isinstance(reason, tuple)
        assert reason[0] == "already_started"
        assert reason[1] is first

    await first.stop("normal")


async def test_8_1_name_becomes_free_after_process_exits(runtime: Runtime) -> None:
    """SPEC §8.1: When a registered process exits, the name becomes available again.

    Source: https://www.erlang.org/doc/apps/erts/erlang.html#register/2
    """
    first = await EchoServer.start(name="beta")
    await first.stop("normal")
    await first.stopped()

    assert await runtime.whereis("beta") is None

    second = await EchoServer.start(name="beta")
    assert await runtime.whereis("beta") is second
    await second.stop("normal")


async def test_8_2_port_native_via_tuple_registers_and_finds_process(
    runtime: Runtime,
) -> None:
    """SPEC §8.2 (port variant): Port's native `via=(registry, key)` tuple routes correctly.

    Documented as a port-native equivalent of `{via, Module, Term}`.
    Source: https://www.erlang.org/doc/apps/stdlib/gen_server.html#start_link/4
    """
    await Registry.new("spec-via", "unique")
    proc = await EchoServer.start(via=("spec-via", "alpha"))

    assert await Registry.lookup("spec-via", "alpha") == [proc]
    assert await runtime.whereis(("spec-via", "alpha")) is proc

    await proc.stop("normal")
