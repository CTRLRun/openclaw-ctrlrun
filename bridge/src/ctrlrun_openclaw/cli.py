# SPDX-FileCopyrightText: 2026 The CTRLRun contributors
# SPDX-License-Identifier: Apache-2.0
"""`ctrlrun-openclaw-bridge`, the command the plugin spawns."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from .bridge import APPROVAL_TIMEOUT, OUTCOME_TIMEOUT, serve


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctrlrun-openclaw-bridge",
        description="Answer OpenClaw's before_tool_call from a CTRLRun policy.",
    )
    parser.add_argument("--policy", default=None, help="Policy file. Default: $CTRLRUN_CONFIG, else ./ctrlrun.yaml")
    parser.add_argument(
        "--agent",
        default=None,
        help=(
            "The agent name this install acts under. Set by you, never taken from the "
            "OpenClaw session: a principal supplied by the caller is not an authorization "
            "input (SPEC-v0.3 §4.2). Omit only where the policy configures an identity "
            "provider that answers."
        ),
    )
    parser.add_argument("--environment", default=None, help="The deployment this bridge acts in.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback only. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8931, help="Default: 8931")
    parser.add_argument("--token", default=None, help="Shared secret. Default: $CTRLRUN_OPENCLAW_TOKEN")
    parser.add_argument(
        "--outcome-timeout",
        type=float,
        default=OUTCOME_TIMEOUT,
        help=(
            "How long to wait for after_tool_call before the effect is AMBIGUOUS. "
            f"Default: {OUTCOME_TIMEOUT:g}s"
        ),
    )
    parser.add_argument("--approval-timeout", type=float, default=APPROVAL_TIMEOUT, help="Parked-approval ceiling.")
    parser.add_argument("--verbose", action="store_true", help="Log every decision.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error(
            f"--host {args.host!r} is not loopback. This server fronts the operator's "
            "approval authority and has no business on another interface."
        )
    serve(
        policy=args.policy,
        agent=args.agent,
        environment=args.environment,
        host=args.host,
        port=args.port,
        token=args.token,
        outcome_timeout=args.outcome_timeout,
        approval_timeout=args.approval_timeout,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
