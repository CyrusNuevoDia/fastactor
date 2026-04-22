import pytest
from anyio import Event, fail_after

from fastactor.otp import Cast, GenServer, Info, Process, Runtime


pytestmark = pytest.mark.anyio


class MetaEchoServer(GenServer):
    async def init(self) -> None:
        self.last_call_metadata: dict | None = None
        self.last_cast_metadata: dict | None = None
        self.cast_seen = Event()

    async def handle_call(self, call):
        self.last_call_metadata = call.metadata
        return call.message

    async def handle_cast(self, cast: Cast):
        self.last_cast_metadata = cast.metadata
        self.cast_seen.set()


class InfoRecorder(Process):
    async def init(self) -> None:
        self.last_info_metadata: dict | None = None
        self.seen = Event()

    async def handle_info(self, message: Info):
        self.last_info_metadata = message.metadata
        self.seen.set()


async def test_call_metadata_reaches_handler(runtime: Runtime):
    server = await MetaEchoServer.start()
    await server.call("hi", metadata={"trace_id": "abc", "user": "k"})
    assert server.last_call_metadata == {"trace_id": "abc", "user": "k"}
    await server.stop("normal")


async def test_call_metadata_defaults_to_none(runtime: Runtime):
    server = await MetaEchoServer.start()
    await server.call("hi")
    assert server.last_call_metadata is None
    await server.stop("normal")


async def test_cast_metadata_reaches_handler(runtime: Runtime):
    server = await MetaEchoServer.start()
    server.cast("payload", metadata={"span": "s1"})
    with fail_after(2):
        await server.cast_seen.wait()
    assert server.last_cast_metadata == {"span": "s1"}
    await server.stop("normal")


async def test_info_metadata_reaches_handler(runtime: Runtime):
    proc = await InfoRecorder.start()
    proc.info("payload", metadata={"correlation": "xyz"})
    with fail_after(2):
        await proc.seen.wait()
    assert proc.last_info_metadata == {"correlation": "xyz"}
    await proc.stop("normal")


async def test_prebuilt_message_metadata_survives_send(runtime: Runtime):
    proc = await InfoRecorder.start()
    msg = Info(sender=None, message="direct", metadata={"k": "v"})
    await proc.send(msg)
    with fail_after(2):
        await proc.seen.wait()
    assert proc.last_info_metadata == {"k": "v"}
    await proc.stop("normal")
