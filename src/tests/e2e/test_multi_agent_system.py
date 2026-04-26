"""Five role-named agents collaborate in a shared channel; QA crashes once.

Why actors matter here: each agent is a real Process with its own mailbox, can
crash without taking the others down, and gets restarted by a Supervisor under
`permanent` policy. The Channel is a GenServer that owns thread state; the
subscriber set is a `Registry` in duplicate mode, so termination auto-scrubs
membership without bookkeeping. Replies are fire-and-forget casts; the final
transcript is a synchronous call. With plain async tasks none of this would
compose: a crashed coroutine takes its peers down, there is nowhere to put
shared thread state, and no one is responsible for restart.

OTP pieces showcased:
  - GenServer (Channel + every agent)
  - Supervisor / one_for_one / permanent restart (QA crashes once, comes back)
  - Registry (duplicate mode) as a pub/sub subscriber set
  - handle_cast (Post), handle_call (Snapshot), handle_info (broadcast notify)
  - Frozen-dataclass message envelopes
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

import anyio
import pytest

from fastactor.otp import Call, Cast, GenServer, Info, Process, Registry, Supervisor

pytestmark = pytest.mark.anyio

SUBSCRIBERS = "channel.subscribers"


# --- Message envelopes -----------------------------------------------------


@dataclass(frozen=True)
class Post:
    author: str
    text: str
    mentions: tuple[str, ...] = ()
    thread_id: str | None = None  # None = new top-level thread


@dataclass(frozen=True)
class ThreadMessage:
    seq: int
    thread_id: str
    author: str
    text: str
    mentions: tuple[str, ...]
    is_root: bool


@dataclass(frozen=True)
class Snapshot:
    pass


# --- Channel ---------------------------------------------------------------


class Channel(GenServer):
    async def init(self, *, init_count: dict[str, int]) -> None:
        self.threads: dict[str, list[ThreadMessage]] = {}
        self._seq = 0
        await Registry.new(SUBSCRIBERS, mode="duplicate")
        init_count["channel"] = init_count.get("channel", 0) + 1
        print("[init] channel")

    async def handle_cast(self, cast: Cast) -> None:
        post: Post = cast.message
        thread_id = post.thread_id or f"t-{uuid.uuid4().hex[:6]}"
        self._seq += 1
        msg = ThreadMessage(
            seq=self._seq,
            thread_id=thread_id,
            author=post.author,
            text=post.text,
            mentions=post.mentions,
            is_root=post.thread_id is None,
        )
        self.threads.setdefault(thread_id, []).append(msg)

        prefix = "" if msg.is_root else "  └─ "
        tag = (" " + " ".join(f"@{m}" for m in msg.mentions)) if msg.mentions else ""
        print(f"{prefix}[{msg.author:>9}]{tag} {msg.text}")

        async def notify(proc: Process) -> None:
            proc.info(msg)

        await Registry.dispatch(SUBSCRIBERS, "all", notify)

    async def handle_call(self, call: Call) -> Any:
        if isinstance(call.message, Snapshot):
            return {tid: list(msgs) for tid, msgs in self.threads.items()}
        return None


# --- Agent base + 5 roles --------------------------------------------------


class TeamAgent(GenServer):
    role: ClassVar[str] = ""
    keywords: ClassVar[tuple[str, ...]] = ()
    replies: ClassVar[tuple[str, ...]] = ()
    stagger_index: ClassVar[int] = 0

    async def init(
        self,
        *,
        channel: Channel,
        init_count: dict[str, int],
    ) -> None:
        self.channel = channel
        self._replied: set[str] = set()
        await Registry.register(SUBSCRIBERS, "all", self)
        init_count[self.role] = init_count.get(self.role, 0) + 1
        print(f"[init] {self.role}")

    async def handle_info(self, message: Info) -> None:
        msg = message.message
        if not isinstance(msg, ThreadMessage) or msg.author == self.role:
            return
        self._maybe_crash(msg)
        if not self._should_reply(msg) or msg.thread_id in self._replied:
            return
        self._replied.add(msg.thread_id)
        await anyio.sleep(0.05 + 0.03 * self.stagger_index)
        text = self.replies[hash(msg.thread_id) % len(self.replies)]
        self.channel.cast(Post(author=self.role, text=text, thread_id=msg.thread_id))

    def _should_reply(self, msg: ThreadMessage) -> bool:
        if self.role in msg.mentions:
            return True
        if msg.is_root and any(k in msg.text.lower() for k in self.keywords):
            return True
        return False

    def _maybe_crash(self, msg: ThreadMessage) -> None:
        return

    async def terminate(self, reason: Any) -> None:
        if reason not in ("normal", "shutdown"):
            print(f"[exit] {self.role}: {reason!r}")
        await super().terminate(reason)


class GTMAgent(TeamAgent):
    role = "gtm"
    keywords = ("launch", "go-to-market", "positioning", "campaign", "messaging")
    replies = (
        "We need to nail positioning before code freeze.",
        "Aligning with the Q3 launch calendar — flag any slips early.",
        "Let me draft the announcement copy this week.",
    )
    stagger_index = 0


class EngineerAgent(TeamAgent):
    role = "eng"
    keywords = ("api", "performance", "bug", "implementation", "scale", "engineering")
    replies = (
        "I can prototype on a feature branch by Friday.",
        "Performance budget: <200ms p95 — will benchmark before merge.",
        "Risk: we're touching the auth path, needs careful review.",
    )
    stagger_index = 1


class DesignerAgent(TeamAgent):
    role = "designer"
    keywords = ("ux", "redesign", "mock", "wireframe", "landing page", "design")
    replies = (
        "Drafting Figma mocks now — share by EOD tomorrow.",
        "Concerned about contrast on the hero — will A/B two variants.",
        "Need a copy hand-off from GTM before final pass.",
    )
    stagger_index = 2


class QAAgent(TeamAgent):
    role = "qa"
    keywords = ("regression", "test", "release", "blocker", "rollout")
    replies = (
        "Need a written test plan before staging.",
        "Recommend a feature-flagged rollout with 1% canary.",
        "I'll draft regression coverage — flag any auth/payment touchpoints.",
    )
    stagger_index = 3

    async def init(  # ty: ignore[invalid-method-override]  # type: ignore[override]
        self,
        *,
        channel: Channel,
        init_count: dict[str, int],
        crash_box: list[bool],
    ) -> None:
        self.crash_box = crash_box
        await super().init(channel=channel, init_count=init_count)

    def _maybe_crash(self, msg: ThreadMessage) -> None:
        if msg.is_root and not self.crash_box[0]:
            self.crash_box[0] = True
            raise RuntimeError("qa simulated mid-handler crash")


class SDRAgent(TeamAgent):
    role = "sdr"
    keywords = ("customer", "outbound", "demo", "lead", "sales", "rollout")
    replies = (
        "Three customers asked about this last week — strong signal.",
        "I can line up three demo calls once design is shippable.",
        "Outbound list is queued; will hold until launch date is firm.",
    )
    stagger_index = 4


# --- Test ------------------------------------------------------------------


AGENT_CLASSES: tuple[type[TeamAgent], ...] = (
    GTMAgent,
    EngineerAgent,
    DesignerAgent,
    QAAgent,
    SDRAgent,
)

SCRIPT: list[tuple[float, str, str, tuple[str, ...]]] = [
    (
        0.0,
        "PM",
        "We need a landing page redesign for the Q3 launch — what should we focus on?",
        ("designer", "gtm"),
    ),
    (1.5, "PM", "What are the engineering risks here?", ("eng",)),
    (1.5, "PM", "Any regression-testing or customer-rollout concerns?", ("qa", "sdr")),
]


async def test_multi_agent_team_collaborates_through_a_supervised_channel(
    make_supervisor,
):
    init_count: dict[str, int] = {}
    crash_box: list[bool] = [False]

    sup = await make_supervisor(
        strategy="one_for_one", max_restarts=10, max_seconds=5.0
    )
    channel: Channel = await sup.start_child(
        Supervisor.child_spec("channel", Channel, kwargs={"init_count": init_count}),
    )
    for cls in AGENT_CLASSES:
        kwargs: dict[str, Any] = {"channel": channel, "init_count": init_count}
        if cls is QAAgent:
            kwargs["crash_box"] = crash_box
        await sup.start_child(Supervisor.child_spec(cls.role, cls, kwargs=kwargs))

    for delay, author, text, mentions in SCRIPT:
        await anyio.sleep(delay)
        channel.cast(Post(author=author, text=text, mentions=mentions))

    await anyio.sleep(2.0)  # let final replies and any restart settle

    transcript: dict[str, list[ThreadMessage]] = await channel.call(Snapshot())

    # 1. Three top-level threads — one per scripted PM post.
    roots = [m for thread in transcript.values() for m in thread if m.is_root]
    assert len(roots) == 3
    assert {r.author for r in roots} == {"PM"}

    # 2. Each thread got at least one threaded reply.
    for thread_id, msgs in transcript.items():
        assert any(not m.is_root for m in msgs), f"thread {thread_id} got no replies"

    # 3. No agent ever replied to itself within a thread.
    for msgs in transcript.values():
        seen_authors_excluding_pm: set[str] = set()
        for m in msgs:
            if m.author == "PM":
                continue
            assert m.author not in seen_authors_excluding_pm, (
                f"{m.author} replied twice in thread {m.thread_id}"
            )
            seen_authors_excluding_pm.add(m.author)

    # 4. Supervisor restart of QA is observable — and only QA restarted.
    assert crash_box[0] is True, "QA's deliberate crash never fired"
    assert init_count["qa"] == 2, (
        f"qa init_count={init_count['qa']}, expected 2 (one initial + one restart)"
    )
    for role in ("channel", "gtm", "eng", "designer", "sdr"):
        assert init_count[role] == 1, (
            f"{role} restarted unexpectedly: init_count={init_count[role]}"
        )

    # 5. QA still participated after restart (post 3 mentions @qa).
    qa_replies = [
        m for thread in transcript.values() for m in thread if m.author == "qa"
    ]
    assert qa_replies, "QA never replied — restart didn't recover the agent"

    # 6. Mention semantics: every @-mentioned agent replied in that thread.
    for root in roots:
        thread_msgs = transcript[root.thread_id]
        replied_authors = {m.author for m in thread_msgs if not m.is_root}
        for mentioned in root.mentions:
            assert mentioned in replied_authors, (
                f"{mentioned} was @-mentioned in {root.thread_id} but did not reply"
            )

    await sup.stop("normal")
