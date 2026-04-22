import importlib
import subprocess
import sys
import typing as t
from pathlib import Path

import pytest
from anyio import Event, fail_after, sleep

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

opentelemetry = pytest.importorskip("opentelemetry")
metrics_api = importlib.import_module("opentelemetry.metrics")
trace_api = importlib.import_module("opentelemetry.trace")
MeterProvider = importlib.import_module("opentelemetry.sdk.metrics").MeterProvider
InMemoryMetricReader = importlib.import_module(
    "opentelemetry.sdk.metrics.export"
).InMemoryMetricReader
TracerProvider = importlib.import_module("opentelemetry.sdk.trace").TracerProvider
SimpleSpanProcessor = importlib.import_module(
    "opentelemetry.sdk.trace.export"
).SimpleSpanProcessor
InMemorySpanExporter = importlib.import_module(
    "opentelemetry.sdk.trace.export.in_memory_span_exporter"
).InMemorySpanExporter
StatusCode = trace_api.StatusCode
tel = importlib.import_module("fastactor.telemetry")
otp = importlib.import_module("fastactor.otp")
helpers = importlib.import_module("helpers")
Call = otp.Call
Continue = otp.Continue
GenServer = otp.GenServer
Runtime = otp.Runtime
Supervisor = otp.Supervisor
BoomServer = helpers.BoomServer
EchoServer = helpers.EchoServer
await_child_restart = helpers.await_child_restart


pytestmark = pytest.mark.anyio


_EXPORTER = InMemorySpanExporter()
_TP = TracerProvider()
_TP.add_span_processor(SimpleSpanProcessor(_EXPORTER))
_READER = InMemoryMetricReader()
_MP = MeterProvider(metric_readers=[_READER])
trace_api.set_tracer_provider(_TP)
metrics_api.set_meter_provider(_MP)


@pytest.fixture
async def runtime() -> t.AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def _clear_otel() -> t.Iterator[None]:
    _EXPORTER.clear()
    yield


def _span_parent_of(spans: t.Sequence[t.Any], child: t.Any) -> t.Any | None:
    parent = child.parent
    if parent is None:
        return None
    for span in spans:
        if span.context.span_id == parent.span_id:
            return span
    return None


def _find_span(spans: t.Sequence[t.Any], name: str, **attrs: t.Any) -> t.Any:
    for span in spans:
        if span.name != name:
            continue
        if all(span.attributes.get(key) == value for key, value in attrs.items()):
            return span
    raise AssertionError(f"missing span {name} with attrs {attrs}")


def _has_ancestor(spans: t.Sequence[t.Any], child: t.Any, ancestor: t.Any) -> bool:
    current = _span_parent_of(spans, child)
    while current is not None:
        if current.context.span_id == ancestor.context.span_id:
            return True
        current = _span_parent_of(spans, current)
    return False


def span_duration(span: t.Any) -> float:
    return (span.end_time - span.start_time) / 1_000_000_000


def _metric_sum(name: str) -> int | float:
    _MP.force_flush()
    total: int | float = 0
    metrics_data = _READER.get_metrics_data()
    if metrics_data is None:
        return total
    for resource_metric in metrics_data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    total += getattr(point, "value", 0)
    return total


class MiddleServer(GenServer):
    async def init(self, echo: EchoServer) -> None:
        self.echo = echo

    async def handle_call(self, call: Call) -> t.Any:
        return await self.echo.call(call.message)


class InfoProbeServer(GenServer):
    async def init(self) -> None:
        self.seen = Event()

    async def handle_info(self, message) -> None:
        self.seen.set()


class FlakyContinueServer(GenServer):
    attempts: t.ClassVar[int] = 0

    async def init(self, crash_times: int, stabilized: Event) -> Continue:
        self.crash_times = crash_times
        self.stabilized = stabilized
        return Continue("boot")

    async def handle_continue(self, term: t.Any) -> None:
        type(self).attempts += 1
        if type(self).attempts <= self.crash_times:
            raise RuntimeError(f"crash-{type(self).attempts}")
        self.stabilized.set()


class DeferredServer(GenServer):
    async def handle_call(self, call: Call) -> None:
        runtime = Runtime.current()
        assert runtime._task_group is not None
        runtime._task_group.start_soon(self._reply_later, call)
        return None

    async def _reply_later(self, call: Call) -> None:
        await sleep(0.1)
        call.set_result("done")


