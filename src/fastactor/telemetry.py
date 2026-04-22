import typing as t

try:
    from opentelemetry import trace, metrics, context, propagate
    from opentelemetry.trace import Span, StatusCode, Status, set_span_in_context

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    trace = metrics = context = propagate = None  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    Span = object  # type: ignore[assignment,misc]  # ty: ignore[invalid-assignment]
    StatusCode = Status = set_span_in_context = None  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

if t.TYPE_CHECKING:
    from collections.abc import Iterable

    from opentelemetry.context import Context
    from opentelemetry.metrics import Meter, MeterProvider, Observation
    from opentelemetry.trace import Tracer, TracerProvider

    from fastactor.otp import Runtime

VERSION = "0.1.0"

ATTR_PROCESS_ID = "fastactor.process.id"
ATTR_PROCESS_CLASS = "fastactor.process.class"
ATTR_PARENT_ID = "fastactor.parent.id"
ATTR_MESSAGE_TYPE = "fastactor.message.type"
ATTR_MESSAGE_SENDER_ID = "fastactor.message.sender.id"
ATTR_TARGET_ID = "fastactor.target.id"
ATTR_TARGET_CLASS = "fastactor.target.class"
ATTR_CALL_TIMEOUT = "fastactor.call.timeout"
ATTR_SUPERVISOR_NAME = "fastactor.supervisor.name"
ATTR_CHILD_ID = "fastactor.child.id"
ATTR_RESTART_REASON = "fastactor.restart.reason"
ATTR_RESTART_COUNT = "fastactor.restart.count"
ATTR_RESTART_STRATEGY = "fastactor.restart.strategy"
ATTR_RPC_SYSTEM = "rpc.system"
RPC_SYSTEM_VALUE = "fastactor"

_install_count = 0
_tracer: "Tracer | None" = None
_meter: "Meter | None" = None


def _otel_error() -> RuntimeError:
    return RuntimeError(
        "OpenTelemetry is not installed. Install with: pip install 'fastactor[otel]'"
    )


def _context_with_span(span: Span) -> "Context":
    if set_span_in_context is None:
        raise _otel_error()
    return set_span_in_context(span)


def _reason_label(reason: t.Any) -> str:
    from fastactor.otp import Shutdown

    if reason in {"normal", "shutdown"}:
        return "normal"
    if isinstance(reason, Shutdown):
        return "shutdown"
    return "crash"


def is_enabled() -> bool:
    return _OTEL_AVAILABLE and _install_count > 0


