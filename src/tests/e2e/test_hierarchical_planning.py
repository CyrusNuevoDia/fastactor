"""Hierarchical planning with planner, parallel researchers, and a shared results actor.

Why this is uniquely actor-hard: decomposition, fan-out work, shared-state
aggregation, and synthesis each want different lifecycles. A planner should be
short-lived, researchers should run concurrently and fail independently, the
shared results process should serialize writes without locks, and the
synthesizer should consume whatever survived. In plain asyncio you'd hand-roll
task orchestration, locking, and cleanup at every call-site.
"""

import json
from typing import cast

import anyio
import pytest

from e2e.helpers import AgentReply, Ask, FakeAgent, poll_value
from fastactor.otp import Agent, DynamicSupervisor

pytestmark = pytest.mark.anyio


async def _spawn_researcher(
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
                "handler": lambda _prompt, result=answer: result,
                "latency": latency,
                "fail_after": fail_after,
                "freed": freed,
            },
        )
    )


async def test_plan_execute_synthesize(runtime):
    """G: planner decomposes a research task. W: researchers run in parallel and synthesizer combines findings. T: all findings are gathered and actors terminate cleanly."""
    freed: set[str] = set()
    planner = await FakeAgent.start(
        label="planner",
        handler=lambda p: json.dumps(["history", "applications", "limitations"]),
        latency=0.05,
        fail_after=None,
        freed=freed,
    )

    plan_reply = await planner.call(Ask(prompt="Research: AI agents"))
    topics = json.loads(plan_reply.text)
    await planner.stop("normal")

    results_agent = await Agent.start(lambda: [])
    dsup = await DynamicSupervisor.start()
    researchers = [
        await _spawn_researcher(
            dsup,
            label=f"researcher-{topic}",
            answer=f"findings on {topic}",
            latency=0.05,
            fail_after=None,
            freed=freed,
        )
        for topic in topics
    ]

    async with anyio.create_task_group() as tg:

        async def run(researcher: FakeAgent, topic: str) -> None:
            reply: AgentReply = await researcher.call(Ask(prompt=f"Research {topic}"))
            await results_agent.update(lambda s, r=reply: [*s, r])

        for researcher, topic in zip(researchers, topics, strict=True):
            tg.start_soon(run, researcher, topic)

    findings = cast(
        list[AgentReply],
        await poll_value(
            lambda: results_agent.get(lambda s: list(s)),
            lambda s: len(s) == 3,
        ),
    )

    synthesizer = await FakeAgent.start(
        label="synthesizer",
        handler=lambda p: f"Summary: {p}",
        latency=0.05,
        fail_after=None,
        freed=freed,
    )
    context = " | ".join(f.text for f in findings)
    summary: AgentReply = await synthesizer.call(Ask(prompt=context))

    await dsup.stop("normal")
    await synthesizer.stop("normal")
    await results_agent.stop("normal")

    assert len(findings) == 3
    assert sorted(f.text for f in findings) == [
        "findings on applications",
        "findings on history",
        "findings on limitations",
    ]
    assert summary.text == f"Summary: {context}"
    assert freed >= {
        "planner",
        "synthesizer",
        "researcher-history",
        "researcher-applications",
        "researcher-limitations",
    }


async def test_one_researcher_fails_partial_results_synthesized(runtime):
    """G: one researcher crashes while others succeed. W: partial findings are accumulated. T: synthesis still completes from surviving results."""
    freed: set[str] = set()
    results_agent = await Agent.start(lambda: [])
    dsup = await DynamicSupervisor.start()

    topics = ["history", "applications", "limitations"]
    researchers = {
        topic: await _spawn_researcher(
            dsup,
            label=f"researcher-{topic}",
            answer=f"findings on {topic}",
            latency=0.02,
            fail_after=0 if topic == "applications" else None,
            freed=freed,
        )
        for topic in topics
    }
    errors: dict[str, Exception] = {}

    async with anyio.create_task_group() as tg:

        async def run(topic: str, researcher: FakeAgent) -> None:
            try:
                reply: AgentReply = await researcher.call(
                    Ask(prompt=f"Research {topic}")
                )
                await results_agent.update(lambda s, r=reply: [*s, r])
            except Exception as exc:
                errors[topic] = exc

        for topic, researcher in researchers.items():
            tg.start_soon(run, topic, researcher)

    findings = cast(
        list[AgentReply],
        await poll_value(
            lambda: results_agent.get(lambda s: list(s)),
            lambda s: len(s) == 2,
        ),
    )

    synthesizer = await FakeAgent.start(
        label="synthesizer",
        handler=lambda p: f"Summary: {p}",
        latency=0.05,
        fail_after=None,
        freed=freed,
    )
    context = " | ".join(f.text for f in findings) or "no findings"
    summary: AgentReply = await synthesizer.call(Ask(prompt=context))

    await dsup.stop("normal")
    await synthesizer.stop("normal")
    await results_agent.stop("normal")

    assert len(findings) == 2
    assert sorted(f.text for f in findings) == [
        "findings on history",
        "findings on limitations",
    ]
    assert "applications" in errors
    assert summary.text == f"Summary: {context}"
    assert "researcher-applications" in freed
