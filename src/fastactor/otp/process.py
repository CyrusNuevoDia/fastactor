from dataclasses import dataclass, field
import logging
import typing as t

from anyio import (
    ClosedResourceError,
    EndOfStream,
    Event,
    create_memory_object_stream,
    fail_after,
)
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from ..settings import settings
from ..utils import id_generator
from ._exceptions import Crashed, Shutdown, _is_normal_shutdown_reason
from ._messages import Continue, Down, Exit, Ignore, Info, Message, Stop

if t.TYPE_CHECKING:
    from .supervisor import Supervisor

logger = logging.getLogger(__name__)


@dataclass(repr=False)
class Process:
    id: str = field(default_factory=id_generator("process"))
    supervisor: "Supervisor | None" = None
    trap_exits: bool = False

    _inbox: MemoryObjectSendStream[Message] | None = field(default=None, init=False)
    _mailbox: MemoryObjectReceiveStream[Message] | None = field(
        default=None, init=False
    )

    _started: Event = field(default_factory=Event, init=False)
    _stopped: Event = field(default_factory=Event, init=False)
    _crash_exc: Exception | None = field(default=None, init=False)
    _pending_continue: Continue | None = field(default=None, init=False)

    links: set["Process"] = field(default_factory=set, init=False)
    monitors: set["Process"] = field(default_factory=set, init=False)
    monitored_by: set["Process"] = field(default_factory=set, init=False)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other: object):
        if not isinstance(other, Process):
            return NotImplemented
        return self.id == other.id

    async def _init(self, *args, **kwargs):
        logger.debug("%s init", self)
        try:
            init_result = await self.init(*args, **kwargs)
        except Exception as error:
            raise Crashed(str(error)) from error

        match init_result:
            case Ignore():
                self._started.set()
                self._stopped.set()
            case Stop(reason=reason):
                self._started.set()
                self._stopped.set()
                raise Shutdown(reason)
            case Continue() as continuation:
                self._pending_continue = continuation
                self._inbox, self._mailbox = create_memory_object_stream(
                    settings.mailbox_size
                )
            case _:
                self._inbox, self._mailbox = create_memory_object_stream(
                    settings.mailbox_size
                )

    async def init(self, *args, **kwargs) -> Ignore | Stop | Continue | None:
        return None

    def has_started(self) -> bool:
        return self._started.is_set()

    async def started(self) -> t.Self:
        await self._started.wait()
        return self

    def has_stopped(self) -> bool:
        return self._stopped.is_set()

    async def stopped(self) -> t.Self:
        await self._stopped.wait()
        return self

    async def handle_exit(self, message: Exit) -> None:
        pass

    async def handle_down(self, message: Down) -> None:
        pass

    async def handle_continue(self, term: t.Any) -> None:
        return None

    async def on_terminate(self, reason: t.Any) -> None:
        return None

    async def terminate(self, reason: t.Any):
        logger.debug("%s terminate reason=%s", self, reason)

        try:
            await self.on_terminate(reason)
        except Exception as error:
            logger.error("%r in on_terminate of %s: %s", error, self, reason)

        if self.monitored_by:
            logger.debug("%s notifying monitors", self)
            for process in list(self.monitored_by):
                process.demonitor(self)
                try:
                    await process.send(Down(self, reason))
                except Exception as error:
                    logger.error(
                        "%r sending Down(%s, %s) to monitor: %s",
                        error,
                        self,
                        reason,
                        process,
                    )

        if self.links:
            logger.debug("%s notifying links", self)
            abnormal_shutdown = not _is_normal_shutdown_reason(reason)
            for process in list(self.links):
                self.unlink(process)
                if process.trap_exits:
                    try:
                        await process.send(Exit(self, reason))
                    except Exception as error:
                        logger.error(
                            "%r sending Exit(%s, %s) to linked actor: %s",
                            error,
                            self,
                            reason,
                            process,
                        )
                elif abnormal_shutdown:
                    try:
                        await process.stop(reason, sender=self)
                    except Exception as error:
                        logger.error("%r killing %s: %s", error, process, reason)

        for process in list(self.monitors):
            process.demonitor(self)

        try:
            from .runtime import Runtime

            Runtime.current().unregister(self)
        except RuntimeError:
            pass

        logger.debug("%s terminated reason=%s", self, reason)

    def link(self, other: "Process"):
        self.links.add(other)
        other.links.add(self)

    def unlink(self, other: "Process"):
        self.links.discard(other)
        other.links.discard(self)

    def monitor(self, other: "Process"):
        self.monitors.add(other)
        other.monitored_by.add(self)

    def demonitor(self, other: "Process"):
        self.monitors.discard(other)
        other.monitored_by.discard(self)

    async def send(self, message: t.Any):
        assert self._inbox is not None
        await self._inbox.send(message)

    def send_nowait(self, message: t.Any):
        assert self._inbox is not None
        self._inbox.send_nowait(message)

    async def stop(
        self,
        reason: t.Any = "normal",
        timeout: float | None = 60,
        sender: "Process | None" = None,
    ):
        if self.has_stopped():
            return

        logger.debug("%s stop %s", self, reason)
        self.send_nowait(Stop(sender or self.supervisor, reason))
        with fail_after(timeout):
            await self.stopped()

    async def kill(self):
        if self.has_stopped():
            return

        logger.debug("%s kill", self)
        if self._mailbox is not None:
            await self._mailbox.aclose()
        await self.stopped()

    def info(self, message: t.Any, sender: "Process | None" = None):
        sender = sender or self.supervisor
        self.send_nowait(Info(sender, message))

    async def handle_info(self, message: Info) -> Continue | None:
        return None

    async def loop(self, *args, **kwargs):
        try:
            await self._init(*args, **kwargs)
        except (Crashed, Shutdown) as error:
            self._crash_exc = error
            self._started.set()
            self._stopped.set()
            return
        await self._loop()

    async def _run_in_process_context[R](
        self,
        func: t.Callable[..., t.Awaitable[R]],
        *args,
        **kwargs,
    ) -> R:
        from .runtime import _current_process

        token = _current_process.set(self)
        try:
            return await func(*args, **kwargs)
        finally:
            _current_process.reset(token)

    def _set_pending_continue(self, result: t.Any):
        if isinstance(result, Continue):
            self._pending_continue = result

    async def _loop(self):
        if self._mailbox is None:
            return

        reason: t.Any = "normal"

        try:
            async with self._mailbox:
                self._started.set()
                logger.debug("%s loop started", self)
                while True:
                    if self._pending_continue is not None:
                        continuation = self._pending_continue
                        self._pending_continue = None
                        await self._run_in_process_context(
                            self.handle_continue,
                            continuation.term,
                        )
                        continue

                    try:
                        message = await self._mailbox.receive()
                        logger.debug("%s received message %s", self, message)
                    except (EndOfStream, ClosedResourceError):
                        break

                    await self._run_in_process_context(self._handle_message, message)
        except ClosedResourceError:
            reason = "killed"
        except Shutdown as error:
            reason = error.reason
            if not _is_normal_shutdown_reason(reason):
                self._crash_exc = (
                    reason if isinstance(reason, Exception) else Crashed(str(reason))
                )
        except Exception as error:
            self._crash_exc = error
            reason = error
        finally:
            await self.terminate(reason)
            self._stopped.set()

    async def _handle_message(self, message: Message) -> bool:
        match message:
            case Stop(_, reason):
                raise Shutdown(reason)
            case Exit(_, reason):
                if not self.trap_exits and not _is_normal_shutdown_reason(reason):
                    if message.sender is not None:
                        self.unlink(message.sender)
                    raise Shutdown(reason)
                await self.handle_exit(message)
            case Down():
                await self.handle_down(message)
            case Info():
                result = await self.handle_info(message)
                self._set_pending_continue(result)
            case _:
                logger.warning("%s dropped unexpected message %r", self, message)
        return True

    @classmethod
    async def start(
        cls,
        *args,
        trap_exits=False,
        supervisor: "Supervisor | None" = None,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        **kwargs,
    ) -> t.Self:
        from .runtime import Runtime

        runtime = Runtime.current()
        supervisor = supervisor or runtime.supervisor

        process = cls(supervisor=supervisor, trap_exits=trap_exits)
        return await runtime.spawn(process, *args, name=name, via=via, **kwargs)

    @classmethod
    async def start_link(
        cls,
        *args,
        trap_exits=False,
        supervisor: "Supervisor | None" = None,
        name: str | None = None,
        via: tuple[str, t.Any] | None = None,
        **kwargs,
    ) -> t.Self:
        from .runtime import Runtime

        runtime = Runtime.current()
        supervisor = supervisor or runtime.supervisor

        process = cls(supervisor=supervisor, trap_exits=trap_exits)
        if supervisor is not None:
            process.link(supervisor)
        return await runtime.spawn(process, *args, name=name, via=via, **kwargs)
