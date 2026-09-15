# SPDX-FileCopyrightText: 2026 The CTRLRun contributors
# SPDX-License-Identifier: Apache-2.0
"""OpenClaw's approval prompt, as a `FrameworkInterrupt`. SPEC-v0.5 §2.1.

OpenClaw has a human-in-the-loop primitive of its own: a `before_tool_call` hook returns
`requireApproval`, the host parks the tool call, asks the operator through whichever channel
the session is bound to, and calls `onResolution` with `allow-once`, `allow-always`, `deny`,
`timeout` or `cancelled`. That is the whole reason this is an adapter rather than a wrapper:
the approval is asked and answered in the surface the operator is already looking at.

Two things about it are worth stating plainly, because both are load-bearing.

**The interrupt cannot block inside the hook.** OpenClaw's standard runner gives
`before_tool_call` 15 seconds and fails closed on expiry, so a `wait()` that blocked there
would deny every approval a human took longer than fifteen seconds to answer. So the hook
returns immediately with `requireApproval`, the host owns the waiting, and `interrupt()`
blocks here on the worker thread instead, on a verdict the plugin posts back to
`/v1/resolve`. The grant is still written in exactly one place, by
`InterruptApprovalProvider`, never by this module.

**`approved_arguments` come from the host's snapshot, never from the request.** The kernel is
explicit that handing back `PendingApproval.arguments` is manufacturing the check. OpenClaw
freezes its own parameter snapshot when it raises the approval, and the plugin sends that
snapshot back with the verdict. It is the host's record of what the human looked at, which is
the only thing that makes the binding check mean anything.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from collections.abc import Callable

from ctrlrun.adapter import ApprovalAnswer, PendingApproval

from .call import Call

_LOG: Final = logging.getLogger("ctrlrun_openclaw.interrupt")

#: What `grant_approval` writes on the record. A channel, never a person: OpenClaw's
#: `onResolution` says a decision arrived, not who made it, and SPEC-v0.3 §13 puts
#: authenticating the approver out of scope.
APPROVER_CHANNEL: Final = "openclaw:approval"


class OpenClawInterrupt:
    """`requireApproval` and `onResolution`, in the shape SPEC-v0.5 §2.1 asks for."""

    framework: Final = "openclaw"

    #: OpenClaw freezes the parameter snapshot the approval was raised against and the plugin
    #: returns it with the verdict, so the binding check is mandatory here. If a host version
    #: is found that cannot return a distinct snapshot, this must become `False` rather than
    #: echoing the request back: the conformance kit then reports `binding: not_applicable`
    #: with the reason, permanently, which is the honest outcome and the one worth avoiding.
    carries_approved_arguments: Final = True

    def __init__(self, current: Callable[[], Call], timeout: float) -> None:
        #: The call being decided on this worker thread. One `Control` serves every tool call
        #: in the install, so the provider holds one interrupt and the interrupt asks which
        #: call it is inside. A thread-local, because `Control.execute` is synchronous and the
        #: worker owns its thread for the length of the call.
        self._current = current
        self._timeout = timeout

    def interrupt(self, pending: PendingApproval) -> ApprovalAnswer:
        """Publish the request to the waiting hook, then block for the operator's answer."""
        _LOG.info(
            "approval %s requested for %s (agent %s)",
            pending.request_id,
            pending.action,
            pending.agent,
        )
        granted, approver, approved = self._current().await_verdict(self._timeout)
        return ApprovalAnswer(
            granted=granted,
            approver=approver or APPROVER_CHANNEL,
            approved_arguments=approved,
        )


def describe(pending: PendingApproval) -> dict[str, Any]:
    """What the plugin puts in the host's approval card.

    Deliberately the request's own fields and nothing invented: the action, the arguments the
    request was recorded against, the resource, and who is asking.
    """
    return {
        "requestId": pending.request_id,
        "action": pending.action,
        "actionHash": pending.action_hash,
        "arguments": pending.arguments,
        "resource": pending.resource,
        "environment": pending.environment,
        "agent": pending.agent,
        "user": pending.user,
        "expiresAt": pending.expires_at.isoformat(),
    }
