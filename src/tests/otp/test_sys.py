"""OTP conformance suite — §9 sys protocol.

See `SPEC.md` §9 for the source claims. The port ships no `sys` module;
there is no equivalent of sys:get_state/suspend/resume/replace_state. Every claim
is a skipped stub — fill in bodies once the system-message protocol exists.
"""

import pytest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skip(reason="sys protocol not implemented in port"),
]


async def test_9_1_get_state_returns_state_without_invoking_callbacks() -> None:
    """SPEC §9.1: sys.get_state/1 returns the current state without calling user callbacks.

    Source: https://www.erlang.org/doc/apps/stdlib/sys.html#get_state/1
    """


async def test_9_2_suspend_pauses_message_processing() -> None:
    """SPEC §9.2: sys.suspend/1 pauses the process; queued messages are not dispatched.

    Source: https://www.erlang.org/doc/apps/stdlib/sys.html#suspend/1
    """


async def test_9_2_resume_drains_queued_messages() -> None:
    """SPEC §9.2: sys.resume/1 resumes the process and drains any queued messages.

    Source: https://www.erlang.org/doc/apps/stdlib/sys.html#resume/1
    """


async def test_9_3_replace_state_applies_fun_atomically() -> None:
    """SPEC §9.3: sys.replace_state/2 applies a transform to the current state.

    Source: https://www.erlang.org/doc/apps/stdlib/sys.html#replace_state/2
    """
