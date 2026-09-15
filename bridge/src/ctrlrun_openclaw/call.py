# SPDX-FileCopyrightText: 2026 The CTRLRun contributors
# SPDX-License-Identifier: Apache-2.0
"""One OpenClaw tool call, tracked across the three requests the host makes about it.

OpenClaw decides and executes in different places. `before_tool_call` is a fail-closed gate
with a 15-second budget, the host runs the tool itself, and `after_tool_call` observes the
result. CTRLRun's kernel wants one call: `Control.execute(action, executor)`, where the
executor performs the effect and its return or its exception *is* the outcome.

This class is the join. A worker thread holds `execute` open for the whole tool call; the
executor it passes in blocks here, and the host's three HTTP requests drive it:

    POST /v1/decide   -> the worker starts; the executor is entered (ALLOW), or `execute`
                         raises (DENY), or an approval request is published (APPROVE)
    POST /v1/resolve  -> a human's answer arrives; the second pass reserves and enters
    POST /v1/outcome  -> the executor returns, raises `NotExecuted`, or raises anything else

**The third request is the one that matters, and the one the host does not guarantee.**
`after_tool_call` is an observation hook: OpenClaw's own reference says handlers run
concurrently, return values are ignored, and "for a policy requirement, use a fail-closed gate
rather than assuming an observation ... hook will reject the operation on failure". So an
outcome that never arrives is not an error here. It is `AMBIGUOUS`, the effect record stays
reserved, and the next attempt on the same key is refused until a human runs `ctrlrun resolve`.
That is the one guarantee a decision-only guardrail cannot offer, and it is why this adapter
holds `execute` open instead of calling `evaluate` and walking away.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Final

from ctrlrun import Action, NotExecuted


#: What the executor raises when the host never said what happened. Anything that is not
#: `NotExecuted` lands the effect in `AMBIGUOUS` (SPEC-v0.1 §5.5), which is the point.
class OutcomeNotReported(Exception):
    """`after_tool_call` never arrived, or arrived saying the host does not know."""


#: The answer `/v1/decide` and `/v1/resolve` return to the plugin.
ALLOW: Final = "allow"
DENY: Final = "deny"
APPROVAL: Final = "approval"


@dataclass(frozen=True)
class Answer:
    """What the plugin turns into a `PluginHookBeforeToolCallResult`."""

    decision: str
    reason: str | None = None
    request_id: str | None = None
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": self.decision}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.request_id is not None:
            body["requestId"] = self.request_id
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class Call:
    """The state of one tool call while `Control.execute` is held open for it."""

    def __init__(
        self, call_id: str, tool: str, arguments: dict[str, Any], outcome_timeout: float
    ) -> None:
        self.call_id = call_id
        self.tool = tool
        self.arguments = arguments
        #: Built on the worker thread, under the operator's identity context. The principal is
        #: an authorization input (SPEC-v0.3 §4.2), so it is resolved where that context is
        #: entered and never from anything the host sent.
        self.action: Action | None = None
        self.effect_key: str | None = None
        self.receipt: Any = None
        self.error: BaseException | None = None
        self._outcome_timeout = outcome_timeout
        # Two gates, because two different HTTP requests wait for an answer about this call:
        # the first pass answers `/v1/decide`, the pass after a human answers `/v1/resolve`.
        self._gates = (threading.Event(), threading.Event())
        self._answers: list[Answer | None] = [None, None]
        self._pass = 0
        self._verdict: queue.Queue[tuple[bool, str, dict[str, Any] | None]] = queue.Queue(maxsize=1)
        self._outcome: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=1)

    # -- the executor CTRLRun calls -------------------------------------------------

    def executor(self) -> None:
        """Entered only when the action is allowed and its effect is reserved.

        Entering is itself the decision: it is what tells the waiting `/v1/decide` (or
        `/v1/resolve`) that the host may run the tool. Then it blocks until the host says
        what happened, because that is the half `evaluate()` alone throws away.
        """
        self._publish(Answer(ALLOW))
        try:
            status, detail = self._outcome.get(timeout=self._outcome_timeout)
        except queue.Empty:
            raise OutcomeNotReported(
                f"OpenClaw never reported an outcome for {self.tool} within "
                f"{self._outcome_timeout:g}s. The effect is reserved and unresolved; "
                f"'ctrlrun resolve' is the only way out."
            ) from None
        if status == "ok":
            return None
        if status == "error":
            # The one claim that asserts non-execution. The host only makes it when the tool
            # itself raised before reaching anything consequential.
            raise NotExecuted(detail or "the tool reported an error")
        raise OutcomeNotReported(detail or "the host does not know what happened")

    # -- what the HTTP handlers drive -----------------------------------------------

    def _publish(self, answer: Answer) -> None:
        self._answers[self._pass] = answer
        self._gates[self._pass].set()

    def publish(self, answer: Answer) -> None:
        self._publish(answer)

    def advance(self) -> None:
        """Move to the second pass: a human answered and `execute` is about to run again."""
        self._pass = 1

    def await_answer(self, which: int, timeout: float) -> Answer:
        if not self._gates[which].wait(timeout):
            return Answer(DENY, reason="ctrlrun_bridge_timeout")
        answer = self._answers[which]
        return answer if answer is not None else Answer(DENY, reason="ctrlrun_bridge_no_answer")

    def verdict(
        self, granted: bool, approver: str, approved_arguments: dict[str, Any] | None
    ) -> None:
        self._verdict.put((granted, approver, approved_arguments))

    def await_verdict(self, timeout: float) -> tuple[bool, str, dict[str, Any] | None]:
        return self._verdict.get(timeout=timeout)

    def outcome(self, status: str, detail: str | None) -> None:
        self._outcome.put((status, detail))
