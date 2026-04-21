import pytest

from support import CounterServer

from fastactor.otp import AlreadyRegistered, Registry

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