def instrument(
    runtime: "Runtime",
    *,
    tracer_provider: "TracerProvider | None" = None,
    meter_provider: "MeterProvider | None" = None,
) -> t.Callable[[], None]:
    global _install_count, _tracer, _meter

    if not _OTEL_AVAILABLE:
        raise _otel_error()

    from opentelemetry.metrics import Observation

    from fastactor.otp import Runtime

    if not isinstance(runtime, Runtime):
        raise TypeError("runtime must be a Runtime")

    assert trace is not None
    assert metrics is not None

    if _tracer is None:
        if tracer_provider is None:
            _tracer = trace.get_tracer("fastactor", VERSION)
        else:
            _tracer = tracer_provider.get_tracer("fastactor", VERSION)
    if _meter is None:
        if meter_provider is None:
            _meter = metrics.get_meter("fastactor", VERSION)
        else:
            _meter = meter_provider.get_meter("fastactor", VERSION)

    meter = _meter
    assert meter is not None

    process_started_counter = meter.create_counter(
        "fastactor.process.started",
        unit="{process}",
        description="Processes started",
    )
    process_stopped_counter = meter.create_counter(
        "fastactor.process.stopped",
        unit="{process}",
        description="Processes stopped, tagged by reason",
    )
    process_active = meter.create_up_down_counter(
        "fastactor.process.active",
        unit="{process}",
        description="Live processes registered with the runtime",
    )
    meter.create_histogram(
        "fastactor.message.handle_duration",
        unit="s",
        description="Time to handle a mailbox message",
    )
    supervisor_restart_counter = meter.create_counter(
        "fastactor.supervisor.restart",
        unit="{restart}",
        description="Supervisor child restarts",
    )

    def observe_mailbox_depth(_options: t.Any) -> "Iterable[Observation]":
        for proc in tuple(runtime.processes.values()):
            if proc._inbox is None:
                continue
            try:
                depth = proc._inbox.statistics().current_buffer_used
            except (AttributeError, RuntimeError):
                continue
            yield Observation(
                depth,
                {
                    ATTR_PROCESS_ID: proc.id,
                    ATTR_PROCESS_CLASS: type(proc).__name__,
                },
            )

    meter.create_observable_gauge(
        "fastactor.mailbox.depth",
        callbacks=[observe_mailbox_depth],
        unit="{message}",
        description="Current mailbox depth per process",
    )

    def on_started(proc) -> None:
        attrs = {ATTR_PROCESS_CLASS: type(proc).__name__}
        process_started_counter.add(1, attrs)
        process_active.add(1, attrs)

    def on_stopped(proc, *, reason: t.Any) -> None:
        process_stopped_counter.add(
            1,
            {
                ATTR_PROCESS_CLASS: type(proc).__name__,
                "reason": _reason_label(reason),
            },
        )
        process_active.add(-1, {ATTR_PROCESS_CLASS: type(proc).__name__})

    def on_child_restarted(supervisor, *, spec_id: str, restart_count: int) -> None:
        supervisor_restart_counter.add(
            1,
            {
                ATTR_SUPERVISOR_NAME: supervisor.id,
                ATTR_CHILD_ID: spec_id,
            },
        )

    listeners: list[tuple[str, t.Callable[..., None]]] = [
        ("started", on_started),
        ("stopped", on_stopped),
        ("child:restarted", on_child_restarted),
    ]
    for event, callback in listeners:
        runtime.emitter.on(event, callback)

    _install_count += 1
    installed = True

    def uninstrument() -> None:
        nonlocal installed
        global _install_count

        if not installed:
            return

        installed = False
        for event, callback in listeners:
            runtime.emitter.remove_listener(event, callback)
        _install_count -= 1

    return uninstrument


def get_tracer() -> "Tracer":
    if not _OTEL_AVAILABLE:
        raise _otel_error()
    if _tracer is None:
        raise RuntimeError("FastActor telemetry is not instrumented.")
    return _tracer


def get_meter() -> "Meter":
    if not _OTEL_AVAILABLE:
        raise _otel_error()
    if _meter is None:
        raise RuntimeError("FastActor telemetry is not instrumented.")
    return _meter


def inject(metadata: dict[str, t.Any] | None) -> dict[str, t.Any] | None:
    if not is_enabled():
        return metadata

    assert propagate is not None
    if metadata is None:
        carrier: dict[str, t.Any] = {}
        propagate.inject(carrier)
        return carrier or None

    propagate.inject(metadata)
    return metadata


def extract(metadata: dict[str, t.Any] | None) -> "Context | None":
    if not metadata or not is_enabled():
        return None
    if context is None or propagate is None:
        return None
    return propagate.extract(metadata)


def record_exception(span, exc: BaseException) -> None:
    if span is None or not is_enabled():
        return
    if not span.is_recording():
        return
    assert Status is not None
    assert StatusCode is not None
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


__all__ = [
    "instrument",
    "is_enabled",
    "get_tracer",
    "get_meter",
    "inject",
    "extract",
    "record_exception",
    "ATTR_PROCESS_ID",
    "ATTR_PROCESS_CLASS",
    "ATTR_PARENT_ID",
    "ATTR_MESSAGE_TYPE",
    "ATTR_MESSAGE_SENDER_ID",
    "ATTR_TARGET_ID",
    "ATTR_TARGET_CLASS",
    "ATTR_CALL_TIMEOUT",
    "ATTR_SUPERVISOR_NAME",
    "ATTR_CHILD_ID",
    "ATTR_RESTART_REASON",
    "ATTR_RESTART_COUNT",
    "ATTR_RESTART_STRATEGY",
    "ATTR_RPC_SYSTEM",
    "RPC_SYSTEM_VALUE",
]
