import typing as t


class Crashed(Exception):
    def __init__(self, reason: t.Any):
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return str(self.reason)


class Failed(Exception):
    def __init__(self, reason: t.Any):
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return str(self.reason)


class Shutdown(Exception):
    def __init__(self, reason: t.Any):
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return str(self.reason)


def _is_normal_shutdown_reason(reason: t.Any) -> bool:
    match reason:
        case "normal" | "shutdown":
            return True
        case Shutdown():
            return True
        case _:
            return False
