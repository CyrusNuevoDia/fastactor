"""Run N voter agents until one option reaches consensus.

Why actors matter here: cancelling a task group only cancels caller
coroutines, while the voter processes themselves keep running. Calling
`dsup.stop("normal")` tears those processes down with their `terminate()`
hooks guaranteed to run, so cleanup is explicit and reliable. Bare
`asyncio.wait(FIRST_COMPLETED)` only cancels tasks and can leave stateful
resources dangling.
"""

import anyio
import pytest

from e2e.helpers import AgentReply, Ask, FakeAgent, poll_value
from fastactor.otp import Call, DynamicSupervisor, GenServer

pytestmark = pytest.mark.anyio


class Tallier(GenServer):
    async def init(self, *, threshold: int) -> None:
        self.threshold = threshold
        self.votes: dict[str, int] = {}

    async def handle_call(self, call: Call) -> str | None:
        vote: str = call.message
        self.votes[vote] = self.votes.get(vote, 0) + 1
        for option, count in self.votes.items():
            if count >= self.threshold:
                return option
        return None


async def _spawn_voter(
    dsup: DynamicSupervisor,
    *,
    label: str,
    answer: str,
    latency: float,
    fail_after: int | None,
    freed: set[str],
) -> FakeAgent:
    return await dsup.start_child(
        dsup.child_spec(
            label,
            FakeAgent,
            kwargs={
                "label": label,
                "handler": lambda _prompt, choice=answer: choice,
                "latency": latency,
                "fail_after": fail_after,
                "freed": freed,
            },
        )
    )


async def test_majority_wins_and_remaining_voters_are_terminated(runtime):
    """G: 5 voters with fixed answers. W: option_A reaches threshold first. T: all terminate() hooks ran."""
    freed: set[str] = set()
    tallier = await Tallier.start(threshold=3)
    dsup = await DynamicSupervisor.start()

    configs = [
        ("voter-0", "option_A", 0.05, None),
        ("voter-1", "option_A", 0.10, None),
        ("voter-2", "option_B", 0.15, None),
        ("voter-3", "option_A", 0.20, None),
        ("voter-4", "option_B", 0.30, None),
    ]
    voters = {
        label: await _spawn_voter(
            dsup,
            label=label,
            answer=answer,
            latency=latency,
            fail_after=fail_after,
            freed=freed,
        )
        for label, answer, latency, fail_after in configs
    }
    winner: str | None = None

    async with anyio.create_task_group() as tg:

        async def vote_coro(label: str, voter: FakeAgent) -> None:
            nonlocal winner
            try:
                reply: AgentReply = await voter.call(Ask(prompt="cast your vote"))
            except BaseException:
                return

            result = await tallier.call(reply.text)
            if result is not None and winner is None:
                winner = result
                tg.cancel_scope.cancel()

        for label, voter in voters.items():
            tg.start_soon(vote_coro, label, voter)

    await dsup.stop("normal")
    await tallier.stop("normal")

    expected_freed = set(voters)
    await poll_value(lambda: freed.copy(), lambda labels: labels == expected_freed)

    assert winner == "option_A"
    assert freed == expected_freed


async def test_voter_crash_does_not_block_consensus(runtime):
    """G: 5 voters and one crashes first. W: option_A still reaches threshold. T: crashed voter was recorded and freed."""
    freed: set[str] = set()
    crashes: list[str] = []
    tallier = await Tallier.start(threshold=3)
    dsup = await DynamicSupervisor.start()

    configs = [
        ("voter-0", "option_A", 0.05, None),
        ("voter-1", "option_A", 0.10, None),
        ("voter-2", "option_B", 0.01, 0),
        ("voter-3", "option_A", 0.15, None),
        ("voter-4", "option_B", 0.20, None),
    ]
    voters = {
        label: await _spawn_voter(
            dsup,
            label=label,
            answer=answer,
            latency=latency,
            fail_after=fail_after,
            freed=freed,
        )
        for label, answer, latency, fail_after in configs
    }
    winner: str | None = None

    async with anyio.create_task_group() as tg:

        async def vote_coro(label: str, voter: FakeAgent) -> None:
            nonlocal winner
            try:
                reply: AgentReply = await voter.call(Ask(prompt="cast your vote"))
            except Exception:
                crashes.append(label)
                return

            result = await tallier.call(reply.text)
            if result is not None and winner is None:
                winner = result
                tg.cancel_scope.cancel()

        for label, voter in voters.items():
            tg.start_soon(vote_coro, label, voter)

    await dsup.stop("normal")
    await tallier.stop("normal")

    expected_freed = set(voters)
    await poll_value(lambda: freed.copy(), lambda labels: labels == expected_freed)

    assert winner == "option_A"
    assert "voter-2" in crashes
    assert "voter-2" in freed
