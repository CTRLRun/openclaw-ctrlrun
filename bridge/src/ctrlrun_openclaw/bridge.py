# SPDX-FileCopyrightText: 2026 The CTRLRun contributors
# SPDX-License-Identifier: Apache-2.0
"""The loopback server the OpenClaw plugin talks to.

OpenClaw is TypeScript and CTRLRun's kernel is Python, so something has to cross the process
boundary. It is a loopback HTTP server rather than a rewrite of the kernel in TypeScript for
the reason the kernel's own gateway gives: the guarantees live in one store, one policy
evaluator and one receipt chain, and a second implementation of them is a second thing to be
wrong. The plugin is a client; every decision is made here.

Three endpoints, one per thing the host can tell us:

    POST /v1/decide    `before_tool_call` is asking whether this tool call may run
    POST /v1/resolve   a human answered the approval the last `/v1/decide` raised
    POST /v1/outcome   `after_tool_call` is reporting what happened, if it fires at all
    GET  /v1/health    is the bridge up and which policy did it load

The server binds loopback only and requires a shared secret the plugin is started with. It is
not an internet-facing surface and has no business becoming one: the approval authority it
fronts is the operator's.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

from ctrlrun import (
    Action,
    context,
    ActionDenied,
    AmbiguousEffect,
    ApprovalRequired,
    Control,
    DuplicateEffect,
    banner,
    with_approval,
)
from ctrlrun.adapter import InterruptApprovalProvider, PendingApproval
from ctrlrun.effect import resolve_resource

from .call import APPROVAL, DENY, Answer, Call
from .interrupt import APPROVER_CHANNEL, OpenClawInterrupt, describe

_LOG: Final = logging.getLogger("ctrlrun_openclaw.bridge")

#: `before_tool_call`'s standard budget is 15 seconds and it fails closed. The bridge answers
#: in milliseconds on the happy path; this is the ceiling before it gives the hook a refusal
#: it can explain rather than letting the host time out with its own generic one.
DECIDE_TIMEOUT: Final = 10.0

#: How long the executor waits for `after_tool_call`. Past it the effect is AMBIGUOUS, which
#: is a real state a human resolves, not a failure mode to tune away.
OUTCOME_TIMEOUT: Final = 900.0

#: How long a parked approval may sit before the interrupt gives up. The host has its own
#: `timeoutMs` and an unresolved approval always denies there; this only bounds the thread.
APPROVAL_TIMEOUT: Final = 86_400.0

#: The host decisions that mean "run it".
_GRANTING: Final = frozenset({"allow-once", "allow-always"})


class Bridge:
    """One `Control`, and the calls currently in flight against it."""

    def __init__(
        self,
        policy_path: str | None,
        token: str,
        *,
        agent: str | None = None,
        environment: str | None = None,
        outcome_timeout: float = OUTCOME_TIMEOUT,
        approval_timeout: float = APPROVAL_TIMEOUT,
    ) -> None:
        self._token = token
        #: The agent name this install acts under, set by the **operator** on the command
        #: line. It is deliberately not `event.agentId`: a principal taken from the calling
        #: session is `--principal-from-client-info`, which SPEC-v0.3 §8.1 removed from the
        #: gateway by name and §8.4 removed again from the ACS hook. `None` where the policy
        #: configures an identity provider that answers instead.
        self._agent = agent
        self._outcome_timeout = outcome_timeout
        self._approval_timeout = approval_timeout
        self._calls: dict[str, Call] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._control = self._build(policy_path, environment, approval_timeout)
        banner(self._control)

    def _build(self, policy_path: str | None, environment: str | None, timeout: float) -> Control:
        """The operator's line: the `Control` this bridge serves.

        `Control.from_file` installs `LocalApprovalProvider`, and the whole point of an
        adapter is that the provider routes through the framework's own approval prompt
        instead. So the file is loaded the ordinary way and the result is re-homed onto this
        provider, reading every other choice -- policy, store, sinks, authority, identity,
        environment -- back off the loaded `Control` through its public properties. Nothing
        about how the operator configured CTRLRun is re-decided here, and a later `from_file`
        that learns to read something new is inherited rather than missed.
        """
        base = Control.from_file(policy_path, environment=environment)
        interrupt = OpenClawInterrupt(self.current_call, timeout)
        return Control(
            base.policy,
            base.store,
            InterruptApprovalProvider(base.store, interrupt),
            sinks=base.sinks,
            identity=base.identity,
            authority=base.authority,
            environment=base.environment,
            approver_identity=base.approver_identity,
            require_approved_policy=base.require_approved_policy,
            lease=base.lease,
        )

    @property
    def control(self) -> Control:
        return self._control

    def authorized(self, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(presented, self._token)

    def current_call(self) -> Call:
        """The call this worker thread is inside. Read by the interrupt, nothing else."""
        call: Call | None = getattr(self._local, "call", None)
        if call is None:  # pragma: no cover - only reachable if the provider is misused
            raise RuntimeError("no call in flight on this thread")
        return call

    # -- /v1/decide ------------------------------------------------------------------

    def decide(self, body: Mapping[str, Any]) -> dict[str, Any]:
        tool = body.get("tool")
        if not isinstance(tool, str) or not tool:
            return Answer(DENY, reason="ctrlrun_bad_request").to_dict() | {"callId": None}
        params = body.get("params")
        arguments: Mapping[str, Any] = params if isinstance(params, Mapping) else {}

        call_id = uuid.uuid4().hex
        call = Call(call_id, tool, dict(arguments), self._outcome_timeout)
        with self._lock:
            self._calls[call_id] = call

        worker = threading.Thread(
            target=self._run, args=(call,), name=f"ctrlrun-{call_id[:8]}", daemon=True
        )
        worker.start()
        answer = call.await_answer(0, DECIDE_TIMEOUT)
        if answer.decision != APPROVAL:
            # Nothing else will be asked about this call unless it was allowed.
            if answer.decision == DENY:
                self._forget(call_id)
        return answer.to_dict() | {"callId": call_id}

    # -- /v1/resolve -----------------------------------------------------------------

    def resolve(self, body: Mapping[str, Any]) -> dict[str, Any]:
        call_id = body.get("callId")
        call = self._find(call_id)
        if call is None:
            return Answer(DENY, reason="ctrlrun_unknown_call").to_dict()
        decision = body.get("decision")
        approved = body.get("approvedParams")
        granted = decision in _GRANTING
        call.advance()
        call.verdict(
            granted,
            str(body.get("approver") or APPROVER_CHANNEL),
            dict(approved) if isinstance(approved, Mapping) else None,
        )
        if not granted:
            self._forget(str(call_id))
            return Answer(DENY, reason=f"ctrlrun_denied: {decision}").to_dict()
        answer = call.await_answer(1, DECIDE_TIMEOUT)
        if answer.decision == DENY:
            self._forget(str(call_id))
        return answer.to_dict()

    # -- /v1/outcome -----------------------------------------------------------------

    def outcome(self, body: Mapping[str, Any]) -> dict[str, Any]:
        call_id = body.get("callId")
        call = self._find(call_id)
        if call is None:
            # The host reporting an outcome for a call we denied is normal and not an error.
            return {"recorded": False, "reason": "unknown_call"}
        status = body.get("status")
        if status not in {"ok", "error", "unknown"}:
            status = "unknown"
        call.outcome(str(status), _text(body.get("detail")))
        self._forget(str(call_id))
        return {"recorded": True}

    # -- internals -------------------------------------------------------------------

    def _run(self, call: Call) -> None:
        """Hold `Control.execute` open for the whole tool call, on this thread."""
        self._local.call = call
        try:
            with self._identity():
                call.action = self._action(call.tool, call.arguments)
                call.effect_key = self._effect_key(call.action, call.arguments)
                call.receipt = self._control.execute(call.action, call.executor, call.effect_key)
        except ApprovalRequired as pending:
            self._await_human(call, pending.request_id)
        except ActionDenied as denied:
            call.publish(Answer(DENY, reason=denied.reason))
        except DuplicateEffect as duplicate:
            # "Once stays once". The same logical effect already happened, or is happening.
            call.publish(
                Answer(
                    DENY,
                    reason=f"duplicate_effect:{duplicate.state}",
                    detail={"effectKey": duplicate.effect_key},
                )
            )
        except AmbiguousEffect as ambiguous:
            # "Unknown means wait". A previous attempt's outcome was never established.
            call.publish(
                Answer(
                    DENY,
                    reason="ambiguous_effect",
                    detail={
                        "effectKey": ambiguous.effect_key,
                        "resolveWith": f"ctrlrun resolve {ambiguous.effect_key}",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - the hook fails closed; say why in the log
            _LOG.exception("bridge failed deciding %s", call.tool)
            call.publish(Answer(DENY, reason=f"ctrlrun_error: {exc.__class__.__name__}"))
        finally:
            self._local.call = None

    def _await_human(self, call: Call, request_id: str) -> None:
        """Publish the request to the hook, wait on the provider, then re-present it.

        This is `@protect(wait=True)`'s loop, written out because the waiting happens in the
        host rather than in this process: the hook has already returned `requireApproval` by
        the time `wait()` blocks. The grant is written by `InterruptApprovalProvider`, which
        is the same `grant_approval` that `ctrlrun approve` and the webhook call.
        """
        call.publish(
            Answer(APPROVAL, request_id=request_id, detail=self._pending(request_id))
        )
        try:
            self._control.approvals.wait(request_id, None)
            with self._identity(), with_approval(request_id):
                assert call.action is not None  # built on the first pass, on this thread
                call.receipt = self._control.execute(call.action, call.executor, call.effect_key)
        except ActionDenied as denied:
            call.publish(Answer(DENY, reason=denied.reason))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("bridge failed resuming %s", call.tool)
            call.publish(Answer(DENY, reason=f"ctrlrun_error: {exc.__class__.__name__}"))

    def _pending(self, request_id: str) -> dict[str, Any] | None:
        record = self._control.store.get_approval(request_id)
        return None if record is None else describe(PendingApproval.of(record))

    def _identity(self) -> AbstractContextManager[None]:
        """The operator's agent name, entered on the worker thread.

        `Control.resolve_principal` reads the identity provider first and falls back to this
        context. Where the operator configured a provider there is nothing to enter, and the
        provider wins as SPEC-v0.3 §3.2 says it must.
        """
        return nullcontext() if self._agent is None else context(agent=self._agent)

    def _action(self, tool: str, arguments: Mapping[str, Any]) -> Action:
        """Build the action for this tool call.

        The principal comes from `Control.resolve_principal`, never from anything the host
        sent: a session-supplied principal is the one input SPEC-v0.3 §4.2 forbids, and the
        kernel exposes this method so an adapter reads it rather than inventing one.
        """
        principal = self._control.resolve_principal(tool)
        template = self._control.policy.resource_template(tool)
        return Action(
            name=tool,
            arguments=dict(arguments),
            principal=principal,
            resource=None if template is None else resolve_resource(template, arguments),
            environment=self._control.environment,
        )

    def _effect_key(self, action: Action, arguments: Mapping[str, Any]) -> str | None:
        template = self._control.policy.effect_template(action.name)
        return None if template is None else resolve_resource(template, arguments)

    def _find(self, call_id: object) -> Call | None:
        if not isinstance(call_id, str):
            return None
        with self._lock:
            return self._calls.get(call_id)

    def _forget(self, call_id: str) -> None:
        with self._lock:
            self._calls.pop(call_id, None)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:2000]


class _Handler(BaseHTTPRequestHandler):
    """Loopback JSON, and nothing clever."""

    server_version = "ctrlrun-openclaw"
    bridge: Bridge

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        _LOG.debug(fmt, *args)

    def _send(self, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/health":
            self._send(404, {"error": "not_found"})
            return
        self._send(
            200,
            {
                "ok": True,
                "policy": str(self.bridge.control.policy.source),
                "environment": self.bridge.control.environment,
                "mode": str(self.bridge.control.policy.mode),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self.bridge.authorized(self.headers.get("X-CTRLRun-Token")):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "bad_length"})
            return
        if length > 1_048_576:
            self._send(413, {"error": "too_large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad_json"})
            return
        if not isinstance(body, Mapping):
            self._send(400, {"error": "bad_body"})
            return
        routes = {
            "/v1/decide": self.bridge.decide,
            "/v1/resolve": self.bridge.resolve,
            "/v1/outcome": self.bridge.outcome,
        }
        route = routes.get(self.path)
        if route is None:
            self._send(404, {"error": "not_found"})
            return
        self._send(200, route(body))


def serve(
    *,
    policy: str | None = None,
    environment: str | None = None,
    agent: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8931,
    token: str | None = None,
    outcome_timeout: float = OUTCOME_TIMEOUT,
    approval_timeout: float = APPROVAL_TIMEOUT,
) -> None:
    """Run the bridge until the process is stopped."""
    secret = token or os.environ.get("CTRLRUN_OPENCLAW_TOKEN")
    if not secret:
        raise SystemExit(
            "a shared secret is required: set CTRLRUN_OPENCLAW_TOKEN or pass --token. The "
            "plugin sends it on every request, and without one any process on this machine "
            "could answer approvals."
        )
    bridge = Bridge(
        policy,
        secret,
        agent=agent,
        environment=environment,
        outcome_timeout=outcome_timeout,
        approval_timeout=approval_timeout,
    )
    control = bridge.control

    handler = type("_BoundHandler", (_Handler,), {"bridge": bridge})
    server = ThreadingHTTPServer((host, port), handler)
    _LOG.info(
        "ctrlrun openclaw bridge on http://%s:%d, policy %s", host, port, control.policy.source
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
