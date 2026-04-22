"""Exception hierarchy — `Crashed` (init/runtime errors), `Failed` (supervisor
failures like already_started / max restart intensity), `Shutdown` (normal-exit
sentinel).

`is_normal_shutdown_reason` is the single source of truth for what counts as a
normal exit (`"normal"`, `"shutdown"`, or `Shutdown(...)`). Everything else cascades.
See `src/fastactor/otp/README.md#exceptions`.
"""

import typing as t


class ProcessException(Exception):
    def __init__(self, reason: t.Any):
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return str(self.reason)


class Crashed(ProcessException):
    pass


class Failed(ProcessException):
    pass


class Shutdown(ProcessException):
    pass


def is_normal_shutdown_reason(reason: t.Any) -> bool:
    match reason:
        case "normal" | "shutdown":
            return True
        case Shutdown():
            return True
        case _:
            return False