def test_import_without_opentelemetry() -> None:
    code = """
from importlib import import_module
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd() / "src"))
sys.modules["opentelemetry"] = None

telemetry = import_module("fastactor.telemetry")
assert telemetry.is_enabled() is False

try:
    telemetry.instrument(object())
except RuntimeError as error:
    assert "fastactor[otel]" in str(error) or "OpenTelemetry is not installed" in str(error)
else:
    raise AssertionError("instrument unexpectedly succeeded")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout


async def test_two_hop_call_produces_correct_parent_chain() -> None:
    async with Runtime(telemetry=True):
        echo = await EchoServer.start()
        middle = await MiddleServer.start(echo=echo)

        assert await middle.call("hi") == "hi"

    spans = _EXPORTER.get_finished_spans()
    middle_call = _find_span(
        spans,
        "fastactor.gen_server.call",
        **{tel.ATTR_TARGET_ID: middle.id},
    )
    middle_handle_message = _find_span(
        spans,
        "fastactor.process.handle_message",
        **{
            tel.ATTR_PROCESS_ID: middle.id,
            tel.ATTR_MESSAGE_TYPE: "Call",
        },
    )
    middle_handle_call = _find_span(
        spans,
        "fastactor.gen_server.handle_call",
        **{tel.ATTR_TARGET_ID: middle.id},
    )
    echo_call = _find_span(
        spans,
        "fastactor.gen_server.call",
        **{tel.ATTR_TARGET_ID: echo.id},
    )
    echo_handle_message = _find_span(
        spans,
        "fastactor.process.handle_message",
        **{
            tel.ATTR_PROCESS_ID: echo.id,
            tel.ATTR_MESSAGE_TYPE: "Call",
        },
    )
    echo_handle_call = _find_span(
        spans,
        "fastactor.gen_server.handle_call",
        **{tel.ATTR_TARGET_ID: echo.id},
    )

    relevant = [
        middle_call,
        middle_handle_message,
        middle_handle_call,
        echo_call,
        echo_handle_message,
        echo_handle_call,
    ]
    trace_ids = {span.context.trace_id for span in relevant}

    assert len(trace_ids) == 1
    assert _span_parent_of(spans, middle_handle_message) is middle_call
    assert _span_parent_of(spans, middle_handle_call) is middle_handle_message
    assert _span_parent_of(spans, echo_call) is middle_handle_call
    assert _span_parent_of(spans, echo_handle_message) is echo_call
    assert _span_parent_of(spans, echo_handle_call) is echo_handle_message
    assert _has_ancestor(spans, echo_call, middle_handle_call)


async def test_handle_message_records_exception_on_crash() -> None:
    async with Runtime(telemetry=True):
        boom = await BoomServer.start()

        with pytest.raises(RuntimeError, match="Boom!"):
            await boom.call("boom")

    spans = _EXPORTER.get_finished_spans()
    handle_message = _find_span(
        spans,
        "fastactor.process.handle_message",
        **{
            tel.ATTR_PROCESS_ID: boom.id,
            tel.ATTR_MESSAGE_TYPE: "Call",
        },
    )
    handle_call = _find_span(
        spans,
        "fastactor.gen_server.handle_call",
        **{tel.ATTR_TARGET_ID: boom.id},
    )

    for span in (handle_message, handle_call):
        assert span.status.status_code is StatusCode.ERROR
        assert any(event.name == "exception" for event in span.events)


async def test_supervisor_restart_counter_fires_per_restart() -> None:
    expected_restarts = 3
    stabilized = Event()
    FlakyContinueServer.attempts = 0
    baseline = _metric_sum("fastactor.supervisor.restart")

    async with Runtime(telemetry=True):
        sup = await Supervisor.start(max_restarts=5, max_seconds=10)
        child = await sup.start_child(
            sup.child_spec(
                "worker",
                FlakyContinueServer,
                kwargs={
                    "crash_times": expected_restarts,
                    "stabilized": stabilized,
                },
            )
        )

        for _ in range(expected_restarts):
            child = await await_child_restart(sup, "worker", child, timeout=2)

        with fail_after(2):
            await stabilized.wait()

    total = _metric_sum("fastactor.supervisor.restart") - baseline

    assert total == expected_restarts


async def test_context_propagates_through_process_send() -> None:
    async with Runtime(telemetry=True):
        server = await InfoProbeServer.start()

        tracer = trace_api.get_tracer("tests.telemetry")
        with tracer.start_as_current_span("user-root") as user_span:
            trace_id = user_span.get_span_context().trace_id
            server.info("payload")

        with fail_after(2):
            await server.seen.wait()

    spans = _EXPORTER.get_finished_spans()
    user_root = _find_span(spans, "user-root")
    info_span = _find_span(
        spans,
        "fastactor.process.handle_message",
        **{
            tel.ATTR_PROCESS_ID: server.id,
            tel.ATTR_MESSAGE_TYPE: "Info",
        },
    )

    assert user_root.context.trace_id == trace_id
    assert info_span.context.trace_id == trace_id
    assert _span_parent_of(spans, info_span) is user_root


async def test_deferred_reply_server_span_is_short() -> None:
    async with Runtime(telemetry=True):
        server = await DeferredServer.start()

        assert await server.call("wait", timeout=1) == "done"

    spans = _EXPORTER.get_finished_spans()
    client_span = _find_span(
        spans,
        "fastactor.gen_server.call",
        **{tel.ATTR_TARGET_ID: server.id},
    )
    server_span = _find_span(
        spans,
        "fastactor.gen_server.handle_call",
        **{tel.ATTR_TARGET_ID: server.id},
    )

    assert span_duration(server_span) < 0.05
    assert span_duration(client_span) >= 0.08
    assert span_duration(client_span) >= span_duration(server_span)


async def test_uninstrument_stops_emission() -> None:
    async with Runtime(telemetry=True):
        server = await EchoServer.start()
        assert await server.call("hi") == "hi"

    assert _EXPORTER.get_finished_spans()

    _EXPORTER.clear()

    async with Runtime(telemetry=False):
        server = await EchoServer.start()
        assert await server.call("bye") == "bye"

    assert not _EXPORTER.get_finished_spans()
