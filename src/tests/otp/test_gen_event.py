"""OTP conformance suite — §7 gen_event.

See `SPEC.md` §7 for the source claims. The port does not yet ship a
gen_event module (`src/fastactor/otp/gen_event.py` is absent), so every claim
is a skipped stub. When the module lands, fill in the bodies and remove the
file-level skip.
"""

import pytest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skip(reason="gen_event module not implemented in port"),
]


async def test_7_1_event_manager_dispatches_to_every_handler() -> None:
    """SPEC §7.1: An event is dispatched to every installed handler.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html
    """


async def test_7_2_add_handler_calls_init() -> None:
    """SPEC §7.2: add_handler/3 invokes Handler:init/1.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html#add_handler/3
    """


async def test_7_2_delete_handler_calls_terminate() -> None:
    """SPEC §7.2: delete_handler/3 invokes Handler:terminate/2.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html#delete_handler/3
    """


async def test_7_2_swap_handler_calls_terminate_then_init() -> None:
    """SPEC §7.2: swap_handler calls Old:terminate then New:init({NewArgs, TerminateResult}).

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html#swap_handler/3
    """


async def test_7_3_crashed_handler_is_removed_manager_survives() -> None:
    """SPEC §7.3: A crashing handler is removed; the gen_event manager stays alive.

    Source: https://www.erlang.org/doc/apps/stdlib/gen_event.html
    """
