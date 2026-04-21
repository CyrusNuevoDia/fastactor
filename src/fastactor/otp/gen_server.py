import logging
import typing as t

from ._exceptions import Shutdown
from ._messages import Call, Cast, Continue, Info, Stop
from .process import Process

logger = logging.getLogger(__name__)


class GenServer[Req = t.Any, Rep = t.Any](Process):
    async def handle_call(
        self, call: Call[Req, Rep]
    ) -> Rep | tuple[Rep, Continue] | tuple[Rep, Stop] | None:
        return None

    async def handle_cast(self, cast: Cast[Req]) -> Continue | None:
        return None

    async def handle_info(self, message: Info) -> Continue | None:
        return None

    async def call(
        self,
        request: Req,
        sender: Process | None = None,
        timeout: float = 5,
    ) -> Rep:
        from .runtime import _current_process

        sender = sender or _current_process.get() or self.supervisor
        callmsg: Call[Req, Rep] = Call(sender, request)
        self.send_nowait(callmsg)
        return await callmsg.result(timeout)

    def cast(self, request: Req, sender: Process | None = None) -> None:
        from .runtime import _current_process

        sender = sender or _current_process.get() or self.supervisor
        self.send_nowait(Cast(sender, request))

    async def _handle_message(self, message: t.Any) -> bool:
        match message:
            case Call():
                try:
                    reply = await self.handle_call(message)
                    match reply:
                        case None:
                            # NoReply: caller will resolve via message.set_result(...) later
                            pass
                        case (value, Continue() as continuation):
                            self._pending_continue = continuation
                            message.set_result(value)
                        case (value, Stop() as stop):
                            message.set_result(value)
                            raise Shutdown(stop.reason)
                        case _:
                            message.set_result(reply)
                    return True
                except Shutdown:
                    raise
                except Exception as error:
                    message.set_result(error)
                    logger.error(
                        "%s encountered %r processing %s",
                        self,
                        error,
                        message,
                    )
                    raise
            case Cast():
                try:
                    result = await self.handle_cast(message)
                    if isinstance(result, Continue):
                        self._pending_continue = result
                    return True
                except Exception as error:
                    logger.error(
                        "%s encountered %r processing %s",
                        self,
                        error,
                        message,
                    )
                    raise
            case _:
                return await super()._handle_message(message)
